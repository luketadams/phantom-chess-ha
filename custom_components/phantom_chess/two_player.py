"""Two people at the board: recording, out-of-sync handling and PGN saving.

Methods of PhantomChessCoordinator, defined here as functions and bound
onto the class in coordinator.py. Moved verbatim from coordinator.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import PhantomChessCoordinator  # noqa: F401

import chess


from .runtime import (  # noqa: F401 — shared names
    AI_VS_AI_TWO_STEP_SETTLE_S,
    BleakClient,
    BleakError,
    BleakGATTCharacteristic,
    _phantom_to_uci,
    _rotate_uci_180,
    _is_move_frame,
    _build_matrix_from_fen_module,
    _check_consistency,
    _diff_grid_vs_sensor,
    _format_mismatch_instructions,
    _grid_to_fen,
    _parse_matrix_notification,
)

from .const import (
    STATUS_CHECKMATE,
    STATUS_DRAW,
    STATUS_IDLE,
    STATUS_PLAYING,
    STATUS_STALEMATE,
)

# Same logger as before the split, so log filters keep working.
_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".coordinator")


async def async_start_two_player_game(self: PhantomChessCoordinator) -> None:
    """Activate two-player recording atomically with other game starts."""
    async with self._local_start_lock:
        self._assert_no_active_game()
        try:
            await self._async_start_two_player_game()
        except (Exception, asyncio.CancelledError):
            self._two_player_active = False
            self._state["two_player_active"] = False
            self._state["lichess_game_id"] = None
            self._state["game_status"] = STATUS_IDLE
            self.async_set_updated_data(dict(self._state))
            raise


async def _async_start_two_player_game(self: PhantomChessCoordinator) -> None:
    """Start a two-human recording game on the physical board.

    Both players move the physical pieces; the board's sensors report each
    move (SIDE-0 "2-local-player" firmware mode, no AI/Lichess opponent).
    Every detected move is funneled through the same analysis pipeline as
    the local/AI games, so the rich learning view lights up live: the eval
    meter, per-move classification glyphs and the move history. On game end
    the PGN is saved under <config>/phantom_chess/recordings/. v0.4-beta2.
    """
    if not self._ble_connected:
        raise RuntimeError("Board not connected via Bluetooth")
    if self._lichess_task and not self._lichess_task.done():
        self._lichess_task.cancel()
    _lt = getattr(self, "_local_game_task", None)
    if _lt is not None and not _lt.done():
        _lt.cancel()

    self._board = chess.Board()
    self._game_id = None
    self._our_color = chess.WHITE
    self._processed_moves = 0
    self._local_game_active = False
    self._ai_vs_ai_active = False
    self._two_player_active = False
    self._state["game_status"] = "starting"
    self._state["last_move"] = None
    self._state["lichess_game_id"] = "two_player"
    self._state["live_fen"] = self._board.board_fen()
    self._state["local_game_active"] = False
    self._state["two_player_active"] = False
    self._state["two_player_out_of_sync"] = False
    self._state["lichess_active"] = False
    self._state["lichess_review_ready"] = False
    self._state["move_history_moves"] = []
    self._state["opening_name"] = None
    self._state["opening_eco"] = None
    self._state["eval_cp"] = None
    self._state["eval_mate"] = None
    self._state["eval_source"] = None
    self._state["eval_depth"] = None
    self._state["best_move_san"] = None
    self._state["threat_san"] = None
    self._state["last_move_classification"] = None
    self._state["last_move_cpl"] = None
    self._state["last_move_motif"] = None
    self._state["last_game_result"] = None
    self._state["last_game_accuracy_white"] = None
    self._state["last_game_accuracy_black"] = None
    self._state["last_game_top_mistakes"] = []
    self._analysis_board = chess.Board()
    self._state["lichess_white_name"] = "White"
    self._state["lichess_black_name"] = "Black"
    self.paused = False

    try:
        await self._phantom_send_game_assistance(
            auto_castling=True, auto_en_passant=True, auto_snap_to_center=True,
            auto_correct_wrong_move=True, advanced_capture=True, strict_gameplay=False,
        )
    except Exception as _ga_err:
        _LOGGER.warning("Two-player: GAME_ASSISTANCE write failed: %s", _ga_err)

    # SIDE opcode "0" = 2-local-player: the board detects both players'
    # physical moves and never waits for a BLE/AI reply.
    await self.async_phantom_start_game(side="W", side_opcode="0")
    self._two_player_active = True
    self._state["two_player_active"] = True
    self._state["game_status"] = STATUS_PLAYING
    self.hass.async_create_task(self._analyze_starting_position())
    self.hass.async_create_task(self._announce_via_tts(
        "Two-player recording started. White to move."
    ))
    self.async_set_updated_data(dict(self._state))
    _LOGGER.info("Two-player recording started (SIDE-0 2-local-player)")


def _flag_two_player_out_of_sync(
    self: PhantomChessCoordinator, raw_uci: str, rotated_uci: str,
) -> None:
    """Surface a rejected (illegal) physical move during a two-player game.

    Runs on the event loop (scheduled via call_soon_threadsafe from the
    BLE callback). Sets a dashboard flag, posts a persistent notification,
    and announces once per desync episode (re-fires only after a legal
    move clears the flag) so repeated sensor wiggles don't spam.
    v0.4-beta2 — fixes the silent-rejection gap found 2026-06-03.
    """
    if self._state.get("two_player_out_of_sync"):
        return  # already flagged; don't re-notify until a legal move clears it
    self._state["two_player_out_of_sync"] = True
    self.async_set_updated_data(dict(self._state))
    _LOGGER.info(
        "Two-player out-of-sync: rejected illegal move (raw=%s rotated=%s)",
        raw_uci, rotated_uci,
    )
    try:
        self.hass.async_create_task(self.hass.services.async_call(
            "persistent_notification", "create",
            {
                "title": "Phantom Chess — move not legal",
                "message": (
                    "That move isn't legal in the current position, so it "
                    "wasn't recorded. Put the piece back where it was and "
                    "play a legal move to continue. If the pieces and the "
                    "dashboard still disagree, run the "
                    "**phantom_chess.resync_two_player** action to drive the "
                    "board back to the last recorded position."
                ),
                "notification_id": "phantom_chess_two_player_sync",
            },
        ))
    except Exception as err:
        _LOGGER.debug("two-player sync notification failed: %s", err)
    if self._should_announce_active_game():
        self.hass.async_create_task(self._announce_via_tts(
            "That move isn't legal. Please put the piece back and try again."
        ))


def _clear_two_player_out_of_sync(self: PhantomChessCoordinator) -> None:
    """Clear the two-player out-of-sync flag + dismiss its notification."""
    self._state["two_player_out_of_sync"] = False
    try:
        self.hass.async_create_task(self.hass.services.async_call(
            "persistent_notification", "dismiss",
            {"notification_id": "phantom_chess_two_player_sync"},
        ))
    except Exception as err:
        _LOGGER.debug("two-player sync dismiss failed: %s", err)


async def async_resync_two_player(self: PhantomChessCoordinator) -> None:
    """Re-drive the physical board to the last recorded (model) position.

    Conservative recovery for the two-player out-of-sync case. Rather than
    guessing the physical layout from sensors, it re-asserts self._board
    (the last legal recorded position) onto the board via the snapshot
    protocol, keeping SIDE-0 2-local-player mode, so the pieces and the
    dashboard agree again and play can continue. The recorded move history
    (and therefore the saved PGN) is untouched.

    EXPERIMENTAL (v0.4-beta2) — the snapshot drive is proven for AI moves
    but its behaviour while the firmware is in SIDE-0 2-local-player mode
    still needs live-hardware verification. Best-effort: a drive timeout is
    logged, not raised.
    """
    if not self._two_player_active:
        raise RuntimeError("No two-player recording is active.")
    if not self._ble_connected:
        raise RuntimeError("Board not connected via Bluetooth")
    fen = self._board.fen()
    side_letter = "W" if self._board.turn == chess.WHITE else "B"
    _LOGGER.info(
        "Two-player resync: re-driving board to model FEN %s (side=%s)",
        fen, side_letter,
    )
    # Re-send the snapshot from the current model position; side_opcode="0"
    # keeps the firmware in 2-local-player mode after the drive.
    self._phantom_session_initialized = False
    ok = await self._phantom_execute_position(
        fen=fen, side=side_letter, timeout_s=60.0, side_opcode="0",
    )
    self._clear_two_player_out_of_sync()
    self.async_set_updated_data(dict(self._state))
    if not ok:
        _LOGGER.warning(
            "Two-player resync: BLE_MOVE_DONE timed out — board may still "
            "be settling. Model FEN: %s", fen,
        )
    self.hass.async_create_task(self._announce_via_tts(
        "Board re-synced to the last recorded position. Continue playing."
    ))


async def _finalize_two_player_game(self: PhantomChessCoordinator) -> None:
    """End a two-player recording: set result, build review, save PGN."""
    if not self._two_player_active:
        return
    if self._board.is_checkmate():
        self._state["game_status"] = STATUS_CHECKMATE
        winner = "0-1" if self._board.turn == chess.WHITE else "1-0"
        self._state["last_game_result"] = winner + " (checkmate)"
    elif self._board.is_stalemate():
        self._state["game_status"] = STATUS_STALEMATE
        self._state["last_game_result"] = "1/2-1/2 (stalemate)"
    elif self._board.is_insufficient_material():
        self._state["game_status"] = STATUS_DRAW
        self._state["last_game_result"] = "1/2-1/2 (insufficient material)"
    elif self._board.is_seventyfive_moves() or self._board.is_fivefold_repetition():
        self._state["game_status"] = STATUS_DRAW
        self._state["last_game_result"] = "1/2-1/2 (75-move/repetition)"
    else:
        self._state["game_status"] = STATUS_IDLE
        self._state["last_game_result"] = "* (ended early)"

    self._two_player_active = False
    self._state["two_player_active"] = False
    # Clear the game marker like the sculpture loop does at completion —
    # the dashboard's two-player branch keys on lichess_game_id ==
    # "two_player", so leaving it set kept the recording view (and the
    # lichess_game_id sensor) stuck after the game ended (observed live
    # 2026-07-02: game_status idle + sensor still "two_player"). The
    # post-game review view keys on lichess_review_ready, not game_id.
    self._state["lichess_game_id"] = None
    self._state["lichess_review_ready"] = True
    self.async_set_updated_data(dict(self._state))

    try:
        await self.hass.async_add_executor_job(self._save_two_player_pgn)
    except Exception as err:
        _LOGGER.warning("Two-player: PGN save failed: %s", err)

    self.hass.async_create_task(self._build_post_game_review())
    _LOGGER.info(
        "Two-player recording ended: result=%s after %d plies",
        self._state.get("last_game_result"), len(self._board.move_stack),
    )


def _save_two_player_pgn(self: PhantomChessCoordinator) -> str:
    """Write the recorded game to <config>/phantom_chess/recordings/<ts>.pgn.

    Runs in the executor (blocking file IO). Returns the path or None.

    The PGN is rebuilt from the *displayed* move history
    (``move_history_moves``, each entry carrying its UCI) rather than
    directly from ``self._board``. ``self._board`` is mutated inline by
    the discovery callback and can drift from the analysis/history
    pipeline that feeds the dashboard (observed 2026-06-03: a saved PGN
    had 7 plies while the dashboard showed 9). Rebuilding from the
    history guarantees the saved game matches what the user actually saw.
    ``self._board`` is used only as a fallback when the history is empty
    or doesn't replay cleanly. A divergence is logged (with both ply
    counts and FENs) to root-cause the underlying self._board drift.
    """
    import os
    import datetime
    import chess.pgn

    history = self._state.get("move_history_moves") or []
    hist_ucis = [h.get("uci") for h in history if h.get("uci")]

    pgn_board = chess.Board()
    replayed = 0
    replay_clean = True
    for uci in hist_ucis:
        try:
            mv = chess.Move.from_uci(uci)
        except ValueError:
            replay_clean = False
            break
        if mv not in pgn_board.legal_moves:
            replay_clean = False
            break
        pgn_board.push(mv)
        replayed += 1

    board_plies = len(self._board.move_stack)
    if replayed != board_plies:
        _LOGGER.warning(
            "Two-player PGN: displayed history has %d plies (%d replayed) "
            "but self._board has %d — root-cause the drift. "
            "history_fen=%s board_fen=%s",
            len(hist_ucis), replayed, board_plies,
            pgn_board.fen(), self._board.fen(),
        )

    # Prefer the faithfully-replayed history (matches the dashboard);
    # fall back to self._board only if the history was empty or the
    # replay hit an illegal/garbled UCI partway through.
    if replay_clean and replayed == len(hist_ucis) and replayed > 0:
        source_board = pgn_board
    else:
        if hist_ucis and not replay_clean:
            _LOGGER.warning(
                "Two-player PGN: displayed-history replay failed after %d "
                "plies (ucis=%s) — falling back to self._board",
                replayed, hist_ucis,
            )
        source_board = self._board

    game = chess.pgn.Game.from_board(source_board)
    game.headers["Event"] = "Phantom Chess two-player recording"
    game.headers["Site"] = "Phantom Chess Board"
    game.headers["Date"] = datetime.datetime.now().strftime("%Y.%m.%d")
    game.headers["White"] = "White"
    game.headers["Black"] = "Black"
    lr = self._state.get("last_game_result") or ""
    result = "*"
    for r in ("1/2-1/2", "1-0", "0-1"):
        if lr.startswith(r):
            result = r
            break
    game.headers["Result"] = result

    rec_dir = self.hass.config.path("phantom_chess", "recordings")
    os.makedirs(rec_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = os.path.join(rec_dir, "two_player_" + ts + ".pgn")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(game) + "\n")
    self._state["last_recording_pgn"] = path
    _LOGGER.info("Two-player PGN saved: %s", path)
    return path

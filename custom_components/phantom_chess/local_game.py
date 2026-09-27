"""Games against local Stockfish: start, moves, the computer's turn and stopping.

Methods of PhantomChessCoordinator, defined here as functions and bound
onto the class in coordinator.py. Moved verbatim from coordinator.py.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .coordinator import PhantomChessCoordinator  # noqa: F401

import aiohttp
import chess


from .drill_mode import DRILL_ENGINE_LEVEL
from . import runtime as rt
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
    DOMAIN,
    STATUS_CHECKMATE,
    STATUS_DRAW,
    STATUS_IDLE,
    STATUS_PAUSED,
    STATUS_PLAYING,
    UUID_GAME,
    UUID_SELECT_MODE,
)

# Same logger as before the split, so log filters keep working.
_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".coordinator")


async def async_start_local_game(self: PhantomChessCoordinator) -> None:
    """Start once, preserving an existing game when a voice request repeats."""
    async with self._local_start_lock:
        if self._state.get("physical_operation") in ("moving", "undoing", "uncertain"):
            raise RuntimeError("The board is not ready. Wait for movement to finish or check and reset the board.")
        if self._game_id or self._two_player_active or self._ai_vs_ai_active or self._sculpture_active:
            raise RuntimeError("A chess game is already running. End it before starting another.")
        if self._local_game_active:
            return
        if self._local_game_task and not self._local_game_task.done():
            self._local_game_task.cancel()
            try:
                await self._local_game_task
            except asyncio.CancelledError:
                pass
        try:
            await self._async_start_local_game()
        except (Exception, asyncio.CancelledError):
            self._local_game_active = False
            self._state["local_game_active"] = False
            self._state["lichess_game_id"] = None
            self._state["game_status"] = STATUS_IDLE
            self.async_set_updated_data(dict(self._state))
            raise


async def _async_start_local_game(self: PhantomChessCoordinator) -> None:
    """Start a local game against the built-in AI — no Lichess required.

    Delegates BLE activation to async_phantom_start_game (the validated
    protocol-0.3.0 sequence: SELECT_MODE 2 → gameStart matrix → wait for
    Waiting Side → side write). The old direct-write approach to
    UUID_GAME_CONFIG was a no-op for game start — that UUID was
    retroactively identified as the Sculpture channel (see
    phantom_chess_research/PROTOCOL.md).
    """
    if not self._ble_connected:
        raise RuntimeError("Board not connected via Bluetooth")

    # Cancel any existing Lichess stream task. The local-game task is
    # handled below via _replace_local_game_task (which both cancels
    # any in-flight turn AND awaits its cancellation before the new
    # turn starts) — that's the audit §1.4 fix path.
    if self._lichess_task and not self._lichess_task.done():
        self._lichess_task.cancel()

    # Reset state
    self._board = chess.Board()
    self._game_id = None
    self._our_color = chess.WHITE if self.player_color == "white" else (
        chess.BLACK if self.player_color == "black" else
        random.choice([chess.WHITE, chess.BLACK])
    )
    self._processed_moves = 0
    self._local_game_active = True
    self._state["game_status"] = STATUS_PLAYING
    self._state["last_move"] = None
    self._state["lichess_game_id"] = "local"
    # Pre-seed live_fen so the dashboard renders starting position immediately.
    self._state["live_fen"] = self._board.board_fen()
    # Mirror _local_game_active into state so the learning_view_active
    # binary sensor picks it up (Task #9, 2026-05-16). Also reset
    # analysis-pipeline state so the rich learning view renders cleanly
    # for local games too — the analysis hooks below are duplicated
    # from _on_game_full's Lichess path.
    self._state["local_game_active"] = True
    self._state["lichess_active"] = False  # local game is not Lichess
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
    # Fresh analysis board for the in-game learning pipeline.
    self._analysis_board = chess.Board()
    # Set player name attributes for the rich view header.
    you_name = "You"
    ai_name = f"Stockfish level {self.ai_level}"
    self._state["lichess_white_name"] = you_name if self._our_color == chess.WHITE else ai_name
    self._state["lichess_black_name"] = ai_name if self._our_color == chess.WHITE else you_name
    # Fire initial position analysis (opening name + starting eval).
    self.hass.async_create_task(self._analyze_starting_position())
    self.paused = False

    # Disable autoCorrectWrongMove + strict gameplay so the firmware doesn't
    # fight our snapshot writes during a local Stockfish game (same fix as
    # the Lichess path).
    try:
        await self._phantom_send_game_assistance(
            # Restored 2026-05-14 morning: autoCorrectWrongMove=True is
            # Efraín's default. Earlier theory that W=1 caused the 23-move
            # desync was wrong. Without W=1, the firmware detects every
            # imperfect magnet placement after an AI snapshot as a mismatch
            # and gets stuck in Managing Mismatch → Snapping Pieces, blocking
            # subsequent human-move detection. W=1 lets the firmware auto-
            # correct small placement offsets, completing the AI move cleanly.
            auto_castling=True, auto_en_passant=True, auto_snap_to_center=True,
            auto_correct_wrong_move=True, advanced_capture=True, strict_gameplay=False,
        )
    except Exception as _ga_err:
        _LOGGER.warning("Local game: GAME_ASSISTANCE write failed: %s", _ga_err)

    # Activation sequence. The matrix-`side` letter ("W"/"B") is who moves
    # first by color. The SIDE *opcode* ("0"/"1"/"2") is whether the board
    # (human) or BLE (AI) side is doing that move. If user is white,
    # they move first → SIDE "1". If user is black, AI moves first → "2".
    side_letter = "W"  # white always moves first in a fresh game
    side_opcode = "1" if self._our_color == chess.WHITE else "2"
    await self.async_phantom_start_game(side=side_letter, side_opcode=side_opcode)

    await self._begin_saved_session()

    # Announce game start via TTS.
    you_color = "White" if self._our_color == chess.WHITE else "Black"
    self.hass.async_create_task(self._announce_via_tts(
        f"Starting local Stockfish game at level {self.ai_level}. You play {you_color}."
    ))

    self.async_set_updated_data(dict(self._state))
    _LOGGER.info(
        "Local game started — we play as %s, ai_level=%d",
        "white" if self._our_color == chess.WHITE else "black",
        self.ai_level,
    )

    # If AI goes first (player is Black), generate AI's opening move
    # via the serialized replacement helper (audit §1.4).
    if self._our_color == chess.BLACK:
        await self._replace_local_game_task(name=f"{DOMAIN}_local_ai_first")


def _record_and_analyze_local_move(self: PhantomChessCoordinator, move: chess.Move, mover_is_white: bool) -> None:
    """Fire the analysis pipeline for a single local-game move.

    Mirrors what _process_move_list does inline for each Lichess move:
    records a history stub, snapshots the analysis board before/after,
    and schedules the async _analyze_move task. Without this, local
    Stockfish games render an empty rich-learning-view shell — the
    move history stays blank, eval bar stays null, classifications
    never fire. Added 2026-05-16 (Task #9).
    """
    # A legal move was recorded → the model and the physical board agree
    # again, so clear any prior two-player out-of-sync warning.
    if self._two_player_active and self._state.get("two_player_out_of_sync"):
        self._clear_two_player_out_of_sync()
    mover_color = chess.WHITE if mover_is_white else chess.BLACK
    try:
        ply_index = self._record_history_stub(move, mover_color)
    except Exception as err:
        _LOGGER.debug("local-game history stub failed for %s: %s", move.uci(), err)
        return
    board_before = self._analysis_board.copy(stack=False)
    try:
        if move in self._analysis_board.legal_moves:
            self._analysis_board.push(move)
    except Exception as err:
        _LOGGER.debug("local-game analysis-board push failed for %s: %s", move.uci(), err)
    board_after = self._analysis_board.copy(stack=False)
    if self._local_game_active:
        self._queue_checkpoint()
    self.hass.async_create_task(
        self._analyze_move(
            ply_index,
            board_before,
            board_after,
            move,
            mover_is_white,
            session_board=self._board,
        ),
        name=f"{DOMAIN}_analyze_local_ply_{ply_index}",
    )


async def _push_move_to_local_ai(self: PhantomChessCoordinator, uci: str) -> None:
    """Apply the player's physical move to the local board and get AI response."""
    if not self._local_game_active:
        return

    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        _LOGGER.debug("Local AI: invalid UCI move %s", uci)
        return

    if move not in self._board.legal_moves:
        _LOGGER.debug("Local AI: illegal move %s — ignoring", uci)
        # Reject the move via MOVEMENT_VERIFY (UUID_GAME opcode 3, payload "2").
        # Mirrors the accept-path at the discovery callback (~line 1037)
        # which writes b"\x031". Firmware 0.3.0 dropped UUID_CHECK_MOVE
        # (9cc3b57e); rejections previously written there silently failed.
        # Reference: EFRAIN_GAMEPLAY_DOC_2026-05-14.txt opcode 3
        # ("1"=verified, "2"=rejected, "Q/R/B/N"=promotion).
        try:
            await self._ble_write(UUID_GAME, b"\x032")
        except Exception:
            pass
        return

    # Determine mover BEFORE pushing (since push flips board.turn).
    mover_is_white = self._board.turn == chess.WHITE

    # Accept the move
    self._board.push(move)
    self._state["last_move"] = uci

    # Fire the analysis pipeline so the rich learning view populates.
    self._record_and_analyze_local_move(move, mover_is_white)

    if self._board.is_game_over():
        self._finish_local_game()
        return

    self._state["game_status"] = STATUS_PLAYING
    self.async_set_updated_data(dict(self._state))

    # Schedule AI response via the serialized replacement helper
    # (audit §1.4) so a concurrent dashboard / discovery-callback
    # path can't end up running two AI turns at once.
    await self._replace_local_game_task(name=f"{DOMAIN}_local_ai")


async def _replace_local_game_task(self: PhantomChessCoordinator, *, name: str) -> None:
    """Cancel any in-flight _local_game_task, await its cancellation,
    and start a fresh ``_local_ai_turn`` task in its place.

    Audit §1.4 (2026-05-19): five sites previously assigned
    ``self._local_game_task = create_task(_local_ai_turn(), ...)``
    without cancel+await. If a human move arrived mid-AI-think, the
    new task would overwrite the reference but the old task kept
    running → two AI turns computed and dispatched concurrently.
    Funneling all replacement through this helper plus the
    ``_local_game_task_lock`` guarantees only one AI turn is in
    flight at any time.

    Safe to call from any async context. Sync callers (e.g. the
    discovery callback running in ``call_soon_threadsafe``) should
    schedule it via ``hass.loop.create_task(self._replace_local_game_task(name=…))``;
    the lock serializes regardless of how it's launched.
    """
    if self._local_game_task_lock is None:
        self._local_game_task_lock = asyncio.Lock()
    async with self._local_game_task_lock:
        if self._stop_event.is_set():
            return
        old = self._local_game_task
        if old is not None and not old.done():
            old.cancel()
            try:
                await old
            except asyncio.CancelledError:
                pass
            except Exception as err:
                # Old task raised on the way out — log but don't
                # propagate; we're replacing it anyway.
                _LOGGER.debug(
                    "Local AI: prior task raised during cancellation: %s",
                    err,
                )
        self._local_game_task = self.hass.loop.create_task(
            self._local_ai_turn(), name=name,
        )


async def _local_ai_turn(self: PhantomChessCoordinator) -> None:
    """Calculate and execute the AI's response move.

    Routes the engine's move through async_phantom_apply_ai_move so the
    magnet physically moves the piece on the proven cc68a66e path.
    apply_ai_move handles both the BLE writes and the self._board push,
    so this function does NOT push the move itself — it only computes,
    validates legality, dispatches, and then reads game-end state from
    the post-push board.
    """
    board = self._board
    fen = board.fen()
    revision = self._play_revision

    def current() -> bool:
        return (self._local_game_active and not self.paused
                and self._play_revision == revision
                and not self._physical_operation_lock.locked()
                and self._board is board and board.fen() == fen)

    if not current():
        return
    await rt._sleep(0.5)  # Brief pause so board can settle
    if not current():
        return
    if (puzzle := getattr(self, "_puzzle", None)) is not None and puzzle.status == "active":
        await self._puzzle_turn()
        return
    if self._drill_active() and self._check_drill():
        return
    ai_uci = await self._get_ai_move(board.copy())
    if not current():
        return
    if not ai_uci:
        _LOGGER.error("Local AI: failed to get AI move")
        self.paused = True
        self._state["game_status"] = STATUS_PAUSED
        self._state["engine_error"] = "The chess engine is unavailable. Play is paused; try resuming after checking the engine."
        self.async_set_updated_data(dict(self._state))
        return
    self._state["engine_error"] = None

    try:
        move = chess.Move.from_uci(ai_uci)
    except ValueError:
        _LOGGER.error("Local AI: AI returned invalid UCI %s", ai_uci)
        return

    if move not in self._board.legal_moves:
        _LOGGER.error("Local AI: AI returned illegal move %s", ai_uci)
        return

    # Dispatch via the canonical snapshot executor, which pushes the move onto
    # self._board, updates fen/turn/piece_grid/etc., and sets the
    # echo-suppress window.
    #
    # Track whether the move actually got onto self._board so the
    # downstream bookkeeping (analysis pipeline, game-end detection)
    # only fires when the board state contains the move it's
    # describing. Per audit §1.3 (2026-05-19): the original code
    # ran the post-push bookkeeping unconditionally, so if BOTH
    # apply_ai_move raised AND the fallback push didn't run (e.g.
    # discovery callback already mutated _board past the AI's
    # expected turn), the analysis pipeline got a board state
    # missing the AI move and produced garbage classifications +
    # mis-derived game_status.
    try:
        delivered = await self.async_phantom_apply_ai_move(ai_uci)
    except Exception as err:
        _LOGGER.warning("Local AI move %s failed: %s", ai_uci, err)
        delivered = False
    if delivered is False:
        self._local_game_active = False
        self._state["local_game_active"] = False
        self._state["game_status"] = STATUS_PAUSED
        self.paused = True
        self.async_set_updated_data(dict(self._state))
        await self.hass.services.async_call(
            "persistent_notification", "create",
            {
                "title": "Phantom Chess: local game interrupted",
                "message": (
                    f"The board did not confirm the AI move {ai_uci}. "
                    "Play has stopped to avoid recording moves against an uncertain position. "
                    "Check the board and Bluetooth connection, then resume the saved game."
                ),
                "notification_id": "phantom_chess_local_move_failed",
            },
        )
        return

    # Fire the analysis pipeline for the AI's move so the rich learning
    # view gets its move-history entry + classification (Task #9).
    # mover_is_white is the side that JUST moved — the side opposite
    # of current board.turn (which now reflects whose turn is next).
    ai_mover_is_white = self._board.turn == chess.BLACK
    try:
        ai_move_obj = chess.Move.from_uci(ai_uci)
        self._record_and_analyze_local_move(ai_move_obj, ai_mover_is_white)
    except Exception as err:
        _LOGGER.debug("local-game AI analysis hook failed: %s", err)

    if self._board.is_game_over():
        self._finish_local_game()
        return
    if self._drill_active() and self._check_drill():
        return
    self._state["game_status"] = (
        STATUS_PAUSED if self.paused else
        "check" if self._board.is_check() else STATUS_PLAYING
    )

    self.async_set_updated_data(dict(self._state))


def _finish_local_game(self: PhantomChessCoordinator) -> None:
    """Finalize an automatic chess result for either player's last move."""
    outcome = self._board.outcome()
    if outcome is None:
        return
    if (puzzle := getattr(self, "_puzzle", None)) is not None and puzzle.status == "active":
        # The puzzle turn judges the move: a mate solves it, a stalemate
        # or other ending from a wrong move is taken back.
        # Runs on the event loop (move frames are marshalled there); this
        # method is synchronous, so the async judge turn is scheduled.
        self.hass.loop.create_task(
            self._replace_local_game_task(name=f"{DOMAIN}_puzzle_judge"),
            name=f"{DOMAIN}_puzzle_judge_schedule",
        )
        return
    if self._drill_active():
        self._check_drill()  # the drill decides success or failure
        return
    self._local_game_active = False
    self._state["local_game_active"] = False
    self._state["game_status"] = STATUS_CHECKMATE if self._board.is_checkmate() else STATUS_DRAW
    self._state["last_game_result"] = outcome.result()
    self._state["lichess_game_id"] = None
    self._state["lichess_review_ready"] = True
    self._queue_checkpoint("finished")
    self.async_set_updated_data(dict(self._state))
    self.hass.async_create_task(self._build_post_game_review())


async def _get_ai_move(self: PhantomChessCoordinator, board: chess.Board) -> str | None:
    """Get best move for a local-Stockfish game.

    Cascade:
      1. Local Stockfish via LichessAnalysisClient.best_move_for_ai_level —
         handles libc-aware download, ARM support, engine lifecycle.
      2. Lichess cloud eval (free, anonymous, internet required).
      Failure is returned explicitly; no random move is substituted.

    Refactored 2026-05-16 (Task #16/#17 release-readiness): replaces a
    parallel _find_stockfish / _stockfish_best_move pair that searched
    hardcoded /config paths, silently chmod'd uploaded binaries, and
    spawned its own subprocess. The new path goes through the proper
    StockfishFallback engine which is x86_64 AND aarch64 compatible.
    """
    fen = board.fen()

    # 1. Try local Stockfish via the shared engine.
    if self._analysis_client is not None:
        try:
            level = DRILL_ENGINE_LEVEL if self._drill_active() else self.ai_level
            uci = await self._analysis_client.best_move_for_ai_level(board, level)
            if uci:
                _LOGGER.debug("AI move via local Stockfish (level %d): %s", level, uci)
                return uci
        except Exception as sf_err:
            _LOGGER.warning("Local Stockfish move failed: %s — falling back", sf_err)

    # 2. Try Lichess cloud eval (no auth required for cloud-eval endpoint),
    #    unless the user keeps analysis local.
    client = self._analysis_client
    if client is not None and not getattr(client, "allow_cloud", True):
        return None
    try:
        session = rt.async_get_clientsession(self.hass)
        url = f"https://lichess.org/api/cloud-eval?fen={fen}&multiPv=1"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data: dict[str, Any] = await resp.json()
                pvs: list[dict[str, Any]] = data.get("pvs", [])
                if pvs:
                    moves: list[str] = pvs[0].get("moves", "").split()
                    if moves:
                        _LOGGER.debug("AI move via Lichess cloud eval: %s", moves[0])
                        return moves[0]
    except Exception as ce_err:
        _LOGGER.warning("Lichess cloud eval failed: %s", ce_err)

    # Engine failure must never silently substitute a random opponent.
    return None


async def async_stop_local_game(self: PhantomChessCoordinator) -> None:
    """Stop the local AI game and return board to idle.

    Also clears `_ai_vs_ai_active` so the AI-vs-AI loop halts on
    its next iteration.
    """
    was_active = self._local_game_active
    self.paused = True
    self._play_revision += 1
    self._local_game_active = False
    self._ai_vs_ai_active = False
    self._sculpture_active = False
    if self._local_game_task and not self._local_game_task.done():
        self._local_game_task.cancel()
        try:
            await self._local_game_task
        except (asyncio.CancelledError, Exception):
            pass
    if was_active:
        await self.async_checkpoint("finished")
    self._saved_game_id = None
    self._clear_puzzle()
    self._clear_drill()
    self._state["game_status"] = STATUS_IDLE
    self._state["lichess_game_id"] = None
    self._state["local_game_active"] = False  # Task #9
    self._game_id = None
    self.async_set_updated_data(dict(self._state))
    try:
        await self._ble_write(UUID_SELECT_MODE, b"3")  # pause/idle mode
    except Exception:
        pass

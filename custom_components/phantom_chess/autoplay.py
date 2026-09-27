"""Human-free modes: historic-game (sculpture) playback and computer against computer.

Methods of PhantomChessCoordinator, defined here as functions and bound
onto the class in coordinator.py. Moved verbatim from coordinator.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .coordinator import PhantomChessCoordinator  # noqa: F401

import chess


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
    STATUS_PLAYING,
    STATUS_STALEMATE,
    UUID_SELECT_MODE,
)

# Same logger as before the split, so log filters keep working.
_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".coordinator")


def _load_sculpture_games_blocking(self: PhantomChessCoordinator) -> dict[str, Any]:
    """Read and parse the bundled sculpture move-data file (blocking IO).

    Returns the ``games`` mapping: label → {white, black, date, eco,
    result, moves:[uci,…]}. Called via the executor and cached on
    ``self._sculpture_games_cache`` so the file is read at most once.
    """
    import pathlib

    path = pathlib.Path(__file__).parent / "sculpture_games.json"
    with open(path, encoding="utf-8") as fh:
        games: dict[str, Any] = json.load(fh).get("games", {})
        return games


async def _async_get_sculpture_games(self: PhantomChessCoordinator) -> dict[str, Any]:
    """Lazy-load + cache the bundled sculpture move catalog."""
    if self._sculpture_games_cache is None:
        try:
            self._sculpture_games_cache = await self.hass.async_add_executor_job(
                self._load_sculpture_games_blocking
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Sculpture: failed to load move catalog: %s", err)
            self._sculpture_games_cache = {}
    # Every path through the `if` above ends with a non-None assignment,
    # and the `is None` guard means we already had one otherwise — but
    # mypy can't trace that across the try/except, so narrow explicitly.
    assert self._sculpture_games_cache is not None
    return self._sculpture_games_cache


async def async_play_selected_sculpture(self: PhantomChessCoordinator) -> None:
    """Start this mode only when the board and existing session are idle."""
    async with self._local_start_lock:
        self._assert_no_active_game()
        self._saved_game_id = None
        try:
            await self._async_play_selected_sculpture()
        except (Exception, asyncio.CancelledError):
            self._local_game_active = False
            self._sculpture_active = False
            self._state["local_game_active"] = False
            self._state["game_status"] = STATUS_IDLE
            self.async_set_updated_data(dict(self._state))
            raise


async def _async_play_selected_sculpture(self: PhantomChessCoordinator) -> None:
    """Play the selected historic game on the physical board — exactly
    ONE game, driven by the integration, then stop.

    The board is driven move-by-move through the SAME snapshot protocol
    AI-vs-AI uses (chess-play mode, SELECT_MODE 2): each historic ply is
    pushed onto ``self._board`` and the magnet places the pieces. When the
    move list is exhausted the game ends and the post-game review is built.
    Because the integration owns the move stream, only the selected game
    plays — there's no firmware playlist loop to run on and cycle to other
    games (the pre-beta3 behaviour, which entered firmware SELECT_MODE 1
    and let the board autonomously loop through its built-in library).

    Falls back to the legacy firmware-sculpture stub (SELECT_MODE 1 + a
    notification) only if the selected label has no bundled move data.
    """
    if not self._ble_connected:
        raise RuntimeError("Board not connected via Bluetooth")

    games = await self._async_get_sculpture_games()
    record = games.get(self.selected_sculpture)
    if not record or not record.get("moves"):
        # Unknown / un-bundled game — preserve the old firmware behaviour
        # rather than fail outright.
        _LOGGER.warning(
            "Sculpture: no bundled moves for %r; falling back to firmware "
            "sculpture mode", self.selected_sculpture,
        )
        await self._async_start_sculpture()
        try:
            await self.hass.services.async_call(
                "persistent_notification", "create",
                {
                    "title": "Phantom Chess: Sculpture playback",
                    "message": (
                        f"Selected: **{self.selected_sculpture}**\n\n"
                        f"No bundled move data for this game, so the board's "
                        f"own firmware library is playing instead."
                    ),
                    "notification_id": "phantom_chess_sculpture_stub",
                },
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("play_selected_sculpture: notification failed: %s", err)
        return

    moves = list(record["moves"])
    white = record.get("white") or "White"
    black = record.get("black") or "Black"

    # Cancel any active game tasks (mirrors async_start_ai_vs_ai_game).
    if self._lichess_task and not self._lichess_task.done():
        self._lichess_task.cancel()
    if self._local_game_task and not self._local_game_task.done():
        self._local_game_task.cancel()

    # Fresh board + game state. Shaped like async_start_ai_vs_ai_game but
    # tagged with the "sculpture" sentinel so the dashboard can present a
    # dedicated playback view.
    self._board = chess.Board()
    self._game_id = None
    self._our_color = chess.WHITE  # board-player side flag stays "W"
    self._processed_moves = 0
    self._local_game_active = True
    self._state["game_status"] = STATUS_PLAYING
    self._state["last_move"] = None
    self._state["lichess_game_id"] = "sculpture"
    self._state["live_fen"] = self._board.board_fen()
    self._state["local_game_active"] = True
    self._state["lichess_active"] = False
    self._state["lichess_review_ready"] = False
    self._state["move_history_moves"] = []
    self._state["opening_name"] = None
    self._state["opening_eco"] = record.get("eco") or None
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
    self._state["lichess_white_name"] = f"{white} (W)"
    self._state["lichess_black_name"] = f"{black} (B)"
    self.paused = False
    self._sculpture_active = True

    self.hass.async_create_task(self._analyze_starting_position())

    # Same GAME_ASSISTANCE flags local games use, so the firmware handles
    # castling / en-passant / captures / snap-to-center for us.
    try:
        await self._phantom_send_game_assistance(
            auto_castling=True, auto_en_passant=True,
            auto_snap_to_center=True, auto_correct_wrong_move=True,
            advanced_capture=True, strict_gameplay=False,
        )
    except Exception as _ga_err:  # noqa: BLE001
        _LOGGER.warning("Sculpture: GAME_ASSISTANCE write failed: %s", _ga_err)

    # Intro announcement (the per-ply TTS is intentionally suppressed for
    # sculpture — see _should_announce_active_game).
    self.hass.async_create_task(self._announce_via_tts(
        f"Now playing {self.selected_sculpture}. {white} as white, "
        f"{black} as black."
    ))

    # Activate firmware with the standard starting position, board-as-white.
    await self.async_phantom_start_game(side="W", side_opcode="1")

    self.async_set_updated_data(dict(self._state))
    _LOGGER.info(
        "Sculpture playback started: %s (%d plies)",
        self.selected_sculpture, len(moves),
    )

    self._local_game_task = self.hass.loop.create_task(
        self._sculpture_loop(moves), name=f"{DOMAIN}_sculpture_loop",
    )


async def _sculpture_loop(self: PhantomChessCoordinator, moves: list[str]) -> None:
    """Drive the pre-loaded historic game move-by-move, then stop.

    Mirrors ``_ai_vs_ai_loop`` but the move source is the bundled UCI
    list instead of Stockfish. Honors ``self._sculpture_active`` for a
    clean stop from ``async_stop_local_game`` / ``async_back_to_modes``,
    and re-drives the absolute position after a transient BLE drop
    (identical recovery to AI-vs-AI). Ends after the final ply — one game,
    no loop.
    """
    try:
        await rt._sleep(self._sculpture_move_delay)

        # M3: consecutive move-delivery failures (see _ai_vs_ai_loop).

        for uci in moves:
            if not self._sculpture_active:
                break
            # A move that isn't legal on the tracked board means state
            # drift — stop rather than desync the physical board.
            try:
                mv = chess.Move.from_uci(uci)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Sculpture: bad UCI %r; stopping", uci)
                break
            if mv not in self._board.legal_moves:
                _LOGGER.warning(
                    "Sculpture: %s illegal at ply %d (%s); stopping",
                    uci, len(self._board.move_stack), self._board.fen(),
                )
                break

            is_two_step_move = (
                self._board.is_capture(mv) or self._board.is_castling(mv)
            )

            ply_delivered: bool = True
            try:
                ply_delivered = await self.async_phantom_apply_ai_move(uci)
            except Exception as err:
                _LOGGER.warning("Sculpture playback: movement failed; stopping: %s", err)
                self._notify_wedge_circuit_breaker("Sculpture playback")
                break

            if ply_delivered is False:
                self._notify_wedge_circuit_breaker("Sculpture playback")
                break

            # Feed the analysis pipeline so the learning view populates.
            try:
                mover_was_white = (self._board.turn == chess.BLACK)
                self._record_and_analyze_local_move(mv, mover_was_white)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Sculpture: analysis hook failed: %s", err)

            settle = self._sculpture_move_delay
            if is_two_step_move:
                settle = max(settle, AI_VS_AI_TWO_STEP_SETTLE_S)
            await rt._sleep(settle)

        # Terminal handling. If we played the whole game, report its
        # historic result; if stopped early, just go idle.
        played_all = self._sculpture_active and len(self._board.move_stack) >= len(moves)
        if played_all:
            if self._board.is_checkmate():
                self._state["game_status"] = STATUS_CHECKMATE
                winner = "0-1" if self._board.turn == chess.WHITE else "1-0"
                self._state["last_game_result"] = f"{winner} (checkmate)"
            else:
                self._state["game_status"] = STATUS_IDLE
                self._state["last_game_result"] = "Historic game complete"
            self._state["lichess_review_ready"] = True
            review = True
        else:
            self._state["game_status"] = STATUS_IDLE
            review = False

        self._sculpture_active = False
        self._local_game_active = False
        self._state["local_game_active"] = False
        # Clear the sculpture sentinel so the dashboard's playback view
        # yields — to the post-game review (if any), else the picker — and
        # so re-entering the Sculpture Library shows the chooser, not a
        # stale "Now playing" card for the game that just finished.
        self._state["lichess_game_id"] = None
        self.async_set_updated_data(dict(self._state))
        if review:
            self.hass.async_create_task(self._announce_via_tts(
                f"{self.selected_sculpture} complete."
            ))
            self.hass.async_create_task(self._build_post_game_review())
        _LOGGER.info(
            "Sculpture playback ended: %s after %d plies (complete=%s)",
            self.selected_sculpture, len(self._board.move_stack), played_all,
        )
    except asyncio.CancelledError:
        _LOGGER.debug("Sculpture: loop cancelled")
        self._sculpture_active = False
        self._local_game_active = False
        raise
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Sculpture: loop failed unexpectedly: %s", err)
        self._sculpture_active = False
        self._local_game_active = False
        self._state["local_game_active"] = False
        self.async_set_updated_data(dict(self._state))


async def async_start_sculpture(self: PhantomChessCoordinator) -> None:
    """Start this mode only when the board and existing session are idle."""
    async with self._local_start_lock:
        self._assert_no_active_game()
        try:
            await self._async_start_sculpture()
        except (Exception, asyncio.CancelledError):
            self._local_game_active = False
            self._sculpture_active = False
            self._state["local_game_active"] = False
            self._state["game_status"] = STATUS_IDLE
            self.async_set_updated_data(dict(self._state))
            raise


async def _async_start_sculpture(self: PhantomChessCoordinator) -> None:
    """Enter sculpture mode (firmware mode 1).

    Writes "1" to UUID_SELECT_MODE — the firmware enters playlist-replay
    mode. Without a populated playlist (UUID_PLAYLIST/_DB) this currently
    just transitions the firmware into the sculpture sub-state; no game
    plays back yet. Wiring the famous-games library
    (phantom_chess_research/plays_sculpture_v408.json) to UUID_PLAYLIST
    is the follow-up to make this actually replay games.

    Stops any active game first via GAME_END to avoid mode-conflict.
    """
    if not self._ble_connected:
        raise RuntimeError("BLE not connected")
    from .const import MODE_SCULPTURE
    # Clean any prior chess-play state.
    await self._phantom_send_game_end()
    await rt._sleep(0.3)
    # Switch to sculpture mode.
    await self._ble_write(UUID_SELECT_MODE, str(MODE_SCULPTURE).encode())
    _LOGGER.info(
        "Sculpture: entered firmware SELECT_MODE=%d. Playlist content not yet wired.",
        MODE_SCULPTURE,
    )
    # Reset session flag so the next chess-play start_game re-inits properly.
    self._phantom_session_initialized = False


async def async_start_ai_vs_ai_game(
    self: PhantomChessCoordinator, white_ai_level: int | None = None, black_ai_level: int | None = None,
    move_delay_seconds: float = 1.5,
) -> None:
    """Serialize spectator activation with every other mode."""
    async with self._local_start_lock:
        self._assert_no_active_game()
        self._saved_game_id = None
        try:
            await self._async_start_ai_vs_ai_game(white_ai_level, black_ai_level, move_delay_seconds)
        except (Exception, asyncio.CancelledError):
            self._local_game_active = False
            self._ai_vs_ai_active = False
            self._state["local_game_active"] = False
            self._state["game_status"] = STATUS_IDLE
            self.async_set_updated_data(dict(self._state))
            raise


async def _async_start_ai_vs_ai_game(
    self: PhantomChessCoordinator,
    white_ai_level: int | None = None,
    black_ai_level: int | None = None,
    move_delay_seconds: float = 1.5,
) -> None:
    """Start a Stockfish-vs-Stockfish game on the physical board.

    Both sides are played by the local Stockfish engine via the same
    snapshot protocol used for normal AI moves. Drives the magnet
    for every move from both colors. Runs until checkmate, stalemate,
    draw, or `async_stop_local_game` is called.

    Useful for:
      - Autonomous protocol testing — exercises every move type
        (castling, captures, promotions) without requiring a human
        at the board.
      - "Watch the AI play itself" demo — the dashboard's learning
        view, eval bar, move classifications, and post-game review
        all render in real time as the game progresses.
      - Stress testing — long games surface BLE reconnect, magnet
        timing, and state-machine edge cases that single-game
        testing misses.

    Args:
        white_ai_level: Stockfish skill for white (1-8). Defaults to
            the integration's current `ai_level`.
        black_ai_level: Stockfish skill for black (1-8). Defaults to
            the same as `white_ai_level`.
        move_delay_seconds: Seconds to wait between move completions.
            Lower values run the game faster but stress the magnet
            and BLE link harder. Defaults to 1.5s.
    """
    if not self._ble_connected:
        raise RuntimeError("Board not connected via Bluetooth")
    # Cancel any active game tasks.
    if self._lichess_task and not self._lichess_task.done():
        self._lichess_task.cancel()
    if self._local_game_task and not self._local_game_task.done():
        self._local_game_task.cancel()

    # Reset state — same shape as async_start_local_game but with a
    # distinct lichess_game_id sentinel so the dashboard can
    # optionally surface "watch AI" mode differently.
    self._board = chess.Board()
    self._game_id = None
    # _our_color is arbitrary for AI-vs-AI but must be set for
    # async_phantom_apply_ai_move's side-flag computation.
    self._our_color = chess.WHITE
    self._processed_moves = 0
    self._local_game_active = True
    self._state["game_status"] = STATUS_PLAYING
    self._state["last_move"] = None
    self._state["lichess_game_id"] = "ai_vs_ai"
    self._state["live_fen"] = self._board.board_fen()
    self._state["local_game_active"] = True
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
    # Header names for the rich learning-view card.
    w_level = white_ai_level if white_ai_level is not None else self.ai_level
    b_level = black_ai_level if black_ai_level is not None else w_level
    self._state["lichess_white_name"] = f"Stockfish level {w_level} (W)"
    self._state["lichess_black_name"] = f"Stockfish level {b_level} (B)"
    self.hass.async_create_task(self._analyze_starting_position())
    self.paused = False

    # Save AI-vs-AI params on self so the loop can read them.
    self._ai_vs_ai_white_level = w_level
    self._ai_vs_ai_black_level = b_level
    self._ai_vs_ai_move_delay = max(0.0, float(move_delay_seconds))
    self._ai_vs_ai_active = True

    # Set the same GAME_ASSISTANCE flags local games use.
    try:
        await self._phantom_send_game_assistance(
            auto_castling=True, auto_en_passant=True,
            auto_snap_to_center=True, auto_correct_wrong_move=True,
            advanced_capture=True, strict_gameplay=False,
        )
    except Exception as _ga_err:
        _LOGGER.warning(
            "AI-vs-AI: GAME_ASSISTANCE write failed: %s", _ga_err,
        )

    # TTS announcement.
    self.hass.async_create_task(self._announce_via_tts(
        f"Starting Stockfish self-play. White at level {w_level}, "
        f"black at level {b_level}."
    ))

    # Activate firmware with the standard starting position.
    side_letter = "W"
    side_opcode = "1"  # board side moves next — same as a human-as-white game
    await self.async_phantom_start_game(side=side_letter, side_opcode=side_opcode)

    self.async_set_updated_data(dict(self._state))
    _LOGGER.info(
        "AI-vs-AI started — white level %d, black level %d, "
        "move delay %.1fs",
        w_level, b_level, self._ai_vs_ai_move_delay,
    )

    # Kick off the loop. The first iteration will compute white's
    # first move (because self._board.turn == chess.WHITE on a fresh
    # board) and dispatch via async_phantom_apply_ai_move.
    self._local_game_task = self.hass.loop.create_task(
        self._ai_vs_ai_loop(), name=f"{DOMAIN}_ai_vs_ai_loop",
    )


async def _ai_vs_ai_await_reconnect(self: PhantomChessCoordinator, timeout: float = 30.0) -> bool:
    """Block (bounded) until the BLE maintain loop restores the link.

    AI-vs-AI pushes a full GAME_START snapshot every ply, keeping the
    steppers near-continuously active; under that load the board's BLE
    link occasionally drops mid-game (observed ply-9 disconnect,
    2026-05-31). ``_ble_loop`` reconnects on its own; this helper just
    waits for ``_ble_connected`` to come back so the spectator game can
    resume instead of dying on the first transient blip.

    Honors the active flag for prompt shutdown. Shared by AI-vs-AI and
    sculpture playback (beta3): both drive the board via the same snapshot
    primitive and want the same reconnect-and-re-drive recovery, so the
    "still active?" check covers either loop's flag. On reconnect it waits
    a short settle so the board can finish connect-time service discovery
    before the caller writes the re-drive snapshot. Returns True once
    reconnected (and still active), False on timeout or if the driving loop
    was stopped while waiting.
    """
    def _active() -> bool:
        return self._ai_vs_ai_active or getattr(self, "_sculpture_active", False)

    deadline = self.hass.loop.time() + timeout
    while self.hass.loop.time() < deadline:
        if not _active():
            return False
        if self._ble_connected:
            await rt._sleep(2.0)
            return self._ble_connected and _active()
        await rt._sleep(1.0)
    return False


def _notify_wedge_circuit_breaker(self: PhantomChessCoordinator, loop_label: str) -> None:
    """M3: log + raise a persistent notification when a mode loop stops
    after ``PHANTOM_EXEC_FAILURE_LIMIT`` consecutive move-delivery failures.

    A wedged board returns False from ``_phantom_execute_position`` every
    ply (BLE_MOVE_DONE never arrives), so the loop would otherwise re-drive
    forever, grinding the magnet. The caller breaks out of the loop right
    after calling this; here we just surface the stop to the user and point
    them at the recovery service. Notification failures are swallowed — a
    UI hiccup must never keep the loop spinning.
    """
    _LOGGER.warning("%s stopped after an unconfirmed movement; explicit recovery is required", loop_label)
    try:
        self.hass.async_create_task(
            self.hass.services.async_call(
                "persistent_notification", "create",
                {
                    "title": "Phantom Chess: board stopped responding",
                    "message": (
                        f"{loop_label} stopped because movement was not confirmed. "
                        "Check the board and Bluetooth connection. Use reset_position "
                        "to reconcile the pieces before restarting this mode. "
                        "resync_detection alone is not proof of physical completion."
                    ),
                    "notification_id": "phantom_chess_wedge_circuit_breaker",
                },
            )
        )
    except Exception as notif_err:  # noqa: BLE001
        _LOGGER.debug(
            "wedge circuit-breaker notification failed: %s", notif_err
        )


async def _ai_vs_ai_loop(self: PhantomChessCoordinator) -> None:
    """Background loop that plays both sides via local Stockfish.

    Honors `self._ai_vs_ai_active` for graceful shutdown via
    `async_stop_local_game`. Tracks the current side's Stockfish
    level by temporarily swapping `self.ai_level` per move so the
    existing `_get_ai_move` cascade picks up the right skill setting
    without needing a separate code path. Each move goes through
    `async_phantom_apply_ai_move`, which drives the magnet and
    pushes onto `self._board` — the loop simply reads
    `self._board.turn` to know whose level to use next.
    """
    try:
        # Small initial pause so the activation snapshot from
        # async_phantom_start_game has time to settle before we fire
        # the first move.
        await rt._sleep(self._ai_vs_ai_move_delay)

        # M3: consecutive move-delivery failures. A wedged board returns
        # False from every snapshot; PHANTOM_EXEC_FAILURE_LIMIT in a row
        # trips the circuit breaker below. Any delivered move resets it.

        while self._ai_vs_ai_active and not self._board.is_game_over():
            # Pick the level based on whose turn it is.
            level = (
                self._ai_vs_ai_white_level
                if self._board.turn == chess.WHITE
                else self._ai_vs_ai_black_level
            )
            saved_level = self.ai_level
            self.ai_level = level
            try:
                uci = await self._get_ai_move(self._board)
            finally:
                self.ai_level = saved_level

            if not uci:
                _LOGGER.warning(
                    "AI-vs-AI: Stockfish returned no move at ply %d; "
                    "stopping loop", len(self._board.move_stack),
                )
                break
            if not self._ai_vs_ai_active:
                # External stop arrived while Stockfish was computing.
                break

            # Detect a two-step physical move (capture or castle): the
            # board keeps moving the captured piece to the graveyard (or
            # the rook) AFTER reporting BLE_MOVE_DONE, so a short gap fires
            # the next snapshot into the moving magnet and wedges the
            # board. We widen the settle for these moves below.
            try:
                _mv = chess.Move.from_uci(uci)
                is_two_step_move = (
                    self._board.is_capture(_mv) or self._board.is_castling(_mv)
                )
            except Exception:  # noqa: BLE001
                is_two_step_move = False

            # Apply via the canonical AI-move path so the snapshot
            # protocol + echo suppression + analysis pipeline all
            # behave identically to a normal local game.
            ply_delivered: bool = True
            try:
                ply_delivered = await self.async_phantom_apply_ai_move(uci)
            except Exception as err:
                _LOGGER.warning("AI-vs-AI: movement failed; stopping: %s", err)
                self._notify_wedge_circuit_breaker("AI-vs-AI")
                break

            if ply_delivered is False:
                self._notify_wedge_circuit_breaker("AI-vs-AI")
                break

            # Fire the analysis pipeline for the move just played so
            # the rich learning view's history populates. We pass
            # mover_is_white based on the side that JUST moved.
            try:
                mover_was_white = (self._board.turn == chess.BLACK)
                self._record_and_analyze_local_move(
                    chess.Move.from_uci(uci), mover_was_white,
                )
            except Exception as err:
                _LOGGER.debug(
                    "AI-vs-AI: analysis hook failed at ply %d: %s",
                    len(self._board.move_stack), err,
                )

            # Brief settle gap before the next move. Captures and castles
            # are two-step physical moves whose second operation completes
            # after BLE_MOVE_DONE, so they need a longer settle or the next
            # snapshot collides with the still-moving magnet.
            settle = self._ai_vs_ai_move_delay
            if is_two_step_move:
                settle = max(settle, AI_VS_AI_TWO_STEP_SETTLE_S)
            await rt._sleep(settle)

        # Terminal handling.
        if self._board.is_checkmate():
            self._state["game_status"] = STATUS_CHECKMATE
            winner = "0-1" if self._board.turn == chess.WHITE else "1-0"
            self._state["last_game_result"] = f"{winner} (checkmate)"
        elif self._board.is_stalemate():
            self._state["game_status"] = STATUS_STALEMATE
            self._state["last_game_result"] = "1/2-1/2 (stalemate)"
        elif self._board.is_insufficient_material():
            self._state["game_status"] = STATUS_DRAW
            self._state["last_game_result"] = "1/2-1/2 (insufficient material)"
        elif (
            self._board.is_seventyfive_moves()
            or self._board.is_fivefold_repetition()
        ):
            self._state["game_status"] = STATUS_DRAW
            self._state["last_game_result"] = "1/2-1/2 (75-move/repetition)"
        else:
            # External stop or stockfish-no-move.
            self._state["game_status"] = STATUS_IDLE

        self._local_game_active = False
        self._ai_vs_ai_active = False
        self._state["local_game_active"] = False
        self._state["lichess_review_ready"] = True
        self.async_set_updated_data(dict(self._state))
        self.hass.async_create_task(self._build_post_game_review())
        _LOGGER.info(
            "AI-vs-AI ended: result=%s after %d plies",
            self._state.get("last_game_result"),
            len(self._board.move_stack),
        )
    except asyncio.CancelledError:
        _LOGGER.debug("AI-vs-AI: loop cancelled")
        self._ai_vs_ai_active = False
        self._local_game_active = False
        raise
    except Exception as err:
        _LOGGER.exception("AI-vs-AI: loop failed unexpectedly: %s", err)
        self._ai_vs_ai_active = False
        self._local_game_active = False
        self._state["local_game_active"] = False
        self.async_set_updated_data(dict(self._state))

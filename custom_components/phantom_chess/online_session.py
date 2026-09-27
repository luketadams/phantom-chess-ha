"""Online play through the Lichess Board API.

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

import aiohttp
import chess

from homeassistant.exceptions import HomeAssistantError

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
    LICHESS_CHALLENGE_AI_URL,
    LICHESS_GAME_EXPORT_URL,
    LICHESS_GAME_STREAM_URL,
    LICHESS_MOVE_URL,
    LICHESS_RESIGN_URL,
    LICHESS_RETRY_SECONDS,
    MODE_CHESS_PLAY,
    STATUS_CHECKMATE,
    STATUS_DRAW,
    STATUS_IDLE,
    STATUS_PLAYING,
    STATUS_RESIGNED,
    STATUS_STALEMATE,
    UUID_SELECT_MODE,
)

# Same logger as before the split, so log filters keep working.
_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".coordinator")


async def async_start_game(
    self, clock_limit_seconds: int = 900, clock_increment_seconds: int = 10,
) -> None:
    """Serialize online activation with local and two-player starts."""
    if not self._lichess_token:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="lichess_token_required"
        )
    async with self._local_start_lock:
        self._assert_no_active_game()
        try:
            await self._async_start_online_game(clock_limit_seconds, clock_increment_seconds)
        except (Exception, asyncio.CancelledError):
            if not self._game_id:
                self._state["game_status"] = STATUS_IDLE
                self._state["lichess_active"] = False
                self.async_set_updated_data(dict(self._state))
            raise


async def _async_start_online_game(
    self,
    clock_limit_seconds: int = 900,
    clock_increment_seconds: int = 10,
) -> None:
    """Start a new game: set board mode, create Lichess AI challenge, stream events.

    Args:
        clock_limit_seconds: Lichess clock.limit value (base time, in seconds).
            Valid range 60-10800. Defaults to 900 (15 minutes — rapid).
        clock_increment_seconds: Lichess clock.increment per move, in seconds.
            Valid range 0-180. Defaults to 10. Combined limit + increment must be ≥ 4s.
    """
    if not self._ble_connected:
        raise RuntimeError("Board not connected via Bluetooth")

    # Reset board state
    self._board = chess.Board()
    self._game_id = None
    self._our_color = None
    self._processed_moves = 0
    self._state["game_status"] = STATUS_PLAYING
    self._state["last_move"] = None
    self.paused = False
    # FIX (2026-05-14): force a fresh GAME_END → HOME precondition for the
    # new game. Without this, if the integration had ever previously sent
    # a snapshot via _phantom_execute_position (sculpture, earlier game,
    # move_piece), the next snapshot would skip the drop-to-HOME step,
    # firmware could be in a stale state, and the GAME_START would land
    # in BLE Playing instead of Waiting Side → the SIDE write that follows
    # gets silently ignored → firmware never transitions to Board Playing
    # → human moves not tracked. async_phantom_start_game has always done
    # this; async_start_game (Lichess path) was missing it.
    self._phantom_session_initialized = False

    # Set board to chess play mode
    await self._ble_write(UUID_SELECT_MODE, str(MODE_CHESS_PLAY).encode())

    # Disable autoCorrectWrongMove (W=0) so the firmware doesn't autoplay
    # "corrections" during HA-driven games. This was almost certainly the
    # cause of the 2026-05-12 23-move Lichess b2xc3 desync. Keep auto
    # castling/en-passant/snap-to-center on (the helpful ones); turn off
    # the four troublesome flags.
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
        _LOGGER.warning("Lichess game: GAME_ASSISTANCE write failed: %s", _ga_err)

    # Earlier releases wrote a (ai_level<<1)+color_bit byte to
    # what we called UUID_GAME_CONFIG (7eeaef37). Per Efraín
    # 2026-05-24, that characteristic is actually UUID_SCULPTURE in
    # firmware and the firmware does NOT interpret the byte — the
    # encoding was app-side only. The write was a no-op end-to-end:
    # firmware accepted it, did nothing with it. Removed 2026-05-24
    # (post-Efraín-reply audit). The integration's authoritative AI
    # level + color signaling lives in the GAME_START opcode 0
    # snapshot's matrix + side flag (via _phantom_execute_position)
    # and, for Lichess, in the POST /api/challenge/ai payload itself.

    # NOTE: Earlier code wrote to UUID_MATRIX_INIT_GAME (e00b41ea...) here
    # to "initialize board LEDs for new game". That characteristic does
    # NOT exist on firmware 0.3.0 — the write was speculative leftover
    # from a 0.1.6-era probe and always failed silently into the bare
    # except. Removed 2026-05-17 after observing that the Task #12 GATT
    # staleness recovery was treating the failure as a stale-cache
    # event and force-disconnecting the BLE link mid-activation,
    # which then aborted the critical SIDE write that follows.

    # Create Lichess AI challenge
    session = rt.async_get_clientsession(self.hass)
    color_param = self.player_color  # "white" | "black" | "random"

    # Lichess clock validation rejects clock.limit=0 with "Invalid clock".
    # Valid ranges: limit 0-10800s, increment 0-180s, combined ≥ 4s.
    # Time control is configurable per-call via clock_limit_seconds and
    # clock_increment_seconds arguments (set from dashboard helpers by the
    # phantom_start_lichess_configured wrapper script). Default 900+10 is a
    # Lichess-typical rapid format; the Phantom board's slow magnet doesn't
    # fit pure-bullet pace well, but blitz onward is fine.
    payload = {
        "level": self.ai_level,
        "color": color_param,
        "clock.limit": int(clock_limit_seconds),
        "clock.increment": int(clock_increment_seconds),
    }

    async with session.post(
        LICHESS_CHALLENGE_AI_URL,
        headers={"Authorization": f"Bearer {self._lichess_token}"},
        data=payload,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status not in (200, 201):
            text = await resp.text()
            raise RuntimeError(f"Lichess challenge failed ({resp.status}): {text}")
        game_data = await resp.json()

    self._game_id = game_data["id"]
    # Announce game start via TTS.
    you_play = self.player_color.title() if self.player_color != "random" else "either color"
    self.hass.async_create_task(self._announce_via_tts(
        f"Starting Lichess game against AI level {self.ai_level}. You play {you_play}."
    ))
    _LOGGER.info("Lichess game started: %s", self._game_id)
    self._state["lichess_game_id"] = self._game_id
    self.async_set_updated_data(dict(self._state))

    # Cancel any existing Lichess task and start a new one
    if self._lichess_task and not self._lichess_task.done():
        self._lichess_task.cancel()

    self._lichess_task = self.hass.loop.create_task(
        self._lichess_stream_loop(self._game_id),
        name=f"{DOMAIN}_lichess",
    )
    # Supervisor: if the stream task dies while a game is still active
    # (e.g. silent failure during a BLE storm), auto-trigger a state
    # reconcile against Lichess so lichess_active doesn't stay stuck
    # ON after the game has actually ended on Lichess's side. Closes
    # Task #14 (2026-05-16).
    self._lichess_task.add_done_callback(self._lichess_task_done_cb)

    # Drive firmware into the correct active-game state based on who plays
    # first. self._our_color was set when Lichess responded with the game.
    # If we (human/board) are white, we move first → SIDE "1" (board moves).
    # If we (human/board) are black, AI moves first → SIDE "2" (BLE moves).
    # The SIDE opcode is only honored while firmware is in Waiting Side,
    # so it must be sent inside _phantom_execute_position's snapshot
    # sequence — there's no after-the-fact override path. Fixed 2026-05-14
    # after observing firmware stuck in BLE Playing when user was white.
    # The Lichess stream task is responsible for setting self._our_color;
    # it runs concurrently with this code, so we wait briefly (up to 5s)
    # for the gameFull event to be parsed before deciding the SIDE opcode.
    # Falls back to player_color preference if the stream is slow.
    for _ in range(50):  # up to 5s in 100ms increments
        if self._our_color is not None:
            break
        await rt._sleep(0.1)
    if self._our_color is not None:
        side_opcode = "1" if self._our_color == chess.WHITE else "2"
    else:
        # Stream hadn't completed; fall back to preference. For "random"
        # default to "1" (user moves first) — least disruptive failure mode.
        side_opcode = "2" if self.player_color == "black" else "1"
    _LOGGER.debug(
        "Lichess game: SIDE opcode = %s (our_color=%s, pref=%s)",
        side_opcode,
        "white" if self._our_color == chess.WHITE else ("black" if self._our_color == chess.BLACK else "unknown"),
        self.player_color,
    )
    try:
        await self._phantom_execute_position(
            fen=chess.STARTING_FEN, side="W", timeout_s=30.0,
            side_opcode=side_opcode,
        )
        _LOGGER.debug(
            "Lichess game: firmware activated with SIDE=%s (our_color=%s)",
            side_opcode, "white" if self._our_color == chess.WHITE else "black",
        )
    except Exception as fw_err:
        _LOGGER.warning(
            "Lichess game: failed to enter tracking mode: %s — "
            "human moves may not be detected",
            fw_err,
        )


def _lichess_task_done_cb(self, task: asyncio.Task) -> None:
    """Callback fired when _lichess_task finishes.

    Normal exit path: the task completed because the game ended cleanly
    (terminal gameState event received and processed, _game_id cleared).
    Unexpected exit path: the task died but _game_id is still set, which
    means the integration thinks the game is in progress but the stream
    is dead. We trigger an automatic reconcile against Lichess to catch
    terminal events the stream missed.

    Added 2026-05-16 (Task #14) — observed during the verification game:
    lichess_active stuck ON after Luke finished the game on his phone
    because the stream task died silently during a BLE storm.
    """
    try:
        if task.cancelled():
            return  # explicit cancel — normal shutdown path
        exc = task.exception()
        if exc is not None:
            _LOGGER.error(
                "Lichess stream task crashed: %s",
                exc,
                exc_info=exc,
            )
        elif self._game_id is None:
            return  # task exited cleanly AND game is done — perfect
        else:
            _LOGGER.warning(
                "Lichess stream task exited with _game_id still set "
                "(%s) — reconciling state against Lichess",
                self._game_id,
            )
        # Either case: schedule a reconcile. The reconcile method
        # itself is idempotent (no-op if Lichess still says the
        # game is active).
        if self._game_id is not None:
            self.hass.async_create_task(
                self.async_reconcile_lichess_state(),
                name=f"{DOMAIN}_lichess_reconcile",
            )
    except Exception as cb_err:
        # Done-callbacks shouldn't propagate; log and swallow.
        _LOGGER.exception("Error in _lichess_task done callback: %s", cb_err)


async def async_reconcile_lichess_state(self) -> None:
    """Query Lichess for current game status and sync local state.

    When the stream task has missed a terminal event (BLE storm,
    network blip, mobile app race), local lichess_active can stay
    stuck ON after the game has actually ended. This method does a
    one-shot REST query against /api/game/export/{gameId} and, if
    the game is terminal, synthesizes a gameState event and feeds
    it through the standard terminal handler so all downstream
    side effects fire (lichess_active=False, post-game review build,
    last_game_result, etc.).

    Safe to call repeatedly: if Lichess still says the game is
    active, this is a no-op. Auto-triggered by _lichess_task_done_cb
    when the stream task dies unexpectedly; also exposed as the
    `phantom_chess.reconcile_lichess_state` service so the user has
    a manual escape hatch.

    Added 2026-05-16 (Task #14).
    """
    if not self._game_id:
        _LOGGER.info("reconcile_lichess_state: no active game; nothing to do")
        return
    if not self._lichess_token:
        _LOGGER.warning("reconcile_lichess_state: no Lichess token configured")
        return

    url = LICHESS_GAME_EXPORT_URL.format(game_id=self._game_id)
    session = rt.async_get_clientsession(self.hass)
    headers = {
        "Authorization": f"Bearer {self._lichess_token}",
        "Accept": "application/json",
    }
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                _LOGGER.warning(
                    "reconcile_lichess_state: Lichess returned %s for game %s",
                    resp.status, self._game_id,
                )
                return
            data = await resp.json()
    except Exception as err:
        _LOGGER.warning("reconcile_lichess_state: query failed: %s", err)
        return

    status = data.get("status", "started")
    # Lichess "active" statuses we shouldn't sync from.
    if status in ("started", "created"):
        _LOGGER.info(
            "reconcile_lichess_state: game %s still active on Lichess (status=%s); no sync needed",
            self._game_id, status,
        )
        return

    # Game is terminal — synthesize a gameState event and reuse the
    # standard _on_game_state terminal-handling path. Lichess uses
    # the same status vocabulary in /api/game/export/{id} as it does
    # in the streaming gameState events, so we don't need to remap.
    _LOGGER.info(
        "reconcile_lichess_state: game %s terminal on Lichess (status=%s) "
        "but integration thought it was active — syncing state",
        self._game_id, status,
    )
    synthesized_event = {
        "type": "gameState",
        "status": status,
        "moves": data.get("moves", ""),
        "winner": data.get("winner"),
    }
    await self._on_game_state(synthesized_event)


async def async_resume_from_phone(self) -> None:
    """Push the integration's current board state to firmware via
    RESET_DETECTION (opcode 14) so the physical board re-syncs with
    what's actually been played.

    Use case: AI move failed to drive to the board after retries
    (caught by the apply_ai_move retry loop), so the user continued
    the game on Lichess.org or their phone. self._board has stayed
    in sync via the Lichess gameState stream — this method pushes
    that authoritative state to the firmware so the physical board
    catches up without resigning the Lichess game.

    Added 2026-05-16 as part of Task #7 (the third recovery tier for
    hardware errors, complementing transparent retry + reconcile).
    """
    if not self._game_id:
        _LOGGER.debug("resume_from_phone: no active game; nothing to do")
        return
    if not self._ble_connected:
        _LOGGER.warning("resume_from_phone: BLE not connected")
        return

    fen = self._board.board_fen()
    _LOGGER.debug(
        "resume_from_phone: pushing FEN %s to firmware via RESET_DETECTION",
        fen,
    )
    try:
        await self._phantom_send_reset_detection(fen)
    except Exception as err:
        _LOGGER.error("resume_from_phone: RESET_DETECTION failed: %s", err)
        raise

    # Clear the AI-move-failed notification if it's still showing.
    try:
        await self.hass.services.async_call(
            "persistent_notification", "dismiss",
            {"notification_id": "phantom_chess_ai_move_failed"},
        )
    except Exception:
        pass  # dismiss is best-effort; absence of notification is fine

    _LOGGER.info("resume_from_phone: sync complete")


async def async_start_lichess_configured(self) -> None:
    """Start a Lichess game using the clock controls + ai_level +
    player_color that the integration's select/number entities
    currently hold. Replaces the v0.3 script
    `phantom_start_lichess_configured`. Added 2026-05-26 (v0.4-alpha3).
    """
    await self.async_start_game(
        clock_limit_seconds=self.lichess_clock_minutes * 60,
        clock_increment_seconds=self.lichess_clock_increment,
    )


async def _post_resign_once(self, game_id: str) -> tuple[bool, int, str]:
    """Single best-effort resign POST to Lichess.

    Returns ``(ok, status, body)`` — ``status`` is 0 and ``body`` the
    exception text on a transport error, else the HTTP status and (on
    failure) the response body. Shared by :meth:`async_resign` (with a
    retry) and :meth:`async_back_to_modes` (single shot, non-blocking).
    """
    session = rt.async_get_clientsession(self.hass)
    url = LICHESS_RESIGN_URL.format(game_id=game_id)
    try:
        async with session.post(
            url,
            headers={"Authorization": f"Bearer {self._lichess_token}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status in (200, 201):
                return True, resp.status, ""
            return False, resp.status, await resp.text()
    except Exception as err:  # noqa: BLE001 — surfaced to caller as failure
        return False, 0, str(err)


def _clear_lichess_game_session(self) -> None:
    """Tear down local Lichess game-session state without awaiting the
    stream's terminal echo.

    Fix B/C (live 2026-07-08): if the gameState stream is dead the terminal
    event never arrives, so a successful resign (or a back_to_modes teardown)
    would otherwise leave ``lichess_active`` stuck ON with a zombie game. We
    clear ``_game_id`` FIRST so the stream task's done-callback takes its
    clean-exit branch (it keys on ``_game_id``) rather than firing a
    reconcile, then cancel the stream task and clear the session markers +
    clocks. The caller sets ``game_status`` / ``last_game_result`` first.
    """
    self._game_id = None
    task = self._lichess_task
    self._lichess_task = None
    if task is not None and not task.done():
        task.cancel()
    self._state["lichess_active"] = False
    self._state["lichess_game_id"] = None
    self._state["lichess_white_clock"] = None
    self._state["lichess_black_clock"] = None


async def async_resign(self) -> None:
    """Resign the current game.

    Fix B (live 2026-07-08): the old non-200 branch only debug/WARNING
    logged — invisible to the user, so a failed resign silently left the
    game live. Now: retry ONCE after 2s, and on final failure raise a
    persistent notification. On success clear local game state DIRECTLY
    (don't wait for the stream echo — a dead stream never delivers the
    terminal event, leaving lichess_active stuck ON).
    """
    if not self._game_id:
        return
    game_id = self._game_id
    ok, status, body = await self._post_resign_once(game_id)
    if not ok:
        _LOGGER.warning(
            "Resign failed (HTTP %s): %s — retrying once in 2s", status, body,
        )
        await rt._sleep(2)
        ok, status, body = await self._post_resign_once(game_id)
    if not ok:
        _LOGGER.warning(
            "Resign failed again (HTTP %s): %s — leaving game live on Lichess",
            status, body,
        )
        try:
            await self.hass.services.async_call(
                "persistent_notification", "create",
                {
                    "title": "Phantom Chess: Resign failed",
                    "message": (
                        f"Resign failed (HTTP {status}) — the game is still "
                        f"live on Lichess; retry or resign from the Lichess app."
                    ),
                    "notification_id": "phantom_chess_resign_failed",
                },
            )
        except Exception:  # noqa: BLE001 — notification is best-effort
            pass
        return
    self._state["game_status"] = STATUS_RESIGNED
    self._clear_lichess_game_session()
    try:
        await self.hass.services.async_call(
            "persistent_notification", "dismiss",
            {"notification_id": "phantom_chess_resign_failed"},
        )
    except Exception:  # noqa: BLE001 — dismiss is best-effort
        pass
    self.async_set_updated_data(dict(self._state))


async def _lichess_stream_loop(self, game_id: str) -> None:
    """Stream Lichess game events and bridge AI moves to the board."""
    url = LICHESS_GAME_STREAM_URL.format(game_id=game_id)
    headers = {"Authorization": f"Bearer {self._lichess_token}"}
    session = rt.async_get_clientsession(self.hass)

    retry_delay = LICHESS_RETRY_SECONDS
    while not self._stop_event.is_set() and self._game_id == game_id:
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=None, connect=15),
            ) as resp:
                if resp.status in (401, 403):
                    # Token expired / revoked. Trigger reauth flow and
                    # exit the loop — there's no point retrying with the
                    # same dead token. User will be prompted in HA UI to
                    # re-enter their token; on success the integration
                    # reloads and the stream restarts cleanly.
                    # Added 2026-05-16 (Task #20 release-readiness).
                    _LOGGER.error(
                        "Lichess stream: HTTP %s — token rejected. "
                        "Triggering reauth flow.",
                        resp.status,
                    )
                    if self._entry is not None:
                        self._entry.async_start_reauth(self.hass)
                    return
                if resp.status != 200:
                    _LOGGER.warning("Lichess stream returned %s", resp.status)
                    await rt._sleep(retry_delay)
                    continue

                retry_delay = LICHESS_RETRY_SECONDS
                async for raw_line in resp.content:
                    if self._stop_event.is_set() or self._game_id != game_id:
                        return
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue  # heartbeat
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_lichess_event(event)

        except asyncio.CancelledError:
            return
        except Exception as err:
            _LOGGER.warning("Lichess stream error: %s, retrying in %ds", err, retry_delay)
            await rt._sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


async def _handle_lichess_event(self, event: dict[str, Any]) -> None:
    """Dispatch a Lichess ndjson event."""
    etype = event.get("type")

    if etype == "gameFull":
        await self._on_game_full(event)
    elif etype == "gameState":
        await self._on_game_state(event)
    elif etype == "gameFinish":
        self._on_game_finish(event)


async def _on_game_full(self, event: dict[str, Any]) -> None:
    """First event — tells us which color we're playing.

    (Historical aside: earlier iterations parsed `event["white"]["id"]`
    and compared against the stored Lichess username. That approach was
    replaced by the simpler check below — we know our color from the
    stored `player_color` because for AI challenges we always start as
    the human, color picked at challenge creation. The unused
    `white_id` / `our_username` locals are gone as of alpha26.)
    """
    # Simpler approach: check the initialFen and state.moves together.
    # For an AI game we're always the human. The "white" field for AI challenges
    # is the human when color="white"; we stored the color in player_color.
    if self.player_color == "white":
        self._our_color = chess.WHITE
    elif self.player_color == "black":
        self._our_color = chess.BLACK
    else:
        # "random" — detect from who Lichess assigned as white
        # If the white player has an "aiLevel" key, we're black
        if "aiLevel" in event.get("white", {}):
            self._our_color = chess.BLACK
        else:
            self._our_color = chess.WHITE

    _LOGGER.info(
        "Game %s: we play as %s",
        self._game_id,
        "white" if self._our_color == chess.WHITE else "black",
    )

    # ── Reset learning-dashboard state for the new game ──────────────────
    # The dashboard's rich Lichess view conditional turns on when
    # lichess_active flips True. lichess_review_ready stays off until
    # the game ends (see _on_game_state terminal-status block).
    self._analysis_board = chess.Board()
    white_info = event.get("white") or {}
    black_info = event.get("black") or {}
    self._state["lichess_active"] = True
    self._state["lichess_review_ready"] = False
    self._state["lichess_white_name"] = (
        white_info.get("name") or white_info.get("id")
        or (f"Stockfish level {white_info.get('aiLevel')}"
            if white_info.get("aiLevel") is not None else None)
    )
    self._state["lichess_black_name"] = (
        black_info.get("name") or black_info.get("id")
        or (f"Stockfish level {black_info.get('aiLevel')}"
            if black_info.get("aiLevel") is not None else None)
    )
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
    # Fire opening + initial eval lookup for the starting position.
    self.hass.async_create_task(self._analyze_starting_position())
    # Extract initial clocks if present.
    state = event.get("state", {})
    self._update_clocks_from_event(state)
    self.async_set_updated_data(dict(self._state))

    # Process any moves already in the gameFull state
    if state.get("moves"):
        await self._process_move_list(state["moves"])


async def _on_game_state(self, event: dict[str, Any]) -> None:
    """Incremental game state — contains full move list."""
    status = event.get("status", "started")
    moves_str = event.get("moves", "")
    # Lichess pushes wtime/btime in milliseconds on each gameState.
    self._update_clocks_from_event(event)
    await self._process_move_list(moves_str)

    # Check for terminal states
    terminal = False
    result_str: str | None = None
    if status in ("mate", "checkmate"):
        self._state["game_status"] = STATUS_CHECKMATE
        terminal = True
        # Winner is the side that just moved (the side NOT to move now).
        # Conservative: derive from the board if available.
        try:
            if self._analysis_board.is_checkmate():
                result_str = "0-1" if self._analysis_board.turn == chess.WHITE else "1-0"
            else:
                result_str = "1-0/0-1"
        except Exception:
            result_str = "checkmate"
        self._game_id = None
    elif status in ("stalemate",):
        self._state["game_status"] = STATUS_STALEMATE
        terminal = True
        result_str = "1/2-1/2 (stalemate)"
        self._game_id = None
    elif status in ("draw", "outoftime", "aborted"):
        self._state["game_status"] = STATUS_DRAW
        terminal = True
        result_str = f"1/2-1/2 ({status})"
        self._game_id = None
    elif status == "resign":
        self._state["game_status"] = STATUS_RESIGNED
        terminal = True
        # Lichess sets event.winner when the game ends on resign.
        winner = event.get("winner")
        if winner == "white":
            result_str = "1-0 (resignation)"
        elif winner == "black":
            result_str = "0-1 (resignation)"
        else:
            result_str = "resign"
        self._game_id = None

    if terminal:
        self._state["lichess_active"] = False
        self._state["last_game_result"] = result_str
        # Build the post-game review payload (top 3 mistakes by CPL,
        # filtered to the user's color; plus accuracy for both sides).
        self.hass.async_create_task(self._build_post_game_review())

    self.async_set_updated_data(dict(self._state))


def _on_game_finish(self, event: dict[str, Any]) -> None:
    status = event.get("status", {})
    name = status.get("name", "") if isinstance(status, dict) else str(status)
    _LOGGER.info("Game finished: %s", name)
    self._game_id = None
    # Reinforce terminal flags — defensive in case _on_game_state's
    # terminal branch missed an edge case.
    self._state["lichess_active"] = False
    self.async_set_updated_data(dict(self._state))


def _update_clocks_from_event(self, event: dict[str, Any]) -> None:
    """Pull wtime/btime (ms) from a Lichess event into the clock sensors.

    Both gameFull and gameState events carry wtime/btime. Initial values
    come from gameFull; per-move updates from gameState.
    """
    # Lichess gameState has wtime/btime at the top level; gameFull has
    # them under "state". Handle either shape.
    src = event if "wtime" in event else event.get("state", {})
    wtime = src.get("wtime")
    btime = src.get("btime")
    if isinstance(wtime, (int, float)):
        self._state["lichess_white_clock"] = int(wtime / 1000)
    if isinstance(btime, (int, float)):
        self._state["lichess_black_clock"] = int(btime / 1000)


async def _process_move_list(self, moves_str: str) -> None:
    """Parse the full move list from Lichess and process any new moves."""
    if not moves_str:
        return

    all_moves = moves_str.strip().split()
    new_moves = all_moves[self._processed_moves:]
    if not new_moves:
        return

    for uci in new_moves:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            _LOGGER.warning("Invalid UCI move from Lichess: %s", uci)
            continue

        # Determine whose move this is
        move_color = chess.WHITE if (self._processed_moves % 2 == 0) else chess.BLACK

        # ── Analysis-side bookkeeping ──────────────────────────────────
        # Append a stub entry to move_history_moves and snapshot the board
        # BEFORE the move. The analysis task fills classification/cpl/etc.
        # in-place once cloud-eval returns.
        ply_index = self._record_history_stub(move, move_color)
        board_before_analysis = self._analysis_board.copy(stack=False)
        try:
            if move in self._analysis_board.legal_moves:
                self._analysis_board.push(move)
        except Exception as err:
            _LOGGER.debug("analysis board push failed for %s: %s", uci, err)
        board_after_analysis = self._analysis_board.copy(stack=False)
        # Fire the async eval + classification. Doesn't block the main
        # move-processing path; updates state when it returns.
        self.hass.async_create_task(
            self._analyze_move(
                ply_index,
                board_before_analysis,
                board_after_analysis,
                move,
                move_color == chess.WHITE,
                session_board=self._board,
            ),
            name=f"{DOMAIN}_analyze_ply_{ply_index}",
        )

        if move not in self._board.legal_moves:
            # Most common cause: the move was already pushed onto
            # self._board by the discovery callback (the human's own
            # move). The Lichess stream echoes all moves including
            # ours; we mustn't re-push. But we MUST advance
            # _processed_moves so the next move's color attribution
            # (which uses processed_moves % 2) stays correct.
            _LOGGER.debug(
                "Skipping already-pushed/illegal move %s on board %s",
                uci, self._board.fen(),
            )
            self._processed_moves += 1
            continue

        if move_color != self._our_color:
            # AI (Lichess Stockfish) move — route through the proven
            # async_phantom_apply_ai_move which uses the triplet
            # (movementVerify + side + opcode 2 MOVEMENT) on cc68a66e.
            # That method handles BOTH the BLE writes AND the
            # self._board.push, so we don't push here.
            # Uses the firmware-0.3.0-only path; the old 0.1.6 probe
            # method was removed in the 2026-05-17 cleanup.
            try:
                await self.async_phantom_apply_ai_move(uci)
            except Exception as err:
                _LOGGER.error("Failed to apply Lichess AI move %s: %s", uci, err)
                # Fall back to local push so HA stays in sync even if
                # the magnet didn't fire.
                if move in self._board.legal_moves:
                    self._board.push(move)
        else:
            # Our color's move (mirrored back from Lichess after we POST'd
            # it). The discovery callback may have already pushed via the
            # human-move path; push here as a safety net if not.
            if move in self._board.legal_moves:
                self._board.push(move)

        self._processed_moves += 1
        self._state["last_move"] = uci

        # If it's now our turn, drain the physical-move queue
        # (in case the player moved while we were processing)
        if self._board.turn == self._our_color:
            await self._drain_physical_move_queue()

    # Update game status
    if self._board.is_checkmate():
        self._state["game_status"] = STATUS_CHECKMATE
    elif self._board.is_stalemate():
        self._state["game_status"] = STATUS_STALEMATE
    elif self._board.is_check():
        self._state["game_status"] = "check"
    else:
        self._state["game_status"] = STATUS_PLAYING

    self.async_set_updated_data(dict(self._state))


async def _drain_physical_move_queue(self) -> None:
    """Send any queued physical moves to the active game backend.

    Post-2026-05-14 audit fix: the queue now holds already-resolved UCI
    strings (not raw firmware payload), so no re-parsing via
    _phantom_to_uci. The discovery callback's legality check has already
    disambiguated rotation and rejected illegal moves before queuing.
    """
    while not self._physical_move_queue.empty():
        uci = self._physical_move_queue.get_nowait()
        if not uci or len(uci) < 4:
            _LOGGER.warning("Drain: skipping malformed queue entry %r", uci)
            continue
        if self._local_game_active:
            await self._push_move_to_local_ai(uci)
        else:
            await self._push_move_to_lichess(uci)


async def _push_move_to_lichess(self, uci: str) -> None:
    """POST a move to the Lichess Board API."""
    if not self._game_id:
        _LOGGER.debug("No active game; discarding move %s", uci)
        return

    session = rt.async_get_clientsession(self.hass)
    url = LICHESS_MOVE_URL.format(game_id=self._game_id, move=uci)
    async with session.post(
        url,
        headers={"Authorization": f"Bearer {self._lichess_token}"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status == 200:
            _LOGGER.debug("Move %s accepted by Lichess", uci)
        else:
            text = await resp.text()
            _LOGGER.error("Lichess rejected move %s: %s — %s", uci, resp.status, text)

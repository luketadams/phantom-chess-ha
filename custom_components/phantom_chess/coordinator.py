"""DataUpdateCoordinator for Phantom Chess Board — BLE ↔ Lichess/local-AI bridge."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .lichess_analysis import LichessAnalysisClient

import aiohttp
import chess

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.issue_registry import async_delete_issue
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    BLE_MAX_RETRY_SECONDS,
    BLE_RETRY_SECONDS,
    CONF_BLE_ADDRESS,
    CONF_LICHESS_TOKEN,
    DOMAIN,
    LICHESS_CHALLENGE_AI_URL,
    LICHESS_GAME_EXPORT_URL,
    LICHESS_GAME_STREAM_URL,
    LICHESS_MOVE_URL,
    LICHESS_RESIGN_URL,
    LICHESS_RETRY_SECONDS,
    MODE_CHESS_PLAY,
    MOVE_PREFIX,
    STATUS_CHECKMATE,
    STATUS_DRAW,
    STATUS_IDLE,
    STATUS_PAUSED,
    STATUS_PLAYING,
    STATUS_RESIGNED,
    STATUS_STALEMATE,
    UUID_BATTERY_INFO,
    UUID_ERROR_MSG,
    UUID_FIRMWARE_STATE,
    UUID_GAME,
    UUID_RECEIVE_MOVEMENT,
    UUID_SCULPTURE,
    UUID_SELECT_MODE,
    UUID_SEND_MATRIX,
    UUID_SOUND_LEVEL,
    UUID_STATUS_BOARD,
    UUID_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Lazy import bleak — HA installs it as part of the bluetooth stack
try:
    from bleak import BleakClient, BleakError
    from bleak.backends.characteristic import BleakGATTCharacteristic
except ImportError:
    BleakClient = None  # type: ignore[assignment,misc]
    BleakError = Exception  # type: ignore[assignment,misc]


def _phantom_to_uci(move_str: str) -> str:
    """Convert Phantom move notation to UCI.

    "M 1 e2-e4" → "e2e4"        (firmware 0.1.6 / 0.3.0 white)
    "M 2 e7-e5" → "e7e5"        (firmware 0.3.0 black)
    "M 1 d5xe4" → "d5e4"        (capture)
    "M 1 e1-g1" → "e1g1"        (castling — king target square is enough)
    "M e2-e4"   → "e2e4"        (older firmware variant without index)
    """
    # Robust parser: extract the first '<sq>[-x]<sq>' token from the string.
    # This avoids relying on an exact "M N " prefix and tolerates both indexed
    # ("M 1 e2-e4") and unindexed ("M e2-e4") variants.
    m = re.search(r"([a-h][1-8])[-x]([a-h][1-8])", move_str)
    if m:
        return m.group(1) + m.group(2)
    # Fallback to legacy behaviour
    stripped = move_str.removeprefix(MOVE_PREFIX)
    return re.sub(r"[-x]", "", stripped)


def _rotate_uci_180(uci: str) -> str:
    """Apply a 180° rotation (rank-mirror + from-to swap) to a UCI move.

    Firmware 0.3.0 reports black-piece sensor events with this exact transform
    applied. To recover the actual move from the firmware's report, apply the
    same transform again — the operation is an involution.

      'e7e5' (actual black move) → 'e2e4' (rank-mirror) → 'e4e2' (from-to-swap)
                                                         ^ firmware emits this
      'e4e2' (decode firmware → actual) → 'e5e7' → 'e7e5'

    Validated 2026-05-10:
      - Luke played e7→e5 physically; firmware emitted "M 1 e4-e2".
      - rotate_180("e4e2") == "e7e5" ✓
      - White moves are reported without the transform.

    Used by the discovery-callback path to pick the right interpretation by
    legality-checking both candidates against the current python-chess board.
    """
    if len(uci) < 4:
        return uci
    f_file, f_rank, t_file, t_rank = uci[0], uci[1], uci[2], uci[3]
    promotion = uci[4:] if len(uci) > 4 else ""
    try:
        f_rank_m = str(9 - int(f_rank))
        t_rank_m = str(9 - int(t_rank))
    except ValueError:
        return uci
    # Mirror ranks and swap from-to in one shot.
    return f"{t_file}{t_rank_m}{f_file}{f_rank_m}{promotion}"


# ── Matrix-state notification parsing (UUID_SEND_MATRIX, firmware 0.3.0) ──────
# The board emits notifications on 1b034927 in the form:
#   "CLEAN: Match.,<100-char piece grid>,<100-char binary bitmap>"
# - Piece grid: 10×10 row-major, '.' = empty, uppercase = white piece
#   (P/N/B/R/Q/K), lowercase = black. Rows 0 and 9 are gutter rows for
#   captured pieces; rows 1–8 are the playing area; cols 0 and 9 are
#   borders, cols 1–8 are files a–h.
# - Bitmap: 10×10 of '0'/'1' representing raw hall-effect sensor state.
# Source: live capture 2026-05-09 + setupBoard asm.

# Matrix parsing, FEN conversion, and mismatch diff helpers extracted to
# `matrix.py` as the first step of the Task #21 coordinator split
# (2026-05-16). These functions are pure (stateless) — they never needed
# to be on the coordinator class. Imported with underscore aliases to
# preserve every existing call-site verbatim; no behavior change.
from .matrix import (  # noqa: E402 — intentional late import, kept beside the extraction-doc comment above
    build_matrix_from_fen as _build_matrix_from_fen_module,
    check_consistency as _check_consistency,
    diff_grid_vs_sensor as _diff_grid_vs_sensor,
    format_mismatch_instructions as _format_mismatch_instructions,
    grid_to_fen as _grid_to_fen,
    parse_matrix_notification as _parse_matrix_notification,
)


class PhantomChessCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages BLE connection to the Phantom board and the Lichess Board API game."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: dict[str, Any],
        entry: "ConfigEntry | None" = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            # We push updates via async_set_updated_data; polling is just a safety net.
            update_interval=timedelta(seconds=30),
        )
        # Hold a reference to the ConfigEntry so we can read entry.options
        # at runtime (TTS overrides, debug-dump toggle, etc.). Optional
        # for backwards compatibility — older callers pass only entry_data.
        # Added 2026-05-16 (Task #16 release-readiness).
        self._entry = entry
        self._ble_address: str = entry_data[CONF_BLE_ADDRESS].upper()
        self._lichess_token: str = entry_data[CONF_LICHESS_TOKEN]

        # BLE
        self._ble_client: BleakClient | None = None
        self._ble_task: asyncio.Task | None = None
        self._matrix_poll_task: asyncio.Task | None = None
        self._ble_connected = False

        # Lichess
        self._lichess_task: asyncio.Task | None = None
        self._game_id: str | None = None
        self._our_color: chess.Color | None = None  # chess.WHITE or chess.BLACK
        self._processed_moves: int = 0  # how many UCI moves we've already handled

        # python-chess board state
        self._board = chess.Board()

        # Analysis-side board, advanced in lockstep with Lichess's authoritative
        # move list. Separate from self._board because that one is mutated by
        # the discovery callback (human moves arrive via BLE before the
        # Lichess stream confirms them), making it unreliable for board-before
        # snapshots during eval. Reset on each new Lichess game.
        self._analysis_board: chess.Board = chess.Board()
        # Lazily initialized; needs the HA event loop.
        # Quoted forward-ref so mypy doesn't require importing the
        # heavy LichessAnalysisClient at module top.
        self._analysis_client: "LichessAnalysisClient | None" = None  # set in async_setup

        # Game settings (mutated by HA services / select entities)
        self.ai_level: int = 3
        self.player_color: str = "random"  # "white" | "black" | "random"
        # Integration-owned mode + sculpture pickers (v0.4-alpha1, Option C
        # step 1). Replaces the input_select.phantom_chess_setup_mode /
        # input_select.phantom_chess_sculpture_game helpers that v0.3
        # required users to create by hand. The select-platform entities in
        # select.py read/write these fields. Defaults match v0.3's helpers
        # so v0.3→v0.4 dashboards see the same initial state.
        from .const import (
            DEFAULT_SETUP_MODE,
            DEFAULT_SCULPTURE_GAME,
            DEFAULT_TRAINING_WHEELS,
            DEFAULT_LICHESS_CLOCK_MINUTES,
            DEFAULT_LICHESS_CLOCK_INCREMENT,
        )
        self.setup_mode: str = DEFAULT_SETUP_MODE
        self.selected_sculpture: str = DEFAULT_SCULPTURE_GAME
        # v0.4-alpha2: training-wheels toggle + Lichess clock controls.
        # Replace input_boolean.phantom_chess_training_wheels and the two
        # input_number.phantom_chess_lichess_clock_* helpers from v0.3.
        # The dashboard's `phantom_start_lichess_configured` script
        # historically read the helpers and passed minutes*60 +
        # increment_seconds to `phantom_chess.start_game`. With these
        # fields owned by the integration, that script becomes a thin
        # wrapper (or can be replaced by a service call that reads them
        # directly — see start_game_configured in services.yaml once
        # added).
        self.training_wheels: bool = DEFAULT_TRAINING_WHEELS
        self.lichess_clock_minutes: int = DEFAULT_LICHESS_CLOCK_MINUTES
        self.lichess_clock_increment: int = DEFAULT_LICHESS_CLOCK_INCREMENT
        # v0.4-alpha30: persistent AI-vs-AI spectator-mode config. The
        # dashboard's 5th mode tile reads these via three RestoreEntity
        # number entities (white_ai_level, black_ai_level,
        # ai_vs_ai_move_delay). The service uses these as defaults when
        # called without explicit args. Defaults match the previous
        # in-method defaults (level 3 = mid, 1.5s = readable pacing).
        self.white_ai_level: int = 3
        self.black_ai_level: int = 3
        self.ai_vs_ai_move_delay: float = 1.5
        # Mechanism speed on the firmware-native 1..5 scale (3 = NORMAL).
        # Was 50 when the entity exposed 0..100; corrected to the actual
        # firmware range 2026-05-13. See XOUXOU_PROTOCOL.md.
        self.mechanism_speed: int = 3
        self.sound_level: int = 16  # 0-32 range; 16 = ~50% volume
        self.paused: bool = False

        # State exposed to entities
        self._state: dict[str, Any] = self._blank_state()

        # Queues / events
        self._physical_move_queue: asyncio.Queue[str] = asyncio.Queue()
        self._stop_event = asyncio.Event()

        # Local AI mode (no Lichess required)
        self._local_game_active: bool = False
        self._local_game_task: asyncio.Task | None = None
        # AI-vs-AI mode: Stockfish plays both sides via the same snapshot
        # protocol used for normal AI moves. Useful for autonomous testing
        # of the protocol (especially castle handling) and as a "watch
        # the AI play itself" demo. Activated by
        # `async_start_ai_vs_ai_game`; the loop checks this flag every
        # iteration so `async_stop_local_game` can halt it cleanly.
        self._ai_vs_ai_active: bool = False
        self._ai_vs_ai_white_level: int = 3
        self._ai_vs_ai_black_level: int = 3
        self._ai_vs_ai_move_delay: float = 1.5
        # Serializes _local_game_task replacement so the four-or-more
        # sites that schedule an AI turn can't race and end up running
        # two AI turns concurrently. All assignments to
        # self._local_game_task MUST go through _replace_local_game_task
        # (audit §1.4, 2026-05-19). The lock is created lazily on first
        # use because asyncio.Lock() needs a running loop, which isn't
        # guaranteed in __init__ depending on how HA constructs the
        # coordinator (esp. during reloads).
        self._local_game_task_lock: asyncio.Lock | None = None

        # Suppress echo notifications from outbound AI moves. Set when an AI
        # move is issued (monotonic-now + ~2s); the discovery callback skips
        # human-move parsing for notifications received within this window.
        # Without this, the firmware's '\x03M ...' move-completion echo gets
        # interpreted as a fresh human move and fails legality (graceful but
        # noisy). See HANDOFF.md §2.
        # Legacy time-based echo suppression. Retained as a 5-second hard
        # safety net but no longer the primary mechanism — see _last_ai_uci.
        self._expecting_ai_echo_until: float = 0.0
        # Post-activation move-detection suppression window. Set by
        # `_phantom_execute_position` to `loop.time() + N` immediately
        # before any GAME_START write. While `loop.time() < value`, the
        # discovery callback's human-move branch treats incoming
        # `\x03M ...` notifications as magnet-settle/sensor-recalibration
        # echoes rather than human moves — this prevents the `M 1 e8-g8`
        # class of spurious detection that fires AFTER firmware transitions
        # to "Board Playing" but BEFORE the magnet has fully settled and
        # before firmware enters "Setting Up" (the existing _reset_modes
        # filter only catches the latter window). The window is cleared
        # early to 0 by the BLE_MOVE_DONE (opcode 0x0c) handler — that's
        # the firmware's authoritative "magnet sequence complete" signal,
        # stronger than CLEAN: Match (which can be followed by additional
        # sensor recalibration events for a few more seconds). Hard
        # timeout 600s as a safety net: if BLE_MOVE_DONE never arrives
        # the user shouldn't be locked out indefinitely. Bug + fix:
        # 2026-05-25 (longer post-CLEAN-Match settle window than
        # initially estimated).
        self._activation_settle_until: float = 0.0
        # Content-based AI-echo detection. When the integration drives an AI
        # move via snapshot, the firmware emits a sensor-derived `\\x03M ...`
        # notification reflecting the magnet's motion. That echo must NOT be
        # treated as a fresh human move. We track the AI's most-recent move
        # UCI and its 180° rotation (for black-piece events) and suppress
        # discovery notifications whose payload matches either, within a
        # generous time window (covers magnet motion + sensor settling).
        # This replaces the earlier purely-time-based suppression that
        # incorrectly assumed the board had its own AI making decisions —
        # it doesn't. Every \\x03M from the firmware is either an echo of
        # our snapshot (suppress) or a real human move (process).
        self._last_ai_uci: str | None = None
        self._last_ai_uci_rotated: str | None = None
        self._last_ai_uci_set_at: float = 0.0
        # Set of UCIs the firmware may emit `\x03M` notifications for as
        # the magnet executes the most-recent AI move. Always includes
        # the primary UCI plus its 180°-rotated form. For castling, ALSO
        # includes the rook's UCI (and rotated). Populated by
        # `_set_last_ai_move` when given the pre-move board; checked by
        # `_is_ai_echo` on every incoming move-format notification.
        # Castle-related entries are the critical addition (2026-05-25)
        # — without them the rook's sensor event was treated as a phantom
        # human move and the unconditional movementVerify ack confused
        # the firmware, causing the board to stop responding on the move
        # AFTER a castle.
        self._last_ai_echo_ucis: set[str] = set()

        # ── Snapshot move protocol state (validated 2026-05-13) ────────────────
        # On firmware 0.3.0, the first GAME_START in a BLE session is treated as
        # a state-initialization (silent transition to BLE Playing — no motor).
        # We must drop firmware to HOME via GAME_END before the first move.
        # Subsequent moves within the same BLE Playing session actuate normally.
        # Reset on BLE disconnect (see _on_ble_disconnect).
        self._phantom_session_initialized: bool = False
        # Future resolved by the discovery callback when a BLE_MOVE_DONE
        # (opcode 0x0C) notification arrives on cc68a66e. Created by
        # _phantom_execute_position before each snapshot write; awaited with
        # a timeout so callers know when the magnet has finished moving.
        self._move_done_future: asyncio.Future | None = None
        # Last target FEN sent over BLE. The firmware's CLEAN: Match notification
        # doesn't echo the matrix back, so the parser uses this as the
        # authoritative state on a clean match. See XOUXOU_PROTOCOL.md.
        self._last_target_fen: str | None = None
        # Signature of the most-recent sensor mismatch set we surfaced as a
        # persistent_notification (Task #8). None if no current mismatch.
        # See _update_mismatch_notification for the signature scheme.
        self._last_mismatch_signature: tuple | None = None
        # Set of characteristic UUIDs (lowercase) that GATT discovery
        # actually returned on this BLE connection. Used by `_ble_write`
        # to distinguish "stale cache, force reconnect" (UUID was here
        # but is now broken) from "never existed, just fail" (UUID never
        # showed up — speculative write to a wrong/older firmware UUID).
        # Refreshed on every successful BLE reconnect.
        # Added 2026-05-17 after a false-positive force-reconnect aborted
        # Lichess game activation when writing the legacy UUID_MATRIX_INIT_GAME.
        self._discovered_uuids: set[str] = set()

    # ── Public state helpers ──────────────────────────────────────────────────

    @property
    def is_ble_connected(self) -> bool:
        return self._ble_connected

    @property
    def state(self) -> dict[str, Any]:
        return self._state

    def _handle_matrix_bytes(self, data: bytes) -> None:
        """Parse a UUID_SEND_MATRIX value and update coordinator state.

        Called from both the notify callback and the periodic poll loop.
        Accepts both 'CLEAN: Match' and 'ERROR: <reason>' message shapes.

        When entry.options.debug_dump is enabled, writes EVERY matrix
        payload (regardless of prefix) to <config>/phantom_chess/debug/
        matrix_log.txt for analysis. Default off in production.
        """
        # Capture EVERY matrix payload to disk for analysis when debug
        # dumps are enabled. File rolls past 1MB to bound size.
        if self._debug_dump_enabled():
            try:
                from datetime import datetime, timezone
                import os
                log_path = self._debug_path("matrix_log.txt")
                try:
                    if os.path.exists(log_path) and os.path.getsize(log_path) > 1_000_000:
                        os.replace(log_path, log_path + ".old")
                except Exception:
                    pass
                try:
                    decoded = data.decode("utf-8", errors="replace").strip()
                except Exception:
                    decoded = "(binary)"
                line = f"{datetime.now(timezone.utc).isoformat()} | hex={data.hex()} | str={decoded!r}\n"
                self.hass.async_add_executor_job(self._append_matrix_log, line)
            except Exception:
                pass

        parsed = _parse_matrix_notification(data)
        if parsed is None:
            return
        # Marshal the rest onto the event loop so the self._state read
        # (dedup check), mutation, and fanout all happen on the same
        # thread. The original code ran this block on whichever thread
        # invoked _handle_matrix_bytes — for the notify callback path
        # that's NOT the loop thread, and the mutation could race
        # against loop-thread reads (entity property gets, analysis
        # tasks, dashboard JSON serialization). Audit §1.5, 2026-05-19.
        self.hass.loop.call_soon_threadsafe(self._apply_matrix_state, parsed)

    def _apply_matrix_state(self, parsed: dict[str, Any]) -> None:
        """Apply a parsed matrix payload to coordinator state. Runs on the
        event loop thread; do NOT call directly from a notify callback —
        go through _handle_matrix_bytes instead.
        """
        # Dedup check (now on loop thread, no read race).
        if (self._state.get("piece_grid") == parsed["piece_grid"]
                and self._state.get("sensor_bitmap") == parsed["sensor_bitmap"]
                and self._state.get("matrix_status") == parsed["status"]
                and self._state.get("matrix_status_message") == parsed["status_message"]):
            return
        from datetime import datetime, timezone
        fen_board = _grid_to_fen(parsed["piece_grid"])
        consistent, mismatches = _check_consistency(
            parsed["piece_grid"], parsed["sensor_bitmap"]
        )
        # Count actual pieces (non-dot characters in the 100-char grid).
        piece_count = sum(1 for c in parsed["piece_grid"] if c != ".")
        self._state["matrix_raw"] = parsed["raw"]
        self._state["piece_grid"] = parsed["piece_grid"]
        self._state["sensor_bitmap"] = parsed["sensor_bitmap"]
        self._state["live_fen"] = fen_board
        self._state["matrix_last_updated"] = datetime.now(timezone.utc).isoformat()
        self._state["position_consistent"] = consistent
        self._state["matrix_mismatches"] = mismatches
        self._state["piece_count"] = piece_count
        self._state["matrix_status"] = parsed["status"]  # "Clean" or "Error"
        self._state["matrix_status_message"] = parsed["status_message"]
        self.async_set_updated_data(dict(self._state))

        # Surface sensor-matrix mismatches as user-facing notifications
        # (Task #8, 2026-05-16). The firmware fires lots of ERROR_MSG
        # events while autocorrecting — we only update the notification
        # when the SET of disagreement squares changes, so it doesn't spam.
        # When consistency returns (CLEAN: Match), we dismiss the notification.
        self._update_mismatch_notification(
            parsed["piece_grid"], parsed["sensor_bitmap"], consistent,
        )

    def _update_mismatch_notification(
        self, piece_grid: str, sensor_bitmap: str, consistent: bool
    ) -> None:
        """Create/update/dismiss the persistent_notification that tells the
        user which pieces need adjusting. Runs on the event loop thread.

        Spam suppression: tracks a signature of the current mismatch set
        and only fires the create/update service call when the signature
        changes. On consistency restored, dismisses any existing notification.
        """
        NOTIF_ID = "phantom_chess_sensor_mismatch"

        if consistent:
            # State restored — dismiss any existing notification.
            if self._last_mismatch_signature is not None:
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "persistent_notification", "dismiss",
                        {"notification_id": NOTIF_ID},
                    )
                )
                self._last_mismatch_signature = None
            return

        # Build the diff signature for change detection.
        diffs = _diff_grid_vs_sensor(piece_grid, sensor_bitmap)
        # Signature: sorted list of (square, type) tuples — same disagreement
        # set always produces the same signature regardless of dict order.
        signature = tuple(sorted(
            (d["square"], d["type"]) for d in diffs
        ))
        if signature == self._last_mismatch_signature:
            return  # same disagreement set as last time — don't re-fire

        self._last_mismatch_signature = signature

        instructions = _format_mismatch_instructions(diffs)
        msg = (
            "The physical board doesn't match the expected position. "
            "Please adjust the pieces below to continue:\n\n"
            f"{instructions}\n\n"
            "*This notification clears automatically once the sensors detect "
            "the corrected position.*"
        )
        self.hass.async_create_task(
            self.hass.services.async_call(
                "persistent_notification", "create",
                {
                    "title": "Phantom Chess: Piece position mismatch",
                    "message": msg,
                    "notification_id": NOTIF_ID,
                },
            )
        )

    def _handle_firmware_mode_bytes(self, data: bytes) -> None:
        """Parse a UUID_FIRMWARE_STATE value.

        This channel is multi-purpose:
          - Mode strings: 'Running', 'Paused', 'HOME', 'Snapping Pieces'
          - Move events:  '<P> <from>-<to>' format (e.g. 'K e1-a4', 'p a7-g6')
        We route move-format strings into a separate state field so the
        firmware_mode sensor stays useful for actual mode tracking.
        """
        try:
            text = data.decode("utf-8", errors="replace").strip()
        except Exception:
            return
        if not text:
            return
        # Marshal state mutation onto the loop. See _handle_matrix_bytes
        # for the same pattern + rationale (audit §1.5, 2026-05-19).
        self.hass.loop.call_soon_threadsafe(self._apply_firmware_mode_state, text)

    def _apply_firmware_mode_state(self, text: str) -> None:
        """Apply firmware-mode-channel text payload to coordinator state.
        MUST run on the event loop thread.
        """
        from datetime import datetime, timezone
        import re

        # Match move format: "<piece-letter> <from-square>-<to-square>" with
        # optional capture char (x). Piece letter is a single chess letter.
        move_pattern = re.compile(
            r"^[PNBRQKpnbrqk]\s+[a-h][1-8][-x][a-h][1-8]$"
        )
        if move_pattern.match(text):
            if self._state.get("firmware_last_move") == text:
                return
            self._state["firmware_last_move"] = text
            self._state["firmware_last_move_updated"] = datetime.now(timezone.utc).isoformat()
            self.async_set_updated_data(dict(self._state))
            return

        # Otherwise treat as mode label.
        if self._state.get("firmware_mode") == text:
            return
        self._state["firmware_mode"] = text
        self._state["firmware_mode_last_updated"] = datetime.now(timezone.utc).isoformat()
        self.async_set_updated_data(dict(self._state))

    async def _matrix_poll_loop(self) -> None:
        """Poll UUID_SEND_MATRIX and UUID_FIRMWARE_STATE every 2 seconds.

        Firmware 0.3.0 emits matrix notifications only when the firmware is
        in an active mode (sculpture playback, chess play, recording).
        When idle (Paused), notifications stop and the cached read value
        also doesn't update. The poll keeps state at most 2s stale during
        active modes; while idle, the dashboard shows the last-known state.
        """
        while not self._stop_event.is_set():
            try:
                client = self._ble_client
                if client is not None and client.is_connected:
                    try:
                        data = await client.read_gatt_char(UUID_SEND_MATRIX)
                        self._handle_matrix_bytes(bytes(data))
                    except Exception as err:
                        _LOGGER.debug("matrix poll read failed: %s", err)
                    try:
                        data = await client.read_gatt_char(UUID_FIRMWARE_STATE)
                        self._handle_firmware_mode_bytes(bytes(data))
                    except Exception as err:
                        _LOGGER.debug("firmware_mode poll read failed: %s", err)
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                return
            except Exception as err:
                _LOGGER.debug("matrix_poll_loop error: %s", err)
                await asyncio.sleep(2)

    def _blank_state(self) -> dict[str, Any]:
        # Seed live_fen + piece_grid from the starting position so the dashboard
        # has something to render before the first physical move arrives.
        try:
            _seed_board = chess.Board()
            _seed_live_fen = _seed_board.board_fen()
            _seed_grid = self._build_phantom_matrix_from_fen(_seed_board.fen())
            _seed_piece_count = sum(1 for c in _seed_grid if c != ".")
        except Exception:
            _seed_live_fen = None
            _seed_grid = None
            _seed_piece_count = None
        return {
            "last_move": None,
            "battery_percent": None,
            "battery_charging": False,
            "game_status": STATUS_IDLE,
            "lichess_game_id": None,
            "firmware_version": None,
            # Live matrix-state, populated from UUID_SEND_MATRIX notifications
            # (or from human-move detection in 0.3.0 — see discovery callback).
            "live_fen": _seed_live_fen,
            "piece_grid": _seed_grid,
            "sensor_bitmap": None,
            "matrix_raw": None,
            "matrix_last_updated": None,
            "position_consistent": None,
            "matrix_mismatches": None,
            "piece_count": _seed_piece_count,
            "firmware_mode": None,
            "firmware_mode_last_updated": None,
            "matrix_status": None,
            "matrix_status_message": None,
            "firmware_last_move": None,
            "firmware_last_move_updated": None,
            # ── Learning-dashboard state (added 2026-05-14) ─────────────────
            # Populated by Lichess analysis pipeline (cloud-eval + classifier).
            # See phantom_chess_research/IN_GAME_DASHBOARD_SPEC_2026-05-14.md.
            "lichess_active": False,
            "lichess_review_ready": False,
            # Mirror of self._local_game_active for the binary sensor +
            # dashboard. Set True by async_start_local_game, False by
            # game-end or async_stop_local_game. (Task #9, 2026-05-16)
            "local_game_active": False,
            "lichess_white_name": None,
            "lichess_black_name": None,
            "lichess_white_clock": None,
            "lichess_black_clock": None,
            "opening_name": None,
            "opening_eco": None,
            "eval_cp": None,
            "eval_mate": None,
            "eval_source": None,
            "eval_depth": None,
            "best_move_san": None,
            "last_move_classification": None,
            "last_move_cpl": None,
            "last_move_motif": None,
            "threat_san": None,
            "move_history_moves": [],
            "last_game_result": None,
            "last_game_accuracy_white": None,
            "last_game_accuracy_black": None,
            "last_game_top_mistakes": [],
            # Caches for the analysis pipeline (not exposed as sensors)
            "_eval_pre_move": None,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Start background tasks — called from __init__.async_setup_entry."""
        self._stop_event.clear()
        # Lichess analysis client (cloud-eval + opening explorer +
        # local Stockfish fallback). Created here because the client
        # acquires the HA aiohttp session lazily; instantiating it during
        # __init__ is fine but its first call needs the event loop.
        # Stockfish binary is cached in /config/phantom_chess/bin/ so it
        # survives integration reloads. First evaluate() call after a fresh
        # install triggers a ~3 MB one-time download.
        from pathlib import Path
        from .lichess_analysis import LichessAnalysisClient
        sf_bin_dir = Path(self.hass.config.path("phantom_chess")) / "bin"
        self._analysis_client = LichessAnalysisClient(
            self.hass, stockfish_bin_dir=sf_bin_dir
        )
        self._ble_task = self.hass.loop.create_task(
            self._ble_loop(), name=f"{DOMAIN}_ble"
        )

    async def async_shutdown(self) -> None:
        """Stop background tasks — called from __init__.async_unload_entry."""
        self._stop_event.set()
        for task in (self._ble_task, self._lichess_task, self._matrix_poll_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._ble_client:
            try:
                await self._ble_client.disconnect()
            except Exception:
                pass
        # Release Stockfish process (idempotent; no-op if never spawned).
        if self._analysis_client is not None:
            try:
                await self._analysis_client.shutdown()
            except Exception:
                pass

    async def _async_update_data(self) -> dict[str, Any]:
        """Called by the coordinator on its polling interval — return cached state."""
        return self._state

    # ── BLE loop ──────────────────────────────────────────────────────────────

    async def _ble_loop(self) -> None:
        """Maintain BLE connection with automatic reconnection.

        Silver quality scale rule `log-when-unavailable`: log once when
        the board becomes unreachable, once when it comes back. We
        achieve that by:

        - Only logging the "lost connection" WARNING on the FIRST retry
          of a cluster (`retry_delay == BLE_RETRY_SECONDS`). The
          `_on_ble_disconnect` callback already emits a one-off
          "Phantom board disconnected" WARNING for the disconnect event
          itself.
        - Letting `_ble_connect_and_run` log the "Connected" INFO line
          when the board is reachable again — that's the "back
          connected" half of the rule.
        - Subsequent retry attempts during the same outage stay
          DEBUG-level so logs don't fill up during long board-off
          periods.
        """
        retry_delay = BLE_RETRY_SECONDS
        first_failure_of_cluster = True
        while not self._stop_event.is_set():
            try:
                await self._ble_connect_and_run()
                retry_delay = BLE_RETRY_SECONDS  # reset on clean run
                first_failure_of_cluster = True
            except asyncio.CancelledError:
                return
            except Exception as err:
                if first_failure_of_cluster:
                    _LOGGER.warning(
                        "BLE connection lost (%s), retrying in %ds",
                        err,
                        retry_delay,
                    )
                    first_failure_of_cluster = False
                else:
                    _LOGGER.debug(
                        "BLE reconnect attempt failed (%s), next retry in %ds",
                        err,
                        retry_delay,
                    )
                self._ble_connected = False
                self.async_set_updated_data(self._state)

            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, BLE_MAX_RETRY_SECONDS)

    async def _ble_connect_and_run(self) -> None:
        """Connect to board, subscribe to notifications, and process events."""
        device = async_ble_device_from_address(
            self.hass, self._ble_address, connectable=True
        )
        if device is None:
            raise BleakError(f"Device {self._ble_address} not found in BT scanner cache")

        async with BleakClient(device, disconnected_callback=self._on_ble_disconnect) as client:
            self._ble_client = client
            self._ble_connected = True
            _LOGGER.info("Connected to Phantom board at %s", self._ble_address)

            # Dump GATT layout to a file when debug_dump is enabled — useful
            # for diagnosing protocol differences across firmware versions.
            # No-op in production. Path: <config>/phantom_chess/debug/gatt.txt
            if self._debug_dump_enabled():
                try:
                    lines = ["=== Phantom GATT services ===\n"]
                    for service in client.services:
                        lines.append(f"Service: {service.uuid}\n")
                        for char in service.characteristics:
                            lines.append(f"  Char: {char.uuid}  props: {char.properties}\n")
                    await self.hass.async_add_executor_job(self._write_gatt_dump, lines)
                    _LOGGER.debug("GATT dump written to %s", self._debug_path("gatt.txt"))
                except Exception as dump_err:
                    _LOGGER.debug("Could not write GATT dump: %s", dump_err)

            # Build a set of available characteristic UUIDs for safe subscription
            available_uuids = {
                char.uuid.lower()
                for service in client.services
                for char in service.characteristics
            }
            # Snapshot the discovery for `_ble_write`'s staleness recovery —
            # so it knows the difference between "we saw this UUID earlier
            # and the cached handle went stale" (force reconnect) vs
            # "this UUID was never here, somebody's writing to the wrong
            # characteristic" (just fail). 2026-05-17.
            self._discovered_uuids = set(available_uuids)

            # Read firmware version
            if UUID_VERSION.lower() in available_uuids:
                try:
                    ver_bytes = await client.read_gatt_char(UUID_VERSION)
                    self._state["firmware_version"] = ver_bytes.decode("utf-8", errors="replace").strip()
                    _LOGGER.info("Firmware version: %s", self._state["firmware_version"])
                except Exception:
                    pass

            # Clean up the legacy firmware_too_old Repairs issue if it exists.
            # The original check assumed firmware ≥ 0.1.6 would expose
            # UUID_STATUS_BOARD and UUID_RECEIVE_MOVEMENT. Firmware 0.3.0
            # (current) dropped those in favor of the UUID_GAME opcode
            # channel and UUID_SEND_MATRIX, which the integration uses
            # instead — the check was a false-positive on all current
            # boards. Removed 2026-05-16; cleanup retained to clear
            # stale issues.
            async_delete_issue(self.hass, DOMAIN, "firmware_too_old")

            # Subscribe to known characteristics. UUID_SEND_MATRIX and
            # UUID_FIRMWARE_STATE were previously left to bleak's service
            # iteration in the DISCOVERY block, but that iteration returned
            # different subsets across sessions (BLE service-cache quirk),
            # leaving the matrix channel un-subscribed in some restarts.
            # Always subscribe explicitly here.
            for uuid, callback, label in [
                (UUID_STATUS_BOARD, self._on_physical_move, "STATUS_BOARD"),
                (UUID_BATTERY_INFO, self._on_battery, "BATTERY_INFO"),
                (UUID_ERROR_MSG, self._on_error_msg, "ERROR_MSG"),
                (UUID_SEND_MATRIX, self._on_matrix_notify, "SEND_MATRIX"),
                (UUID_FIRMWARE_STATE, self._on_firmware_mode_notify, "FIRMWARE_STATE"),
            ]:
                if uuid.lower() in available_uuids:
                    try:
                        await client.start_notify(uuid, callback)
                        _LOGGER.debug("Subscribed to %s (%s)", label, uuid)
                    except Exception as sub_err:
                        _LOGGER.warning("Subscribe %s (%s) failed: %s", label, uuid, sub_err)
                else:
                    _LOGGER.info("Characteristic %s not present on this firmware — skipping", label)

            # ── DISCOVERY MODE ────────────────────────────────────────────────
            # Read all readable characteristics and write to file (avoids log deduplication).
            _SKIP_READ = {
                "93601602-bbc2-4e53-95bd-a3ba326bc04b",  # OTA
                "b583ff00-b77a-42f5-a53f-a9bf4c291d80",  # FACTORY_RESET
            }
            read_lines = ["=== Phantom characteristic values ===\n"]
            for service in client.services:
                for char in service.characteristics:
                    if "read" not in char.properties:
                        continue
                    if char.uuid.lower() in _SKIP_READ:
                        continue
                    try:
                        val = await client.read_gatt_char(char.uuid)
                        try:
                            decoded = val.decode("utf-8", errors="replace").strip()
                        except Exception:
                            decoded = "(binary)"
                        read_lines.append(f"  {char.uuid}  hex={val.hex()!r}  str={decoded!r}\n")
                    except Exception as read_err:
                        read_lines.append(f"  {char.uuid}  ERROR={read_err}\n")
            # Write characteristic values dump when debug_dump enabled.
            if self._debug_dump_enabled():
                await self.hass.async_add_executor_job(self._write_char_values, read_lines)
                _LOGGER.debug("DISCOVERY: characteristic values written to %s",
                              self._debug_path("char_values.txt"))

            # Probe-read UUID_SCULPTURE (7eeaef37). Per Efraín 2026-05-24,
            # the firmware does NOT interpret the byte returned here as a
            # game config — that decoding (ai_level + color) was app-side
            # only, and the byte the firmware exposes is meaningless. The
            # read is retained ONLY for its side effect: forcing bleak to
            # touch this characteristic post-discovery surfaces GATT cache
            # staleness (Task #28, 2026-05-19) so we can reconnect cleanly.
            # The characteristic itself does matter — it carries
            # sculpture-mode opcodes 9/11/12 — but its on-read byte does not.
            try:
                _sc_val = await client.read_gatt_char(UUID_SCULPTURE)
                _LOGGER.debug(
                    "DISCOVERY: UUID_SCULPTURE read OK (%d bytes; content not interpreted)",
                    len(_sc_val) if _sc_val else 0,
                )
            except Exception as sc_err:
                # GATT cache staleness detection. Force a reconnect so
                # _ble_loop gets a fresh GATT table. Without this, the
                # integration can sit in a half-open state where
                # `connected` is on but no notifications arrive until
                # manual reload. Reuses the helper used by _ble_write so
                # the write + read paths behave identically.
                if await self._handle_gatt_staleness(
                    sc_err, UUID_SCULPTURE, op="discovery read"
                ):
                    raise  # propagate to _ble_loop → triggers fresh reconnect
                _LOGGER.debug("DISCOVERY: UUID_SCULPTURE probe read failed: %s", sc_err)

            # Subscribe to every other notify-capable characteristic so we can
            # identify what fires when a piece is physically moved. Log at
            # WARNING level so messages appear in the HA system log.
            # Remove this block once UUIDs are mapped.
            _KNOWN_UUIDS = {u.lower() for u in [
                UUID_STATUS_BOARD, UUID_BATTERY_INFO, UUID_ERROR_MSG,
                UUID_SELECT_MODE, UUID_VERSION, UUID_RECEIVE_MOVEMENT,
                # 2026-05-10: also exclude UUID_SEND_MATRIX and UUID_FIRMWARE_STATE
                # from discovery iteration — they're now subscribed via the dedicated
                # known-characteristics path above. Discovery double-subscribing
                # would land two callbacks on every notify.
                UUID_SEND_MATRIX, UUID_FIRMWARE_STATE,
            ]}
            # Skip OTA and factory reset — don't want accidental triggers
            _SKIP_UUIDS = {
                "93601602-bbc2-4e53-95bd-a3ba326bc04b",  # OTA
                "b583ff00-b77a-42f5-a53f-a9bf4c291d80",  # FACTORY_RESET
            }
            for service in client.services:
                for char in service.characteristics:
                    uuid_lower = char.uuid.lower()
                    if "notify" not in char.properties:
                        continue
                    if uuid_lower in _KNOWN_UUIDS or uuid_lower in _SKIP_UUIDS:
                        continue

                    def _make_discovery_cb(u: str):
                        # Track last-seen value per UUID to suppress repeated identical messages
                        last_seen: dict[str, str] = {}
                        # Game channel — used for movementVerify ack writes
                        GAME_CHANNEL = UUID_GAME
                        def _cb(characteristic, data: bytearray) -> None:
                            try:
                                decoded = data.decode("utf-8", errors="replace").strip()
                            except Exception:
                                decoded = "(binary)"

                            # Firmware 0.3.0 prefixes game-channel ASCII payloads with
                            # the GameOPCode byte. Strip leading non-printable bytes
                            # so we can pattern-match against the textual content.
                            payload_str = decoded
                            opcode_byte: int | None = None
                            if data and data[0] < 0x20:  # control char = opcode
                                opcode_byte = data[0]
                                try:
                                    payload_str = data[1:].decode("utf-8", errors="replace").strip()
                                except Exception:
                                    payload_str = ""

                            # Suppress repeated identical values (e.g. constant "HOME" heartbeat)
                            prev = last_seen.get(u)
                            if decoded == prev:
                                return
                            last_seen[u] = decoded

                            # Matrix-state notification: parse and update sensors.
                            # Two channels carry matrix payloads on firmware 0.3.0:
                            #   - UUID_SEND_MATRIX (1b034927) — bare CLEAN/ERROR string
                            #   - Game channel (cc68a66e) — same payload prefixed with opcode 0x08
                            # The game-channel form arrives during Managing Mismatch and reset.
                            # Strip the opcode byte before parsing so _handle_matrix_bytes sees
                            # the same wire shape it expects from UUID_SEND_MATRIX.
                            if u.lower() == UUID_SEND_MATRIX.lower():
                                self._handle_matrix_bytes(bytes(data))
                            elif (
                                u.lower() == UUID_GAME
                                and opcode_byte == 0x08
                                and len(data) > 1
                            ):
                                _LOGGER.debug(
                                    "GAME_CHANNEL_MATRIX  routing %d-byte 0x08 payload to _handle_matrix_bytes",
                                    len(data) - 1,
                                )
                                self._handle_matrix_bytes(bytes(data[1:]))
                            # Firmware mode notification (Running/Paused/etc.)
                            elif u.lower() == UUID_FIRMWARE_STATE.lower():
                                self._handle_firmware_mode_bytes(bytes(data))

                            _LOGGER.debug(
                                "DISCOVERY notify  uuid=%s  hex=%s  str=%r  payload_str=%r  opcode=%s",
                                u, data.hex(), decoded, payload_str,
                                hex(opcode_byte) if opcode_byte is not None else None,
                            )

                            # BLE_MOVE_DONE (opcode 0x0C) — firmware signals
                            # the magnet has finished moving for the most recent
                            # GAME_START snapshot. Resolve any pending future so
                            # _phantom_execute_position can return. ALSO clear
                            # the post-activation move-suppression window — the
                            # magnet is now done, any subsequent `\x03M`
                            # notification is a real human move. This is the
                            # authoritative release condition (not CLEAN: Match,
                            # which can still be followed by sensor recalibration
                            # noise for several more seconds).
                            if (
                                u.lower() == UUID_GAME
                                and opcode_byte == 0x0c
                            ):
                                if (
                                    self._move_done_future is not None
                                    and not self._move_done_future.done()
                                ):
                                    self._move_done_future.set_result(True)
                                if self._activation_settle_until > 0:
                                    self._activation_settle_until = 0.0

                            # CLEAN: Match notification (opcode 0x08 with that exact
                            # status string) means the firmware's sensor matrix now
                            # matches the last-sent target. The payload doesn't echo
                            # the matrix back, so we trust _last_target_fen as the
                            # authoritative state and update live_fen accordingly.
                            # NOTE: this does NOT clear the post-activation
                            # move-suppression window — the firmware can emit
                            # additional sensor recalibration `\x03M` events
                            # for seconds AFTER CLEAN: Match arrives. The
                            # authoritative "magnet truly done" signal is
                            # BLE_MOVE_DONE (opcode 0x0c), which is where the
                            # suppression window is cleared.
                            if (
                                u.lower() == UUID_GAME
                                and opcode_byte == 0x08
                                and "CLEAN: Match" in payload_str
                                and self._last_target_fen is not None
                            ):
                                self._state["live_fen"] = self._last_target_fen
                                self.hass.loop.call_soon_threadsafe(
                                    self.async_set_updated_data, self._state
                                )

                            # Detect physical moves. In firmware 0.3.0 on cc68a66e, the
                            # human-move notification looks like:
                            #     b"\x03M 1 e2-e4"
                            # — i.e. opcode 0x03 (movementVerify) prefix, then "M <n> <from>-<to>".
                            # We pattern-match on payload_str (with the opcode stripped).
                            _is_move = (
                                payload_str.startswith("M ")
                                or payload_str.startswith("SQ ")
                                or (
                                    len(payload_str) >= 4
                                    and payload_str[0] in "abcdefgh"
                                    and payload_str[1] in "12345678"
                                    and payload_str[2] in "abcdefgh-x"
                                    and payload_str[3] in "abcdefgh12345678"
                                )
                            )
                            if _is_move:
                                # Suppress echoes of OUR last AI move using content-based
                                # detection. This replaces the old time-window kludge that
                                # incorrectly assumed the board had an internal AI —
                                # confirmed via Efraín's doc that the board has no chess
                                # intelligence, so every \\x03M notification is either an
                                # echo of our snapshot OR a real human move.
                                if self._is_ai_echo(payload_str):
                                    _LOGGER.debug(
                                        "DISCOVERY: skipping AI-echo on uuid=%s  payload=%r (matches last AI move=%s)",
                                        u, payload_str, self._last_ai_uci,
                                    )
                                    return
                                # Suppress magnet-driven moves during the
                                # post-activation settle window. Set to
                                # `loop.time() + 45` by every GAME_START
                                # write in `_phantom_execute_position`,
                                # cleared early on CLEAN: Match arrival.
                                # This catches the bug class where firmware
                                # emits an `\x03M` notification AFTER it has
                                # transitioned to "Board Playing" but BEFORE
                                # the magnet has fully settled — the
                                # firmware_mode-based filter below misses
                                # this window because firmware_mode is not
                                # yet "Setting Up". Reproduced 2026-05-25
                                # with a spurious `M 1 e8-g8` after a
                                # start_local_game from a previously-active
                                # board state.
                                if self.hass.loop.time() < self._activation_settle_until:
                                    _LOGGER.debug(
                                        "DISCOVERY: skipping post-activation move on uuid=%s  payload=%r (settle window %.1fs remaining)",
                                        u, payload_str,
                                        self._activation_settle_until - self.hass.loop.time(),
                                    )
                                    from datetime import datetime, timezone
                                    self._state["firmware_last_move"] = payload_str
                                    self._state["firmware_last_move_updated"] = datetime.now(timezone.utc).isoformat()
                                    self.hass.loop.call_soon_threadsafe(
                                        self.async_set_updated_data, dict(self._state)
                                    )
                                    return
                                # Suppress magnet-driven moves during firmware reset/setup
                                # phases. When the firmware drives the magnet to reposition
                                # pieces (Managing Mismatch / Setting Up / Snapping Pieces),
                                # it emits a `\x03M ...` notification for every magnet move
                                # — including long-distance cross-board drags like d2xd7.
                                # These are NOT human moves and applying them to self._board
                                # corrupts game state. Surface them on firmware_last_move so
                                # the dashboard can see the reset progress, but don't push.
                                _reset_modes = {"Managing Mismatch", "Setting Up", "Snapping Pieces"}
                                if self._state.get("firmware_mode") in _reset_modes:
                                    _LOGGER.debug(
                                        "DISCOVERY: skipping magnet-reset move on uuid=%s  payload=%r (firmware_mode=%s)",
                                        u, payload_str, self._state.get("firmware_mode"),
                                    )
                                    # Still record it as the last firmware-emitted move so the
                                    # dashboard can show "the magnet just moved X-Y."
                                    from datetime import datetime, timezone
                                    self._state["firmware_last_move"] = payload_str
                                    self._state["firmware_last_move_updated"] = datetime.now(timezone.utc).isoformat()
                                    self.hass.loop.call_soon_threadsafe(
                                        self.async_set_updated_data, dict(self._state)
                                    )
                                    return
                                _LOGGER.debug(
                                    "DISCOVERY: MOVE on uuid=%s  move=%r (opcode=%s) — acking + queuing",
                                    u, payload_str,
                                    hex(opcode_byte) if opcode_byte is not None else None,
                                )
                                # Surface the move on the firmware_last_move sensor so the
                                # dashboard can show the most recent physical move.
                                from datetime import datetime, timezone
                                self._state["firmware_last_move"] = payload_str
                                self._state["firmware_last_move_updated"] = datetime.now(timezone.utc).isoformat()

                                # Apply the move to our internal python-chess board so
                                # live_position can update without relying on a firmware
                                # matrix push (which doesn't fire during Board Playing).
                                #
                                # Firmware coordinate quirk (2026-05-10): black-piece
                                # sensor events are reported with a 180° rotation applied
                                # (rank-mirror + from-to-swap). White-piece events are
                                # reported as-is. The integration disambiguates by
                                # trying both interpretations against legal_moves and
                                # preferring the as-is one when both are legal.
                                try:
                                    raw_uci = _phantom_to_uci(payload_str)
                                    if raw_uci and len(raw_uci) >= 4:
                                        rotated_uci = _rotate_uci_180(raw_uci)
                                        # Build legality-ranked candidates: prefer as-is when both legal.
                                        candidates: list[tuple[str, chess.Move, str]] = []
                                        for label, candidate_uci in (("as-is", raw_uci), ("rotated", rotated_uci)):
                                            if candidate_uci == raw_uci and label == "rotated":
                                                continue  # identical (palindromic) — skip duplicate try
                                            try:
                                                _mv = chess.Move.from_uci(candidate_uci)
                                            except Exception:
                                                continue
                                            if _mv in self._board.legal_moves:
                                                candidates.append((candidate_uci, _mv, label))
                                        if candidates:
                                            uci, mv, chosen_label = candidates[0]
                                            self._board.push(mv)
                                            self._state["live_fen"] = self._board.board_fen()
                                            self._state["last_move"] = uci
                                            grid = self._build_phantom_matrix_from_fen(self._board.fen())
                                            self._state["piece_grid"] = grid
                                            self._state["piece_count"] = sum(1 for c in grid if c != ".")
                                            self._state["matrix_last_updated"] = self._state["firmware_last_move_updated"]
                                            # Update CLEAN: Match parser's cache so subsequent
                                            # firmware "match" notifications don't revert live_fen
                                            # to a stale earlier snapshot target.
                                            self._last_target_fen = self._board.board_fen()
                                            # Human-move TTS: don't announce the move itself
                                            # (player just made it), but DO announce check/mate
                                            # events triggered by the move. Active-game gate
                                            # excludes sculpture-mode echoes.
                                            if self._should_announce_active_game():
                                                event_speech = self._post_move_event_speech()
                                                if event_speech:
                                                    self.hass.async_create_task(
                                                        self._announce_via_tts(event_speech)
                                                    )
                                            _LOGGER.debug(
                                                "DISCOVERY: applied %s (raw=%s, rotated=%s, chosen=%s)",
                                                uci, raw_uci, rotated_uci, chosen_label,
                                            )
                                            # FIX (2026-05-14, post-Efraín-doc audit):
                                            # ONLY queue moves that passed the legality check —
                                            # and queue the resolved UCI (not raw payload_str).
                                            # Previously the queue insertion was UNCONDITIONAL
                                            # right after this if/else, which meant illegal moves
                                            # (e.g. firmware sensor-echoes of AI moves like the
                                            # AI-piece-just-moved triggering "M 1 e4-e2" notifications
                                            # for a black e7→e5 move with the 180° rotation) got
                                            # POSTed to Lichess → rejection cascade. By moving the
                                            # queue insert inside `if candidates:` and storing the
                                            # already-disambiguated UCI, _drain_physical_move_queue
                                            # also no longer needs to re-parse via _phantom_to_uci.
                                            if self._local_game_active:
                                                # Funnel through the
                                                # serialized replacement
                                                # helper so we can't race
                                                # against an in-flight AI
                                                # turn that's already
                                                # scheduled by some other
                                                # path. The lambda wraps
                                                # the async call so
                                                # call_soon_threadsafe
                                                # doesn't receive a
                                                # coroutine.
                                                self.hass.loop.call_soon_threadsafe(
                                                    lambda: self.hass.loop.create_task(
                                                        self._replace_local_game_task(
                                                            name=f"{DOMAIN}_local_ai_after_human",
                                                        ),
                                                        name=f"{DOMAIN}_local_ai_replace_after_human",
                                                    )
                                                )
                                            elif self._game_id:
                                                self.hass.loop.call_soon_threadsafe(
                                                    self._physical_move_queue.put_nowait, uci
                                                )
                                                self.hass.loop.call_soon_threadsafe(
                                                    lambda: self.hass.loop.create_task(
                                                        self._drain_physical_move_queue(),
                                                        name=f"{DOMAIN}_lichess_drain",
                                                    )
                                                )
                                        else:
                                            _LOGGER.warning(
                                                "DISCOVERY: neither raw=%s nor rotated=%s is legal in current position; recording firmware_last_move only — NOT queuing to Lichess",
                                                raw_uci, rotated_uci,
                                            )
                                except Exception as conv_err:
                                    _LOGGER.debug("DISCOVERY: move-decode failed: %s", conv_err)

                                # Push state to entities.
                                self.hass.loop.call_soon_threadsafe(
                                    self.async_set_updated_data, dict(self._state)
                                )

                                # Acknowledge on cc68a66e with movementVerify "1"
                                # (firmware 0.3.0 — UUID_CHECK_MOVE doesn't exist).
                                async def _ack(move_str: str = payload_str):
                                    try:
                                        await client.write_gatt_char(
                                            GAME_CHANNEL, b"\x031", response=True
                                        )
                                        _LOGGER.debug("DISCOVERY: movementVerify ack sent for %r", move_str)
                                    except Exception as ack_err:
                                        _LOGGER.warning("DISCOVERY: movementVerify ack failed: %s", ack_err)
                                self.hass.loop.call_soon_threadsafe(
                                    lambda: self.hass.loop.create_task(_ack())
                                )
                        return _cb

                    try:
                        await client.start_notify(char.uuid, _make_discovery_cb(char.uuid))
                        _LOGGER.debug("DISCOVERY subscribed to %s (props: %s)", char.uuid, char.properties)
                    except Exception as sub_err:
                        _LOGGER.debug("DISCOVERY subscribe failed %s: %s", char.uuid, sub_err)
            # ── END DISCOVERY MODE ────────────────────────────────────────────

            self.async_set_updated_data(self._state)

            # Start the matrix poll loop (firmware 0.3.0 doesn't push matrix
            # notifications for human-induced sensor changes).
            if UUID_SEND_MATRIX.lower() in available_uuids:
                self._matrix_poll_task = self.hass.loop.create_task(
                    self._matrix_poll_loop(), name=f"{DOMAIN}_matrix_poll"
                )
                _LOGGER.info("Started matrix-state poll loop (2s interval)")

            try:
                # Block here until disconnected or stop requested
                while not self._stop_event.is_set() and client.is_connected:
                    await asyncio.sleep(1)
            finally:
                if self._matrix_poll_task and not self._matrix_poll_task.done():
                    self._matrix_poll_task.cancel()
                    try:
                        await self._matrix_poll_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    self._matrix_poll_task = None

    # ── Developer-debug file dumps ───────────────────────────────────────────
    # These produced /config/phantom_chess_*.txt artifacts that were useful
    # during initial reverse-engineering but have no place in a production
    # install. Refactored 2026-05-16 (Task #16) to:
    #   1. Use hass.config.path() so the directory works on any HA install
    #      (HA OS, container, supervised, core), not just HA OS at /config.
    #   2. Live under a phantom_chess/debug/ subdirectory rather than
    #      polluting the config root.
    #   3. Be no-op when entry.options.debug_dump is not True.

    def _debug_dump_enabled(self) -> bool:
        opts = (self._entry.options if self._entry else {}) or {}
        return bool(opts.get("debug_dump", False))

    def _debug_path(self, filename: str) -> str:
        """Return an absolute path under <config>/phantom_chess/debug/ — the
        only place this integration writes developer-debug artifacts.
        Caller is responsible for ensuring the dir exists (use the helper
        below in executor context). Public-release safe."""
        return self.hass.config.path("phantom_chess", "debug", filename)

    def _write_gatt_dump(self, lines: list[str]) -> None:
        """Write GATT dump to file — runs in executor to avoid blocking the event loop."""
        if not self._debug_dump_enabled():
            return
        import os
        path = self._debug_path("gatt.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.writelines(lines)

    def _append_matrix_log(self, line: str) -> None:
        """Append one line to <config>/phantom_chess/debug/matrix_log.txt — executor."""
        if not self._debug_dump_enabled():
            return
        try:
            import os
            path = self._debug_path("matrix_log.txt")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write(line)
        except Exception:
            pass

    def _write_char_values(self, lines: list[str]) -> None:
        """Write characteristic value dump to file — runs in executor."""
        if not self._debug_dump_enabled():
            return
        import os
        path = self._debug_path("char_values.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.writelines(lines)

    def _on_ble_disconnect(self, client: BleakClient) -> None:
        self._ble_connected = False
        self._ble_client = None
        # Reset session flag so the next reconnect re-initialises via GAME_END.
        self._phantom_session_initialized = False
        # Cancel any pending BLE_MOVE_DONE waiter — it'll never arrive now.
        if self._move_done_future is not None and not self._move_done_future.done():
            self._move_done_future.set_exception(
                ConnectionError("BLE disconnected before BLE_MOVE_DONE")
            )
        _LOGGER.warning("Phantom board disconnected")
        self.hass.loop.call_soon_threadsafe(
            self.async_set_updated_data, self._state
        )

    def _on_physical_move(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Called when the player physically moves a piece (STATUS_BOARD notification)."""
        move_str = data.decode("utf-8", errors="replace").strip()
        _LOGGER.debug("Physical move notification: %s", move_str)
        # Queue it for the game-logic coroutine
        self.hass.loop.call_soon_threadsafe(
            self._physical_move_queue.put_nowait, move_str
        )

    def _on_battery(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Parse battery notification: 'percent,wallStatus,charging,doneCharging'.

        Runs on a non-loop thread (notify callback). Parses without
        touching shared state, then marshals the apply onto the loop
        so the self._state mutation can't race against entity reads.
        Audit §1.5, 2026-05-19.
        """
        try:
            parts = data.decode().strip().split(",")
            percent = int(parts[0])
            charging = parts[2] == "1"
        except Exception:
            # Malformed payload — nothing we can apply.
            return
        self.hass.loop.call_soon_threadsafe(
            self._apply_battery_state, percent, charging,
        )

    def _apply_battery_state(self, percent: int, charging: bool) -> None:
        """Apply parsed battery values to coordinator state. Loop thread only."""
        self._state["battery_percent"] = percent
        self._state["battery_charging"] = charging
        self.async_set_updated_data(dict(self._state))

    def _on_error_msg(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        msg = data.decode("utf-8", errors="replace").strip()
        _LOGGER.warning("Phantom board error: %s", msg)

    def _on_matrix_notify(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """BLE notify callback for UUID_SEND_MATRIX (1b034927).

        Routes the raw bytes through _handle_matrix_bytes, which parses
        CLEAN/ERROR matrix payloads, updates live_position attributes
        (piece_grid, sensor_bitmap, matrix_status), and (when entry.options
        debug_dump is enabled) appends every notification to
        <config>/phantom_chess/debug/matrix_log.txt for analysis.
        Explicit subscription added 2026-05-10 because bleak's service
        discovery iteration returned UUID_SEND_MATRIX in only some sessions.
        """
        _LOGGER.debug(
            "MATRIX_NOTIFY  uuid=%s  len=%d  hex=%s  str=%r",
            characteristic.uuid, len(data), data.hex()[:120],
            data.decode("utf-8", errors="replace")[:120],
        )
        self._handle_matrix_bytes(bytes(data))

    def _on_firmware_mode_notify(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """BLE notify callback for UUID_FIRMWARE_STATE (acb6543c).

        Routes through _handle_firmware_mode_bytes which separates
        mode-label strings from move-event strings. Explicit subscription
        added 2026-05-10 for the same reason as _on_matrix_notify.
        """
        _LOGGER.debug(
            "FIRMWARE_MODE_NOTIFY  uuid=%s  str=%r",
            characteristic.uuid,
            data.decode("utf-8", errors="replace").strip(),
        )
        self._handle_firmware_mode_bytes(bytes(data))

    # ── BLE write helpers ─────────────────────────────────────────────────────

    async def _handle_gatt_staleness(
        self, err: Exception, uuid: str, *, op: str = "access"
    ) -> bool:
        """Detect GATT cache staleness and trigger a fresh-discovery reconnect.

        Two cases produce bleak's "Characteristic not found" error:

          A) Cached but stale — the UUID was in discovery on this connection
             but the cached handle has gone bad (post-power-cycle, post-reload
             reuse, mid-game firmware reset). bleak's service cache is out of
             sync with the actual GATT table on the peripheral. Fix: forced
             disconnect so _ble_loop reconnects and bleak re-discovers
             services from scratch.

          B) Never existed — the integration is accessing a UUID that wasn't
             in discovery to begin with (e.g. a legacy firmware-0.1.6
             characteristic that 0.3.0 doesn't expose). No staleness;
             disconnecting here would be a regression that aborts whatever's
             in progress on the BLE link.

        We discriminate via `self._discovered_uuids` (populated by
        `_ble_connect_and_run` at the top of every successful connect).

        Shared between `_ble_write` (Task #12, 2026-05-16) and the discovery
        path's UUID_SCULPTURE probe-read (Task #28, 2026-05-19; characteristic
        formerly mis-named UUID_GAME_CONFIG) — the latter was the bug-report
        symptom that triggered this extraction. Before
        the extraction, the discovery read silently logged and continued,
        leaving the integration in a half-open "connected but no
        notifications" state until manual reload.

        Returns True if staleness was detected and disconnect was initiated
        (caller should bail), False otherwise (caller handles the error
        normally — typically log + continue).

        Two known BlueZ symptom families are matched:

          1) bleak's translated "Characteristic ... not found" — fires when
             bleak's service cache misses the UUID on the current
             connection. Originally the only case this method handled.

          2) Raw BlueZ `org.freedesktop.DBus.Error.UnknownObject` /
             `... doesn't exist` on the
             `org.bluez.GattCharacteristic1` interface — happens when
             BlueZ has torn down the GATT object (link-layer death,
             adapter reset, peer crash) but bleak's `is_connected`
             flag is still cached True. Added 2026-05-27 after an
             AI-vs-AI repro attempt sat in this state for ~6h.
        """
        msg = str(err).lower()
        looks_like_bleak_cache_miss = (
            "not found" in msg and "characteristic" in msg
        )
        # BlueZ-level GATT object torn down: D-Bus says the GATT
        # characteristic object literally doesn't exist anymore. The
        # interface name varies in error formatting but always contains
        # "gattcharacteristic" or the method name we tried to call.
        looks_like_bluez_gone = (
            "unknownobject" in msg
            or "doesn't exist" in msg
            or "does not exist" in msg
        ) and (
            "gattcharacteristic" in msg
            or "writevalue" in msg
            or "readvalue" in msg
            or "startnotify" in msg
            or "stopnotify" in msg
        )
        if not (looks_like_bleak_cache_miss or looks_like_bluez_gone):
            return False
        # The bleak-cache-miss path additionally gates on "was this UUID
        # ever discovered on this connection?" — if not, disconnecting
        # would be a regression. BlueZ-gone errors don't need that gate:
        # the characteristic object is provably absent at the OS layer.
        if looks_like_bleak_cache_miss and not looks_like_bluez_gone:
            uuid_lc = uuid.lower()
            if uuid_lc not in self._discovered_uuids:
                _LOGGER.debug(
                    "%s to %s failed with 'not found' — UUID was never in "
                    "discovery; not treating as staleness.",
                    op.capitalize(), uuid,
                )
                return False
        _LOGGER.warning(
            "GATT cache staleness on %s to %s: %s — forcing BLE reconnect "
            "for fresh service discovery",
            op, uuid, err,
        )
        # Flip our own connected flag immediately so the binary_sensor
        # reflects reality even if bleak's disconnect callback is slow.
        # The reconnect loop will set it back to True on the next
        # successful connect.
        self._ble_connected = False
        try:
            if self._ble_client is not None:
                await self._ble_client.disconnect()
        except Exception as disc_err:
            _LOGGER.debug(
                "Disconnect during staleness recovery failed: %s", disc_err,
            )
        return True

    async def _ble_write(self, uuid: str, data: str | bytes) -> None:
        if self._ble_client is None or not self._ble_client.is_connected:
            raise RuntimeError("BLE not connected")
        if isinstance(data, str):
            data = data.encode("utf-8")
        try:
            await self._ble_client.write_gatt_char(uuid, data, response=True)
        except BleakError as err:
            await self._handle_gatt_staleness(err, uuid, op="write")
            raise  # always propagate; caller decides retry policy

    async def async_debug_ble_write(self, uuid: str, data: str) -> None:
        """Diagnostic — write arbitrary payload to an arbitrary BLE characteristic.

        Data is UTF-8 by default. Prefix with "hex:" for raw bytes
        (e.g. "hex:0102FF" or "hex:01 02 FF"). Logs at WARNING so probe attempts
        are visible in system logs.
        """
        if data.startswith("hex:"):
            payload = bytes.fromhex(data[4:].replace(" ", ""))
        else:
            payload = data.encode("utf-8")
        _LOGGER.warning(
            "DEBUG_BLE_WRITE → uuid=%s data=%r (%d bytes)", uuid, payload, len(payload)
        )
        try:
            await self._ble_write(uuid, payload)
            _LOGGER.warning("DEBUG_BLE_WRITE OK uuid=%s", uuid)
        except Exception as err:
            _LOGGER.warning("DEBUG_BLE_WRITE FAIL uuid=%s: %s", uuid, err)
            raise

    # ── Phantom 0.3.0 protocol — confirmed via HCI capture 2026-05-09 ────────
    # See PROTOCOL.md for the full activation sequence and game-loop details.

    # Pure FEN→matrix conversion lives in matrix.py now (Task #21 step 1,
    # 2026-05-16). The staticmethod alias preserves every `self.
    # _build_phantom_matrix_from_fen(fen)` call-site in the rest of this
    # file without code changes.
    _build_phantom_matrix_from_fen = staticmethod(_build_matrix_from_fen_module)

    async def _phantom_send_game_start(self, fen: str = chess.STARTING_FEN, side: str = "W") -> None:
        """Send GameOPCode 0 (gameStart) with column-major matrix to UUID_GAME."""
        GAME_CHANNEL = UUID_GAME
        matrix = self._build_phantom_matrix_from_fen(fen)
        payload = bytes([0x00]) + (matrix + "," + side).encode("utf-8")
        _LOGGER.debug("Phantom gameStart: %d bytes (matrix=%s, side=%s)", len(payload), matrix, side)
        await self._ble_write(GAME_CHANNEL, payload)

    async def _phantom_send_side(self, side_value: str) -> None:
        """Send GameOPCode 10 (side) with payload '0', '1', or '2'.
        '1' = white-to-move, '2' = black-to-move, '0' = no-turn (sculpture mode)."""
        GAME_CHANNEL = UUID_GAME
        payload = bytes([0x0a]) + side_value.encode("utf-8")
        _LOGGER.debug("Phantom side: %r", side_value)
        await self._ble_write(GAME_CHANNEL, payload)

    async def _phantom_send_movement_verify(self, value: str = "1") -> None:
        """Send GameOPCode 3 (movementVerify) — confirms a human move was detected."""
        GAME_CHANNEL = UUID_GAME
        payload = bytes([0x03]) + value.encode("utf-8")
        await self._ble_write(GAME_CHANNEL, payload)

    async def _phantom_send_game_end(self) -> None:
        """Send GameOPCode 1 (gameEnd) — drops firmware to HOME state.

        No payload. Used as a precondition before the first GAME_START in a
        BLE session so that the subsequent snapshot actuates the magnet
        rather than just being absorbed as a state-initialization.
        """
        GAME_CHANNEL = UUID_GAME
        _LOGGER.debug("Phantom gameEnd: \\x01")
        await self._ble_write(GAME_CHANNEL, bytes([0x01]))

    async def _phantom_send_game_assistance(
        self,
        auto_castling: bool = True,
        auto_en_passant: bool = True,
        auto_snap_to_center: bool = True,
        auto_correct_wrong_move: bool = False,
        advanced_capture: bool = False,
        strict_gameplay: bool = False,
    ) -> None:
        """Send GameOPCode 11 (GAME_ASSISTANCE) with the six firmware assistance flags.

        Data format: "C,E,S,W,A,G" with each value "0" or "1":
          C = autoCastling           (firmware moves the rook for you on king-castle)
          E = autoEnPassant          (firmware removes the captured pawn)
          S = autoSnapToCenter       (firmware centers misaligned pieces)
          W = autoCorrectWrongMove   (firmware autoplays "corrections" — the cause of
                                      the 2026-05-12 23-move Lichess b2xc3 desync;
                                      default OFF here, contrary to the firmware's
                                      "1,1,1,1,0,0" factory default)
          A = advancedCapture        (firmware's advanced capture logic)
          G = strictGameplay         (firmware tracks graveyard and beeps on mismatch)

        Defaults here are tuned for HA-driven play where we own the game state:
        the firmware's auto-correct and strict-gameplay behaviors fight our snapshot
        writes and produce desyncs/beeps. The first three (castling, en passant,
        snap-to-center) are useful and stay on.

        Reference: EFRAIN_GAMEPLAY_DOC_2026-05-14.txt opcode 11.
        """
        GAME_CHANNEL = UUID_GAME
        flags = "{},{},{},{},{},{}".format(
            "1" if auto_castling else "0",
            "1" if auto_en_passant else "0",
            "1" if auto_snap_to_center else "0",
            "1" if auto_correct_wrong_move else "0",
            "1" if advanced_capture else "0",
            "1" if strict_gameplay else "0",
        )
        payload = bytes([0x0B]) + flags.encode()
        _LOGGER.debug("Phantom game_assistance: %s", flags)
        await self._ble_write(GAME_CHANNEL, payload)

    async def _phantom_send_check_sound(self, sound_type: str = "1") -> None:
        """Send GameOPCode 9 (CHECK_SOUND) — fires the firmware's native sound effect.

        Data: "1" for check, "2" for checkmate.

        Reference: EFRAIN_GAMEPLAY_DOC_2026-05-14.txt opcode 9.
        """
        if sound_type not in ("1", "2"):
            raise ValueError(f"sound_type must be '1' (check) or '2' (checkmate), got {sound_type!r}")
        GAME_CHANNEL = UUID_GAME
        payload = bytes([0x09]) + sound_type.encode()
        _LOGGER.debug("Phantom check_sound: %s", "check" if sound_type == "1" else "checkmate")
        await self._ble_write(GAME_CHANNEL, payload)

    async def _phantom_send_reset_detection(self, fen: str) -> None:
        """Send GameOPCode 14 (RESET_DETECTION) — resync firmware's expected matrix to a FEN.

        Unlike GAME_END + GAME_START, this updates the firmware's *expected* position
        in place without driving pieces — useful when we want the firmware to stop
        complaining about a mismatch we've already accepted, or to reset its model
        after a manual repositioning. Pass the board-only FEN (no metadata fields).

        Reference: EFRAIN_GAMEPLAY_DOC_2026-05-14.txt opcode 14.
        """
        GAME_CHANNEL = UUID_GAME
        board_only_fen = fen.split(" ")[0]
        payload = bytes([0x0E]) + board_only_fen.encode()
        _LOGGER.debug("Phantom reset_detection: %s", board_only_fen)
        await self._ble_write(GAME_CHANNEL, payload)

    async def _phantom_drop_to_home(self, timeout: float = 30.0) -> None:
        """Send GAME_END and block until firmware reports HOME mode.

        Required as a precondition before the first GAME_START in a BLE
        session. Without it, GAME_START silently transitions to BLE Playing
        without driving the motor. See XOUXOU_PROTOCOL.md "Critical
        precondition" section.

        Timeout was 5s originally — observed end-of-game scenarios where
        the firmware is still settling from the last move take longer. 30s
        is the same order as the GAME_START move-done timeout, and a true
        failure shouldn't take longer than that to surface.
        """
        await self._phantom_send_game_end()
        deadline = self.hass.loop.time() + timeout
        while self.hass.loop.time() < deadline:
            if self._state.get("firmware_mode") == "HOME":
                _LOGGER.debug("Phantom: firmware reached HOME after GAME_END")
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(
            f"Firmware did not reach HOME within {timeout}s after GAME_END "
            f"(current mode: {self._state.get('firmware_mode')!r})"
        )

    async def _phantom_execute_position(
        self,
        fen: str,
        side: str = "B",
        timeout_s: float = 30.0,
        side_opcode: str = "2",
    ) -> bool:
        """Drive the magnet to match the given target FEN.

        Implements the validated xouxou snapshot model:
          1. (Once per BLE session) drop to HOME via GAME_END
          2. GAME_START (opcode 0) with the 100-char column-major matrix + side
          3. 300 ms gap
          4. SIDE (opcode 0x0A) with payload `side_opcode` — default "2" matches
             xouxou's spectator pattern (BLE-side is the next mover). Pass "1"
             for active-play scenarios where the human (board side) should
             move first — necessary for the Lichess/Stockfish flow when the
             human plays white. Once firmware transitions out of Waiting Side
             this opcode is no-op, so the SIDE must be written ON this initial
             snapshot sequence — there's no retry path.
          5. Block until BLE_MOVE_DONE (opcode 0x0C) notification or timeout

        Args:
            fen: target board-only FEN or full FEN; only the position field
                 (chars before the first space) is used to build the matrix.
            side: "W" or "B" — xouxou convention is the color of the piece
                  that just moved. For a fresh game start, use "B".
            timeout_s: max seconds to wait for BLE_MOVE_DONE before giving up.
            side_opcode: "0" (2-local-player), "1" (board moves next), "2"
                  (BLE moves next). See Efraín's gameplay doc opcode 10.

        Returns:
            True if BLE_MOVE_DONE arrived before timeout, False otherwise.
        """
        if not self._ble_connected:
            raise RuntimeError("BLE not connected")
        side = side.upper()
        if side not in ("W", "B"):
            raise ValueError(f"side must be 'W' or 'B', got {side!r}")
        if side_opcode not in ("0", "1", "2"):
            raise ValueError(f"side_opcode must be '0'/'1'/'2', got {side_opcode!r}")

        # Session-init: drop to HOME before the first move of a BLE session.
        if not self._phantom_session_initialized:
            await self._phantom_drop_to_home()
            self._phantom_session_initialized = True

        # Cache target for the CLEAN: Match parser and the move-done future.
        self._last_target_fen = fen.split(" ")[0]
        self._move_done_future = self.hass.loop.create_future()
        # Open the post-activation move-detection suppression window
        # BEFORE the GAME_START write so the firmware's first sensor
        # events (which arrive within ~50ms of the write) are already
        # filtered. 600s upper bound (safety net only); the
        # BLE_MOVE_DONE handler clears the window the moment the magnet
        # sequence completes, which is the real release condition. See
        # __init__ for full rationale.
        self._activation_settle_until = self.hass.loop.time() + 600.0
        try:
            await self._phantom_send_game_start(fen=fen, side=side)
            # FIX (2026-05-14 morning): poll for firmware_mode == "Waiting Side"
            # before sending SIDE. The previous fixed 300ms sleep was a guess;
            # observed in logs that firmware was still in "Setting Up" when SIDE
            # arrived, causing SIDE writes to be silently ignored and firmware
            # to stay stuck in Waiting Side forever (instead of transitioning to
            # Board Playing). Now we actively wait up to 5s for Waiting Side.
            deadline = self.hass.loop.time() + 5.0
            while self.hass.loop.time() < deadline:
                if self._state.get("firmware_mode") == "Waiting Side":
                    break
                await asyncio.sleep(0.05)
            else:
                _LOGGER.warning(
                    "Phantom: Waiting Side not reached within 5s after GAME_START "
                    "(current mode: %r); sending SIDE anyway",
                    self._state.get("firmware_mode"),
                )
            # Small additional gap so SIDE write doesn't race the state-notify path
            await asyncio.sleep(0.1)
            await self._phantom_send_side(side_opcode)
            try:
                await asyncio.wait_for(self._move_done_future, timeout=timeout_s)
                _LOGGER.debug("Phantom: BLE_MOVE_DONE received within timeout")
                return True
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Phantom: BLE_MOVE_DONE not received within %.1fs; "
                    "move may not have actuated (current firmware_mode: %r)",
                    timeout_s, self._state.get("firmware_mode"),
                )
                return False
        finally:
            self._move_done_future = None

    async def async_move_piece(
        self,
        from_square: str,
        to_square: str,
        capture: bool = False,
        piece: str = "E",
    ) -> None:
        """Move a piece on the physical board from any square to any other square.

        Snapshot-based primitive: takes the current internal board state,
        applies the move as a raw piece relocation (no chess legality check),
        builds the post-move matrix, and drives the magnet via the validated
        xouxou GAME_START flow.

        For arbitrary state transitions (e.g. setting up a position from a
        FEN), prefer `_phantom_execute_position` directly.

        Args:
            from_square: source square in algebraic notation, e.g. 'e2'.
            to_square: destination square in algebraic notation, e.g. 'e4'.
            capture: kept for API compatibility; currently unused (the firmware
                     resolves the move semantics from the matrix diff).
            piece: kept for API compatibility; currently unused.
        """
        if not self._ble_connected:
            raise RuntimeError("BLE not connected")
        if not (len(from_square) == 2 and len(to_square) == 2):
            raise ValueError(
                f"from_square and to_square must be 2-char algebraic "
                f"notation (got {from_square!r}, {to_square!r})"
            )
        from_square = from_square.lower()
        to_square = to_square.lower()
        if (
            from_square[0] not in "abcdefgh"
            or from_square[1] not in "12345678"
            or to_square[0] not in "abcdefgh"
            or to_square[1] not in "12345678"
        ):
            raise ValueError(
                f"squares must be a-h + 1-8 (got {from_square!r}, {to_square!r})"
            )

        # Build the post-move position by manipulating self._board directly.
        # Using remove_piece_at + set_piece_at on the chess.Board sidesteps
        # turn-tracking and chess-rule validation, so this works for any
        # piece relocation (legal chess move or arbitrary positioning).
        src_sq = chess.parse_square(from_square)
        dst_sq = chess.parse_square(to_square)
        moving = self._board.remove_piece_at(src_sq)
        if moving is None:
            _LOGGER.debug(
                "move_piece: no piece on %s in current internal board; "
                "proceeding anyway — firmware will drive based on matrix diff",
                from_square,
            )
        # Overwrite destination (capture or move); python-chess handles either.
        if moving is not None:
            self._board.set_piece_at(dst_sq, moving)
        new_fen = self._board.board_fen()
        _LOGGER.debug(
            "move_piece: %s → %s, target FEN = %s", from_square, to_square, new_fen
        )

        ok = await self._phantom_execute_position(fen=new_fen, side="B")

        # Update all dashboard-visible state derived from self._board so the
        # dashboard (live_position FEN, piece_grid, last_move) stays in sync
        # with reality. Without this, subsequent move_piece calls operate on
        # a stale self._board, AND the dashboard's piece_grid attribute drifts
        # from the live_fen FEN state.
        self._state["live_fen"] = self._board.board_fen()
        self._state["last_move"] = f"{from_square}{to_square}"
        grid = self._build_phantom_matrix_from_fen(self._board.fen())
        self._state["piece_grid"] = grid
        self._state["piece_count"] = sum(1 for c in grid if c != ".")
        # Keep CLEAN: Match parser cache aligned with the new state.
        self._last_target_fen = self._board.board_fen()
        self.async_set_updated_data(dict(self._state))

        if not ok:
            _LOGGER.warning(
                "move_piece: BLE_MOVE_DONE timed out; firmware/board may be "
                "out of sync with integration's view"
            )

    async def _phantom_send_ai_move(self, uci: str, piece: str = "E") -> None:
        """Send GameOPCode 2 (MOVEMENT) with M-format payload — the explicit
        AI-move-during-active-game path.

        The opcode-2 MOVEMENT write tells the firmware exactly which move
        to physically execute, overriding whatever its onboard AI would
        choose. Use during active games AFTER detecting a human move via
        the `\\x03M` notification; for arbitrary state setting (move_piece,
        start_game reset) use the snapshot-based `_phantom_execute_position`
        instead.

        uci is e.g. 'e2e4' or 'e4xd5' (capture). piece is the Phantom
        piece tag — the official iOS app uses 'E' as the trailing letter
        for engine moves.

        (Duplicate definition at line 1296 removed 2026-05-17 dead-code audit.)
        """
        GAME_CHANNEL = UUID_GAME
        # Detect capture via current python-chess state, then build M-string.
        try:
            move = chess.Move.from_uci(uci)
            capture = self._board.is_capture(move)
        except Exception:
            capture = False
        sep = "x" if capture else "-"
        from_sq = uci[:2]
        to_sq = uci[2:4]
        m_str = f"M {from_sq}{sep}{to_sq} {piece}"
        payload = bytes([0x02]) + m_str.encode("utf-8")
        _LOGGER.debug("Phantom movement: %r", m_str)
        await self._ble_write(GAME_CHANNEL, payload)

    async def _phantom_select_chess_play_mode(self) -> None:
        """Set the firmware to chess-play mode (mode 2) via UUID_SELECT_MODE."""
        await self._ble_write(UUID_SELECT_MODE, b"2")

    async def async_phantom_start_game(
        self,
        fen: str = chess.STARTING_FEN,
        side: str = "W",
        wait_for_running_timeout_s: float = 30.0,
        side_opcode: str = "2",
    ) -> None:
        """Start a Phantom chess game using the snapshot protocol.

        Validated 2026-05-13 on firmware 0.3.0:
          1. GAME_END (\\x01) → wait for firmware HOME mode (session init)
          2. GAME_START (opcode 0) with column-major matrix from FEN
          3. 300 ms gap
          4. SIDE (opcode 0x0A) with literal "2"
          5. Wait for BLE_MOVE_DONE if a diff exists, or for firmware to reach
             BLE Playing if no motor action is needed (sensors already match).

        Replaces the old SELECT_MODE 2 + manual piece-touch protocol — the
        snapshot model bootstraps without user intervention. If the physical
        board doesn't match the target FEN, the magnet drives the pieces to
        match before returning.

        Args:
            fen: target position FEN (board-only or full). Defaults to standard
                 starting position.
            side: "W" or "B" — xouxou convention is the color that just moved.
                  For a fresh start, "B" works (firmware just records the flag).
                  Old default "W" is kept for caller compatibility.
            wait_for_running_timeout_s: max seconds to wait for BLE_MOVE_DONE.
        """
        if not self._ble_connected:
            raise RuntimeError("BLE not connected")
        side = side.upper()
        if side not in ("W", "B"):
            raise ValueError(f"side must be 'W' or 'B', got {side!r}")

        # Reset session-init flag so we always drop to HOME for a fresh game,
        # even if the integration thinks the session is already initialised.
        # A clean start-of-game GAME_END is cheap and avoids state ambiguity.
        self._phantom_session_initialized = False

        _LOGGER.debug(
            "Phantom start_game: snapshot protocol, target FEN = %s, side = %s",
            fen, side,
        )
        ok = await self._phantom_execute_position(
            fen=fen, side=side, timeout_s=wait_for_running_timeout_s,
            side_opcode=side_opcode,
        )
        if not ok:
            _LOGGER.warning(
                "Phantom start_game: BLE_MOVE_DONE timed out — firmware may "
                "still be settling. Current mode: %r",
                self._state.get("firmware_mode"),
            )
        _LOGGER.debug(
            "Phantom start_game: ACTIVATED. Firmware mode: %r",
            self._state.get("firmware_mode"),
        )

    async def async_phantom_apply_ai_move(self, uci: str) -> None:
        """Apply an AI/engine move using the snapshot protocol.

        Push the UCI move onto self._board, build the post-move matrix, drive
        the magnet via _phantom_execute_position (GAME_START opcode 0 +
        column-major matrix + SIDE flag). The firmware moves the pieces to
        match the new state.

        Re-introduced 2026-05-14 after end-of-session reframing: xouxou's
        spectator tool demonstrably uses this exact primitive to push
        arbitrary state transitions over BLE, and the firmware physically
        executes them. The triplet variant (opcode 2 MOVEMENT) made it 23
        moves into a Lichess game on 2026-05-13 before breaking with a
        firmware notification of `M 1 b2xc3` we couldn't reconcile — the
        cause was attributed to "firmware AI autoplay" but never proven.
        The snapshot model removes the opcode-2 MOVEMENT write entirely,
        treating every move as a state-set, which is what xouxou does and
        what the firmware reliably executes for board-sync purposes.

        After the snapshot completes, send SIDE "1" to nudge the firmware
        back into Board Playing for human-move detection on the next turn.

        Snapshot-based moves remain available via _phantom_execute_position
        for any arbitrary state setting (move_piece, start_game reset).
        """

        if not self._ble_connected:
            raise RuntimeError("BLE not connected")
        if len(uci) < 4:
            raise ValueError(f"Invalid UCI move: {uci!r}")

        mv = chess.Move.from_uci(uci)
        if mv not in self._board.legal_moves:
            _LOGGER.warning(
                "apply_ai_move: %s not legal in current board (%s) — "
                "skipping; integration state may be out of sync",
                uci, self._board.fen(),
            )
            return

        # Build TTS announcement BEFORE pushing (we need pre-move board state
        # to detect capture/castle/piece type). The announcement fires for AI
        # moves but is gated on active-game status — sculpture mode handles
        # its own TTS in the script.
        ai_move_speech = self._build_move_speech(mv) if self._should_announce_active_game() else ""

        # Snapshot pre-move board so `_set_last_ai_move` can detect
        # castling and pre-register the rook's UCI for echo suppression.
        # Castle fires TWO `\x03M` notifications (king + rook); without
        # the rook in the echo set, the rook event is treated as a
        # phantom human move and the unconditional movementVerify ack
        # confuses the firmware. See _set_last_ai_move docstring.
        pre_move_board = self._board.copy(stack=False)

        # Apply the move locally first so we have the post-move FEN.
        self._board.push(mv)
        post_fen = self._board.fen()
        # Side flag per Efraín's official gameplay doc (2026-05-14): the
        # comma-trailing color field in the GAME_START payload is the
        # CONSTANT color the BOARD PLAYER (human) is playing — NOT the
        # color that just moved (xouxou's spectator-mode convention we
        # previously copied). Sending the flipping value caused firmware
        # to autocorrect the human's next move because the firmware's
        # internal turn-tracking thought it was the AI's turn.
        # Fixed 2026-05-14 mid-game after observing "Managing Mismatch"
        # → "N c3-b1" autocorrect on Luke's legal Nc3.
        board_player_side = "W" if self._our_color == chess.WHITE else "B"

        # Fire AI-move TTS + any post-move event (check/mate). Fire-and-forget;
        # never blocks the move flow.
        if ai_move_speech:
            event_speech = self._post_move_event_speech()
            full = (ai_move_speech + ". " + event_speech).strip(". ").strip()
            if full:
                self.hass.async_create_task(self._announce_via_tts(full))

        # Register this AI move's UCI(s) for content-based echo detection.
        # When the firmware emits its sensor-derived \\x03M notification(s)
        # for the magnet's motion (typically within 5-15s of the snapshot,
        # one per piece moved — so two for castling), the discovery
        # callback will recognize each as an echo of THIS move and
        # suppress them. Passing `mv` and the pre-move board lets
        # _set_last_ai_move expand the echo set to include the rook's
        # UCI when the AI move is a castle.
        self._set_last_ai_move(uci, mv=mv, pre_move_board=pre_move_board)

        # One-shot retry on transient transport errors. The snapshot is
        # idempotent — sending the same target FEN twice doesn't double-
        # move the magnet, firmware just re-sets to the same matrix.
        # Covers: TypeError 'NoneType object can't be awaited' (root cause
        # not yet pinpointed — see Task #13), BleakError ATT 0x0e
        # (Unlikely Error), BLE disconnect mid-write. After max_attempts
        # we still fall through to the rollback + raise path.
        #
        # The previous catch wrapped the inner exception's message but
        # discarded the traceback, which made the NoneType bug impossible
        # to diagnose. _LOGGER.exception() dumps the full chain so next
        # time we'll see the exact await site that raised it.
        # Combined fix for Tasks #6 and #13 (2026-05-16).
        max_attempts = 2
        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                # After the AI move snapshot, it's the HUMAN's turn (board side),
                # so SIDE "1" must be written inside the activation sequence
                # (only honored while firmware is in Waiting Side). Previously this
                # was done as a separate SIDE "1" write AFTER _phantom_execute_position,
                # but that's a no-op because firmware has already transitioned past
                # Waiting Side. Fixed 2026-05-14.
                ok = await self._phantom_execute_position(
                    fen=post_fen, side=board_player_side, timeout_s=30.0,
                    side_opcode="1",
                )
                if not ok:
                    _LOGGER.warning(
                        "apply_ai_move: BLE_MOVE_DONE timed out for %s; "
                        "board may be out of sync with self._board",
                        uci,
                    )
                last_err = None
                break  # success (or move-done timeout — both exit the retry loop)
            except Exception as exec_err:
                last_err = exec_err
                _LOGGER.exception(
                    "apply_ai_move attempt %d/%d failed for %s",
                    attempt, max_attempts, uci,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(0.25)  # brief backoff before retry
                    continue
        if last_err is not None:
            # All retries exhausted. The physical board did not receive
            # the AI move, but our internal self._board has it pushed
            # AND the Lichess stream will keep advancing as the game
            # continues. Surface a persistent_notification telling the
            # user how to recover (continue on phone, then resume via
            # phantom_chess.resume_from_phone) — never silently end the
            # Lichess game (Task #7 / feedback_no_auto_resign_lichess).
            try:
                self.hass.async_create_task(
                    self.hass.services.async_call(
                        "persistent_notification", "create",
                        {
                            "title": "Phantom Chess: AI move not delivered",
                            "message": (
                                f"The AI's move (`{uci}`) couldn't be driven "
                                f"to the physical board after retries. "
                                f"**Your Lichess game is still active** — "
                                f"play this move on Lichess.org or your phone "
                                f"to continue. When the physical board is "
                                f"back in a clean state, call the "
                                f"`phantom_chess.resume_from_phone` service "
                                f"and the integration will push the current "
                                f"position to the board so you can keep "
                                f"playing physically.\n\n"
                                f"Error: `{last_err}`"
                            ),
                            "notification_id": "phantom_chess_ai_move_failed",
                        },
                    )
                )
            except Exception as notif_err:
                _LOGGER.debug(
                    "Could not create AI-move-failed notification: %s",
                    notif_err,
                )
            # Critical: do NOT roll back self._board.pop() — the move IS
            # legitimate per Lichess; we want self._board to stay in
            # sync with the authoritative Lichess state so the next move
            # detection and resume_from_phone work correctly. The
            # physical board is what's diverged, not our model.
            raise RuntimeError(f"apply_ai_move BLE write failed: {last_err}") from last_err

        # Content-based echo detection: the AI's move is already recorded via
        # _set_last_ai_move() before the BLE write. Any subsequent MOVEMENT
        # notification whose UCI (or 180°-rotated UCI) matches the recorded
        # value is treated as a sensor echo and suppressed. No time window
        # needed — the legacy _expecting_ai_echo_until field is retained as a
        # zero-value safety net only.

        # Update state attributes for entities. self._board was already pushed
        # above (before _phantom_execute_position) so all derived state is fresh.
        self._state["live_fen"] = self._board.board_fen()
        self._state["last_move"] = uci
        grid = self._build_phantom_matrix_from_fen(self._board.fen())
        self._state["piece_grid"] = grid
        self._state["piece_count"] = sum(1 for c in grid if c != ".")
        # Keep the CLEAN: Match parser cache aligned with post-move state.
        self._last_target_fen = self._board.board_fen()
        self.async_set_updated_data(dict(self._state))

        _LOGGER.debug("Phantom AI move applied via snapshot: %s", uci)

    # ── Game services (called from HA services) ───────────────────────────────

    async def async_start_game(
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
                auto_correct_wrong_move=True, advanced_capture=False, strict_gameplay=False,
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

        # Announce game start via TTS.
        you_play = self.player_color.title() if self.player_color != "random" else "either color"
        self.hass.async_create_task(self._announce_via_tts(
            f"Starting Lichess game against AI level {self.ai_level}. You play {you_play}."
        ))

        # Create Lichess AI challenge
        session = async_get_clientsession(self.hass)
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
        _LOGGER.info("Lichess game started: %s", self._game_id)
        self._state["lichess_game_id"] = self._game_id
        self.async_set_updated_data(self._state)

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
            await asyncio.sleep(0.1)
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
        session = async_get_clientsession(self.hass)
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

    async def async_back_to_modes(self) -> None:
        """Reset the dashboard to its mode-picker state.

        Replaces the v0.3 script `phantom_back_to_modes`. Two effects:
        clear the post-game review flag (so the "review" conditional
        card hides), and reset `setup_mode` to "Choose a mode" (so the
        mode picker re-renders). Added 2026-05-26 (v0.4-alpha3).
        """
        from .const import DEFAULT_SETUP_MODE
        self.setup_mode = DEFAULT_SETUP_MODE
        self._state["lichess_review_ready"] = False
        self.async_set_updated_data(dict(self._state))

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

    async def async_play_selected_sculpture(self) -> None:
        """Play back the sculpture game currently selected in
        `select.<device>_sculpture_game`.

        v0.4-alpha3 ships a STUB: enters sculpture mode (firmware
        SELECT_MODE=1) and fires a persistent notification telling the
        user that per-game move sequences will land in a later alpha.
        Replaces the v0.3 script `phantom_play_selected_sculpture` which
        dispatched to one of 18 per-game scripts containing hardcoded
        move data — bundling those move sequences in the integration is
        future work (v0.4-alpha5 or v0.5).
        """
        await self.async_start_sculpture()
        try:
            await self.hass.services.async_call(
                "persistent_notification", "create",
                {
                    "title": "Phantom Chess: Sculpture playback",
                    "message": (
                        f"Selected: **{self.selected_sculpture}**\n\n"
                        f"Entered sculpture mode on the firmware. "
                        f"Per-game move-sequence playback inside the "
                        f"integration is queued for a later v0.4 alpha; "
                        f"for now, the game data lives in the user-side "
                        f"`script.phantom_sculpture_*` scripts from v0.3's "
                        f"`examples/scripts.yaml`. If you have those scripts "
                        f"installed, you can call them directly to play "
                        f"a specific game."
                    ),
                    "notification_id": "phantom_chess_sculpture_stub",
                },
            )
        except Exception as err:
            _LOGGER.debug(
                "play_selected_sculpture: notification create failed: %s", err,
            )

    async def async_resign(self) -> None:
        """Resign the current game."""
        if not self._game_id:
            return
        session = async_get_clientsession(self.hass)
        url = LICHESS_RESIGN_URL.format(game_id=self._game_id)
        async with session.post(
            url,
            headers={"Authorization": f"Bearer {self._lichess_token}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (200, 201):
                _LOGGER.warning("Resign failed: %s", await resp.text())
        self._state["game_status"] = STATUS_RESIGNED
        self._game_id = None
        self.async_set_updated_data(self._state)

    async def async_send_move(self, uci: str) -> None:
        """Manually inject a move (for testing / external control).

        Routes to the active game backend (local AI or Lichess).
        """
        if self._local_game_active:
            await self._push_move_to_local_ai(uci)
        else:
            await self._push_move_to_lichess(uci)

    async def async_execute_dashboard_move(
        self, uci: str, promotion: str | None = None
    ) -> None:
        """User-initiated move from the dashboard interactive board (Task #27).

        Distinct from async_send_move (which assumes the user already moved
        the piece physically) — here the user is sitting back and clicking
        on the dashboard UI, so the firmware must drive the magnet to move
        the physical piece for them. We route through
        async_phantom_apply_ai_move (a misnamed but generic "execute UCI on
        board + push to internal state" primitive) and then notify the game
        backend.

        For Lichess games: drive magnet → POST to Lichess Board API. The
        stream will echo our move back; _process_move_list de-duplicates
        via _processed_moves + already-pushed detection.

        For local games: drive magnet → trigger AI response.

        Validates:
        - There IS an active game (otherwise raises RuntimeError).
        - It IS our turn (otherwise raises RuntimeError — prevents the user
          from accidentally playing the opponent's pieces on the dashboard).
        - The UCI is well-formed and legal in the current position
          (raises ValueError).
        """
        # Normalize promotion suffix. Frontend may send promotion as a
        # separate field for UI clarity, or pre-pasted into the UCI.
        if promotion:
            promo_lc = promotion.strip().lower()
            if promo_lc not in ("q", "r", "b", "n"):
                raise ValueError(
                    f"Invalid promotion piece {promotion!r}; expected q/r/b/n"
                )
            if len(uci) == 4:
                uci = uci + promo_lc
            elif len(uci) == 5 and uci[4].lower() != promo_lc:
                raise ValueError(
                    f"UCI {uci!r} and promotion {promotion!r} disagree"
                )

        try:
            move = chess.Move.from_uci(uci)
        except (ValueError, chess.InvalidMoveError) as err:
            raise ValueError(f"Invalid UCI move {uci!r}: {err}") from err

        if not (self._local_game_active or self._game_id):
            raise RuntimeError(
                "No active game — start a game before executing dashboard moves"
            )

        if move not in self._board.legal_moves:
            raise ValueError(
                f"Move {uci} is not legal in position {self._board.fen()}"
            )

        # Turn-check. self._our_color is the resolved color (random already
        # picked W/B at game start). Don't let the user play the opponent's
        # pieces on the dashboard.
        if self._board.turn != self._our_color:
            raise RuntimeError(
                f"Not your turn — side to move is "
                f"{'white' if self._board.turn == chess.WHITE else 'black'}"
            )

        if not self._ble_connected:
            raise RuntimeError("Board not connected via Bluetooth")

        # Drive the magnet + push onto self._board. apply_ai_move handles
        # the BLE writes, the TTS announcement, the retry loop, and the
        # internal board state — exactly what we want for a dashboard
        # move except the TTS speech is "AI move" framing. That's fine
        # for now (the move announcement is informational either way);
        # if it bothers users we can plumb a "suppress_speech" flag.
        await self.async_phantom_apply_ai_move(uci)

        # Backend notification.
        if self._game_id:
            # Lichess game — POST to API so Lichess records our move.
            # The stream's echo will be de-duplicated by _process_move_list.
            await self._push_move_to_lichess(uci)
        elif self._local_game_active:
            # Local Stockfish game — apply_ai_move already pushed onto
            # self._board. Mirror what _push_move_to_local_ai does for the
            # game-end / next-turn bookkeeping.
            self._state["last_move"] = uci
            # Re-run the analysis pipeline for the user's move so the
            # rich learning view's move history populates. apply_ai_move
            # already pushed; we need to look at the move it just pushed.
            try:
                self._record_and_analyze_local_move(
                    move, mover_is_white=(self._our_color == chess.WHITE),
                )
            except Exception as err:
                _LOGGER.debug("dashboard-move analysis hook failed: %s", err)

            if self._board.is_checkmate():
                self._state["game_status"] = STATUS_CHECKMATE
                self._local_game_active = False
                self._state["local_game_active"] = False
                self.async_set_updated_data(self._state)
                return
            if self._board.is_stalemate() or self._board.is_insufficient_material():
                self._state["game_status"] = STATUS_DRAW
                self._local_game_active = False
                self._state["local_game_active"] = False
                self.async_set_updated_data(self._state)
                return

            self._state["game_status"] = STATUS_PLAYING
            self.async_set_updated_data(self._state)

            # Trigger AI response via the serialized replacement helper
            # (audit §1.4) so we can't race against an in-flight turn
            # already scheduled by the discovery callback path.
            await self._replace_local_game_task(name=f"{DOMAIN}_local_ai_dashboard")

    async def async_takeback(self, count: int = 1) -> None:
        """Undo the last ``count`` plies on the physical board and in
        integration state.

        Wire format: UUID_GAME opcode 5 (TAKE_BACK), payload
        ``"count,FEN,side"`` where:
          - ``count`` = number of plies undone (parsed by firmware but
            currently unused — kept as future-proofing per Efraín's
            2026-05-24 reply to the protocol questions doc);
          - ``FEN`` = position AFTER the takeback (the target state;
            firmware physically rearranges pieces to match);
          - ``side`` = who plays NEXT after the takeback: ``"1"`` = the
            board side (human), ``"0"`` = the BLE side (AI/remote).

        For Lichess games, the takeback is requested through Lichess's
        Board API FIRST. If Lichess accepts, the BLE write fires and
        internal state rolls back. If Lichess refuses (e.g. opponent
        hasn't moved yet, game isn't in a takeback-eligible state), no
        BLE write happens — that keeps the physical board in sync with
        the authoritative Lichess game state instead of drifting.

        For local Stockfish games and no-game-active sessions, the
        internal board is rolled back unconditionally and the BLE write
        repositions the physical pieces.

        Rewritten 2026-05-24 after Efraín confirmed the opcode-5 wire
        format. Pre-rewrite the integration wrote ``b"1"`` to a
        characteristic (89185e7a-…) that doesn't exist on firmware 0.3.0
        — silent no-op + Lichess drift.

        Args:
            count: plies to undo (1 = single move, 2 = move pair).
                   Defaults to 1.

        Raises:
            ValueError if ``count < 1``.
            RuntimeError if BLE is not connected.
        """
        if not self._ble_connected:
            raise RuntimeError("Board not connected via Bluetooth")
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")

        # If a Lichess game is active, request takeback there FIRST.
        if self._game_id:
            session = async_get_clientsession(self.hass)
            url = (
                f"https://lichess.org/api/board/game/"
                f"{self._game_id}/takeback/yes"
            )
            try:
                async with session.post(
                    url,
                    headers={"Authorization": f"Bearer {self._lichess_token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status not in (200, 201):
                        body = await resp.text()
                        _LOGGER.warning(
                            "Takeback: Lichess refused (HTTP %s): %s — "
                            "not driving the board so state stays in "
                            "sync with the active Lichess game",
                            resp.status, body,
                        )
                        return
            except Exception as err:
                _LOGGER.warning(
                    "Takeback: Lichess request raised %s — aborting "
                    "without BLE write so state stays in sync",
                    err,
                )
                return

        # Roll back integration state. Stops early if move_stack runs out.
        popped = 0
        for _ in range(count):
            if not self._board.move_stack:
                break
            try:
                self._board.pop()
                popped += 1
            except IndexError:
                break
        if popped == 0:
            _LOGGER.debug(
                "Takeback: internal board has no moves to undo; "
                "nothing to do",
            )
            return

        # Determine who plays next per opcode 5 semantics.
        # "1" = board side (human) moves next; "0" = BLE side (AI) moves.
        if self._our_color is not None:
            side = "1" if self._board.turn == self._our_color else "0"
        else:
            # No resolved color (rare — e.g. takeback issued before any
            # game started, or during a session that bypassed Lichess
            # color resolution). Default to "1" so the firmware hands
            # the next move to the physical board, which is the least
            # disruptive failure mode (user can just move a piece).
            side = "1"

        fen = self._board.fen()
        payload = b"\x05" + f"{popped},{fen},{side}".encode("utf-8")
        try:
            await self._ble_write(UUID_GAME, payload)
            _LOGGER.info(
                "Takeback: opcode 5 sent (count=%d, side=%s, fen=%s)",
                popped, side, fen,
            )
        except Exception as err:
            _LOGGER.warning("Takeback: BLE write failed: %s", err)
            raise

        # Sync sensor-visible state with the rolled-back position.
        self._state["live_fen"] = self._board.board_fen()
        self._state["last_move"] = (
            self._board.peek().uci() if self._board.move_stack else None
        )
        grid = self._build_phantom_matrix_from_fen(self._board.fen())
        self._state["piece_grid"] = grid
        self._state["piece_count"] = sum(1 for c in grid if c != ".")
        # CLEAN: Match parser cache must track the new target FEN, else
        # the next firmware "match" notification would revert live_fen
        # to whatever the pre-takeback target was.
        self._last_target_fen = self._board.board_fen()
        self.async_set_updated_data(dict(self._state))

    async def async_set_pause(self, paused: bool) -> None:
        """Pause or resume the board mechanism."""
        self.paused = paused
        self._state["game_status"] = STATUS_PAUSED if paused else STATUS_PLAYING
        # Mode 3 = pause, mode 2 = chess play
        mode = 3 if paused else MODE_CHESS_PLAY
        await self._ble_write(UUID_SELECT_MODE, str(mode).encode())
        self.async_set_updated_data(self._state)

    async def async_start_sculpture(self) -> None:
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
        await asyncio.sleep(0.3)
        # Switch to sculpture mode.
        await self._ble_write(UUID_SELECT_MODE, str(MODE_SCULPTURE).encode())
        _LOGGER.info(
            "Sculpture: entered firmware SELECT_MODE=%d. Playlist content not yet wired.",
            MODE_SCULPTURE,
        )
        # Reset session flag so the next chess-play start_game re-inits properly.
        self._phantom_session_initialized = False

    async def async_play_sound(self, sound: str) -> None:
        """Fire one of the firmware's native chess-event sounds.

        Args:
            sound: 'check' or 'checkmate' (case-insensitive). Maps to opcode 9
                   data '1' or '2' per Efraín's gameplay doc.
        """
        if not self._ble_connected:
            raise RuntimeError("BLE not connected")
        normalized = (sound or "").strip().lower()
        if normalized in ("check", "1"):
            await self._phantom_send_check_sound("1")
        elif normalized in ("checkmate", "mate", "2"):
            await self._phantom_send_check_sound("2")
        else:
            raise ValueError(
                f"play_sound: sound must be 'check' or 'checkmate', got {sound!r}"
            )

    async def async_reset_position(self) -> None:
        """Reset internal board state and drive the physical board to starting.

        Use after sculpture playback finishes so the dashboard FEN goes back
        to the standard opening position and self._board is in a clean state
        for the next sculpture or game. Does NOT start a chess game — the
        firmware ends up in BLE Playing / Waiting Side, ready for whatever
        comes next.

        Internal state reset includes self._board, _state["live_fen"],
        _state["last_move"], piece_grid, piece_count, and _last_target_fen.
        The dashboard's picture-entity board will repaint to the starting
        position after this call returns.

        Physical drive happens via _phantom_execute_position with the
        standard starting FEN. The session-init flag is reset first so a
        clean GAME_END → HOME cycle precedes the new snapshot.
        """
        if not self._ble_connected:
            raise RuntimeError("BLE not connected")

        # Reset internal python-chess state.
        self._board = chess.Board()
        self._state["live_fen"] = self._board.board_fen()
        self._state["last_move"] = None
        grid = self._build_phantom_matrix_from_fen(self._board.fen())
        self._state["piece_grid"] = grid
        self._state["piece_count"] = sum(1 for c in grid if c != ".")
        self._last_target_fen = self._board.board_fen()
        self.async_set_updated_data(dict(self._state))

        # Force a fresh GAME_END → HOME cycle and drive physical reset.
        self._phantom_session_initialized = False
        _LOGGER.debug("Phantom reset_position: driving physical board to starting FEN")
        ok = await self._phantom_execute_position(
            fen=chess.STARTING_FEN, side="W", timeout_s=60.0,
        )
        if not ok:
            _LOGGER.warning(
                "Phantom reset_position: BLE_MOVE_DONE timed out — physical "
                "board may still be settling. Current mode: %r",
                self._state.get("firmware_mode"),
            )

    async def async_set_mechanism_speed(self, value: int) -> None:
        """Write mechanism speed (1..5) to the firmware-native UUID.

        Previously wrote `SPEED <value>` to UUID_RECEIVE_MOVEMENT (c60c786b)
        — the 0.1.6 movement channel that doesn't exist on 0.3.0, so the
        write silently no-op'd. The correct UUID for 0.3.0 is
        UUID_MECHANISM_SPEED (acb646cc), and xouxou's tool confirms the
        firmware accepts a plain ASCII integer in the 1..5 range.
        """
        self.mechanism_speed = value
        from .const import UUID_MECHANISM_SPEED
        await self._ble_write(UUID_MECHANISM_SPEED, str(value).encode())

    # ── AI-move echo detection ────────────────────────────────────────────────
    #
    # The Phantom board has no internal chess intelligence — confirmed by
    # Efraín's gameplay doc + the unboxing instructions + the absence of any
    # standalone-play mode. Every `\\x03M ...` notification from the firmware
    # is either:
    #   (a) a sensor-derived echo of the magnet's motion driven by OUR snapshot
    #       (the AI's move that we executed), OR
    #   (b) a sensor-derived report of a HUMAN move on the physical board.
    #
    # The integration must distinguish (a) from (b) precisely — applying an
    # echo as if it were a human move corrupts state. The previous time-based
    # suppression window (3s, then extended to 45s) was a kludge that either
    # missed echoes (window too short) or swallowed legitimate human moves
    # (window too long). The correct discriminator is *content*: does the
    # incoming UCI match the AI's most recently executed move?
    #
    # Black-piece events are reported by the firmware with a 180° rotation
    # (rank-mirror + from-to-swap), so we precompute both forms and compare
    # against either. A wide time window (60s) is retained as a sanity guard
    # — if 60s elapse with no echo it's extremely unlikely the firmware will
    # ever emit one for that move.

    def _set_last_ai_move(
        self,
        uci: str,
        mv: chess.Move | None = None,
        pre_move_board: chess.Board | None = None,
    ) -> None:
        """Record the AI move just executed via snapshot. Subsequent firmware
        echoes of this move (or its 180° rotation) will be suppressed by
        _is_ai_echo until either the window expires or a different move arrives.

        For castling moves, the firmware fires TWO `\\x03M` notifications
        (one for the king, one for the rook). If only the primary `uci` is
        registered, the rook's notification is treated as a phantom human
        move and the ack write to UUID_GAME confuses the firmware's state
        machine — the firmware then ignores the next legitimate human
        move and the board "stops responding." Diagnosed 2026-05-25 by
        Luke. Pass `mv` and `pre_move_board` so we can detect castling
        and pre-register the rook's UCI as an expected echo too.

        Args:
            uci: The primary UCI for the AI move (king move for castling).
            mv: Optional chess.Move object. When supplied with pre_move_board,
                castling and other multi-piece move semantics are honored.
            pre_move_board: Optional board state BEFORE the move was pushed.
                Required to call `is_castling` / `is_kingside_castling`.
        """
        if not uci or len(uci) < 4:
            return
        # Build the set of UCIs the firmware may emit \x03M for. Primary +
        # 180°-rotated form (firmware rotates black-piece events). For
        # castling, also include the rook's primary + rotated UCI.
        echo_ucis: set[str] = {uci, _rotate_uci_180(uci)}
        if mv is not None and pre_move_board is not None:
            try:
                if pre_move_board.is_castling(mv):
                    rank = chess.square_rank(mv.from_square)
                    if pre_move_board.is_kingside_castling(mv):
                        rook_from = chess.square(7, rank)  # h-file
                        rook_to = chess.square(5, rank)    # f-file
                    else:  # queenside
                        rook_from = chess.square(0, rank)  # a-file
                        rook_to = chess.square(3, rank)    # d-file
                    rook_uci = (
                        chess.square_name(rook_from)
                        + chess.square_name(rook_to)
                    )
                    echo_ucis.add(rook_uci)
                    echo_ucis.add(_rotate_uci_180(rook_uci))
            except Exception as err:
                _LOGGER.debug(
                    "AI-echo: castling detection raised %s; falling back "
                    "to single-UCI echo set", err,
                )
        self._last_ai_uci = uci
        self._last_ai_uci_rotated = _rotate_uci_180(uci)
        self._last_ai_echo_ucis = echo_ucis
        import time as _time
        self._last_ai_uci_set_at = _time.monotonic()
        _LOGGER.debug(
            "AI-echo: tracking move=%s, echo set=%s for echo suppression",
            uci, sorted(echo_ucis),
        )

    def _is_ai_echo(self, payload_str: str) -> bool:
        """True if the firmware notification matches any of the UCIs in the
        last AI move's echo set (primary, rotated, plus castle-rook
        variants if applicable). Used by the discovery callback to drop
        magnet-driven sensor events that arrive after every AI move.
        """
        if not getattr(self, "_last_ai_echo_ucis", None):
            # Backward-compat: fall through to legacy single-UCI check
            # if the new set hasn't been populated yet.
            if self._last_ai_uci is None:
                return False
        import time as _time
        if _time.monotonic() - self._last_ai_uci_set_at > 60.0:
            # Window expired — assume all echoes arrived or none will.
            return False
        try:
            incoming_uci = _phantom_to_uci(payload_str)
        except Exception:
            return False
        if not incoming_uci:
            return False
        echo_set = getattr(self, "_last_ai_echo_ucis", None) or {
            self._last_ai_uci,
            self._last_ai_uci_rotated,
        }
        return incoming_uci in echo_set

    # ── TTS announcements for active games ────────────────────────────────────
    #
    # These helpers fire spoken commentary during Lichess and local-Stockfish
    # games via Home Assistant's tts.speak service. Sculpture scripts already
    # have their own per-move TTS (added 2026-05-13); this path adds the same
    # treatment to active games. Each AI/engine move and every check/mate
    # event gets spoken on the Living Room Voice PE.
    #
    # We intentionally do NOT speak the human's own moves — the player just
    # made them, no need to announce. Only AI moves get a full description.
    # Check/checkmate/stalemate events get spoken regardless of whose move.

    _PIECE_NAMES = {
        chess.PAWN: "pawn",
        chess.KNIGHT: "knight",
        chess.BISHOP: "bishop",
        chess.ROOK: "rook",
        chess.QUEEN: "queen",
        chess.KING: "king",
    }

    def _build_move_speech(self, mv: chess.Move) -> str:
        """Describe a move in natural English. Call BEFORE pushing the move
        (uses self._board's pre-move state to detect capture / castle / piece type).
        Returns '' if move is malformed.
        """
        piece = self._board.piece_at(mv.from_square)
        if piece is None:
            return ""
        side = "White" if self._board.turn == chess.WHITE else "Black"
        if self._board.is_castling(mv):
            castle_side = "kingside" if chess.square_file(mv.to_square) > 4 else "queenside"
            return f"{side} castles {castle_side}"
        piece_name = self._PIECE_NAMES.get(piece.piece_type, "piece")
        to_sq = chess.square_name(mv.to_square)
        verb = "takes" if self._board.is_capture(mv) else "to"
        return f"{side} {piece_name} {verb} {to_sq}"

    def _post_move_event_speech(self) -> str:
        """Describe post-move events (check/mate/stalemate). Call AFTER push.
        Returns '' if none apply. self._board.turn is the side NOW to move
        (i.e. the side whose king might be in check)."""
        if self._board.is_checkmate():
            winner = "Black" if self._board.turn == chess.WHITE else "White"
            return f"Checkmate. {winner} wins."
        if self._board.is_stalemate():
            return "Stalemate. Draw."
        if self._board.is_check():
            in_check = "White" if self._board.turn == chess.WHITE else "Black"
            return f"Check on {in_check}."
        return ""

    async def _announce_via_tts(self, message: str) -> None:
        """Surface an announcement to the user.

        Always fires the `phantom_chess_announce` event with the message
        in `event.data.message`. Users wire this to their TTS stack via
        a simple automation (see README). If the integration's options
        flow has `tts_service` and `tts_media_player_entity_id` set,
        ALSO call that TTS service directly — useful for users who don't
        want to author an automation.

        Refactored 2026-05-16 (Task #16 release-readiness): previously
        called tts.google_ai_tts on Luke's specific Voice PE media
        player, which broke the integration for every other user.

        Fire-and-forget; failures only log at debug level.
        """
        if not message:
            return

        # ── Event fan-out (always) ──────────────────────────────────────
        try:
            self.hass.bus.async_fire(
                "phantom_chess_announce",
                {"message": message, "board_address": self._ble_address},
            )
        except Exception as ev_err:
            _LOGGER.debug("phantom_chess_announce event fire failed: %s", ev_err)

        # ── Optional direct TTS call (when configured in options) ──────
        # Two-step null-check so mypy can narrow `self._entry` past
        # the truthy guard before we touch `.options`.
        entry = getattr(self, "_entry", None)
        options = (entry.options if entry is not None else {}) or {}
        tts_service = options.get("tts_service")
        tts_media_player = options.get("tts_media_player_entity_id")
        if tts_service and tts_media_player:
            # tts_service is "domain.service" e.g. "tts.google_ai_tts" — split
            # into ("tts", "google_ai_tts") for the service call. We don't
            # validate the format here; misconfiguration surfaces in HA logs.
            try:
                domain, _, service = tts_service.partition(".")
                if not domain or not service:
                    raise ValueError(f"tts_service must be 'domain.service', got {tts_service!r}")
                await self.hass.services.async_call(
                    "tts", "speak",
                    {
                        "entity_id": tts_service,
                        "media_player_entity_id": tts_media_player,
                        "message": message,
                        "cache": True,
                    },
                    blocking=False,
                )
                _LOGGER.debug("TTS dispatched via %s → %s: %s",
                              tts_service, tts_media_player, message)
            except Exception as e:
                _LOGGER.debug("TTS speak failed (%s): %s", tts_service, e)

    def _should_announce_active_game(self) -> bool:
        """True if we're in an active Lichess or local Stockfish game.
        Sculpture playback has its own TTS path and shouldn't double-announce."""
        # game_status is STATUS_PLAYING during both Lichess (start_game) and
        # local Stockfish (start_local_game). Sculpture mode doesn't set this.
        return self._state.get("game_status") == STATUS_PLAYING

    async def async_set_sound_level(self, value: int) -> None:
        """Write sound settings to UUID_SOUND_LEVEL.

        Per Efraín's gameplay doc 2026-05-14, the characteristic accepts a
        comma-separated payload "volume,sounds_bitmask,tutorial":
          - volume:  0-32 (NOT 0-100 — earlier-assumed range was wrong)
          - sounds:  5-digit binary mask enabling/disabling sound categories
          - tutorial: 0 or 1 (tutorial mode)
        Default factory value is "32,11110,0". We keep the bitmask and tutorial
        flag at their defaults and only vary the volume.
        """
        clamped = max(0, min(32, int(value)))
        self.sound_level = clamped
        payload = f"{clamped},11110,0".encode()
        await self._ble_write(UUID_SOUND_LEVEL, payload)

    # ── Lichess stream loop ───────────────────────────────────────────────────

    async def _lichess_stream_loop(self, game_id: str) -> None:
        """Stream Lichess game events and bridge AI moves to the board."""
        url = LICHESS_GAME_STREAM_URL.format(game_id=game_id)
        headers = {"Authorization": f"Bearer {self._lichess_token}"}
        session = async_get_clientsession(self.hass)

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
                        await asyncio.sleep(retry_delay)
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
                await asyncio.sleep(retry_delay)
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

        # If it's the AI's turn first (we're black), wait for the gameState event
        # which will arrive with the AI's first move.

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

        self.async_set_updated_data(self._state)

    def _on_game_finish(self, event: dict[str, Any]) -> None:
        status = event.get("status", {})
        name = status.get("name", "") if isinstance(status, dict) else str(status)
        _LOGGER.info("Game finished: %s", name)
        self._game_id = None
        # Reinforce terminal flags — defensive in case _on_game_state's
        # terminal branch missed an edge case.
        self._state["lichess_active"] = False
        self.async_set_updated_data(self._state)

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

        self.async_set_updated_data(self._state)

    # ── Lichess analysis pipeline (added 2026-05-14) ────────────────────────
    # See lichess_analysis.py for cloud-eval client + classification logic.
    # See phantom_chess_research/IN_GAME_DASHBOARD_SPEC_2026-05-14.md for the
    # contract these methods fulfill toward the dashboard.

    def _record_history_stub(self, move: chess.Move, mover_color: chess.Color) -> int:
        """Append a placeholder entry to move_history_moves; return its index.

        The placeholder shows "unknown" classification until the async
        _analyze_move task fills in CPL, label, motif, color, glyph.
        """
        from .lichess_analysis import classification_color_glyph
        # Compute SAN from a copy of the analysis board *before* it's pushed.
        # (Caller pushes immediately after calling this.)
        try:
            san = self._analysis_board.san(move)
        except (chess.IllegalMoveError, AssertionError, ValueError):
            san = move.uci()
        side = "white" if mover_color == chess.WHITE else "black"
        ply = (self._state.get("move_history_moves") or [])
        move_num = (len(ply) // 2) + 1
        color, glyph = classification_color_glyph("unknown")
        entry = {
            "ply": len(ply) + 1,
            "move_num": move_num,
            "side": side,
            "san": san,
            "uci": move.uci(),
            "classification": "unknown",
            "cpl": 0,
            "motif": "",
            "color": color,
            "glyph": glyph,
        }
        new_history = list(ply)
        new_history.append(entry)
        self._state["move_history_moves"] = new_history
        return len(new_history) - 1

    async def _analyze_starting_position(self) -> None:
        """Fetch initial eval + opening name for a brand-new game."""
        if self._analysis_client is None:
            return
        try:
            board = chess.Board()
            ev = await self._analysis_client.get_eval(board.fen())
            if ev is not None:
                self._state["eval_cp"] = ev.cp
                self._state["eval_mate"] = ev.mate
                self._state["eval_depth"] = ev.depth
                self._state["eval_source"] = ev.source
                if ev.best_uci:
                    try:
                        m = chess.Move.from_uci(ev.best_uci)
                        if m in board.legal_moves:
                            self._state["best_move_san"] = board.san(m)
                    except (ValueError, chess.IllegalMoveError):
                        pass
            name, eco = await self._analysis_client.get_opening(board.fen())
            if name:
                self._state["opening_name"] = name
                self._state["opening_eco"] = eco
            self.async_set_updated_data(dict(self._state))
        except Exception as err:
            _LOGGER.debug("Starting-position analysis failed: %s", err)

    async def _analyze_move(
        self,
        ply_index: int,
        board_before: chess.Board,
        board_after: chess.Board,
        move: chess.Move,
        mover_is_white: bool,
    ) -> None:
        """Background analysis for a single move. Updates move_history_moves
        in-place at ply_index, and refreshes the eval/best-move/threat
        sensors with the POST-move position. Failures degrade gracefully —
        the stub entry stays as 'unknown'."""
        if self._analysis_client is None:
            return
        try:
            from .lichess_analysis import (
                classify_move,
                classification_color_glyph,
                compute_threat_san,
                detect_fork,
            )
            pre_eval = await self._analysis_client.get_eval(board_before.fen())
            post_eval = await self._analysis_client.get_eval(board_after.fen())
            classification, cpl = classify_move(
                pre_eval, post_eval, move.uci(), mover_is_white
            )
            motif = "fork" if detect_fork(board_before, move) else ""
            color, glyph = classification_color_glyph(classification)

            # Update the history entry in-place. The list may have grown since
            # we appended (other moves arrived) — that's OK, we still own our
            # ply_index slot.
            history = list(self._state.get("move_history_moves") or [])
            # Retain the engine's PRE-move recommendation in the entry so the
            # post-game review can answer "what should I have played?" with a
            # concrete SAN rather than the v1 placeholder "—".
            pre_best_san: str | None = None
            if pre_eval is not None and pre_eval.best_uci:
                try:
                    pre_best_move = chess.Move.from_uci(pre_eval.best_uci)
                    if pre_best_move in board_before.legal_moves:
                        pre_best_san = board_before.san(pre_best_move)
                except (ValueError, chess.IllegalMoveError):
                    pre_best_san = None

            if 0 <= ply_index < len(history):
                history[ply_index] = {
                    **history[ply_index],
                    "classification": classification,
                    "cpl": int(cpl),
                    "motif": motif,
                    "color": color,
                    "glyph": glyph,
                    "best_san": pre_best_san,
                }
                self._state["move_history_moves"] = history

            # If this is the most-recent move, surface its details to the
            # last-move-detail strip.
            if ply_index == len(history) - 1:
                self._state["last_move_classification"] = classification
                self._state["last_move_cpl"] = int(cpl)
                self._state["last_move_motif"] = motif

            # Refresh top-level eval sensors from the POST-move position.
            if post_eval is not None:
                self._state["eval_cp"] = post_eval.cp
                self._state["eval_mate"] = post_eval.mate
                self._state["eval_depth"] = post_eval.depth
                self._state["eval_source"] = post_eval.source
                self._state["best_move_san"] = None
                if post_eval.best_uci:
                    try:
                        m = chess.Move.from_uci(post_eval.best_uci)
                        if m in board_after.legal_moves:
                            self._state["best_move_san"] = board_after.san(m)
                    except (ValueError, chess.IllegalMoveError):
                        pass

            # Threat warning for the side-to-move on the post-move position.
            try:
                threat = await compute_threat_san(
                    board_after, self._analysis_client
                )
                self._state["threat_san"] = threat
            except Exception as err:
                _LOGGER.debug("threat detection failed: %s", err)

            # Opening lookup, only while plausibly still in book.
            if board_after.fullmove_number <= 15:
                try:
                    name, eco = await self._analysis_client.get_opening(
                        board_after.fen()
                    )
                    if name:
                        self._state["opening_name"] = name
                        self._state["opening_eco"] = eco
                except Exception:
                    pass  # leave previous value

            # TTS announcement — only for the human's move and only if it's
            # noteworthy enough per training-wheels setting.
            mover_color = chess.WHITE if mover_is_white else chess.BLACK
            if mover_color == self._our_color:
                await self._maybe_announce_classification(
                    classification, int(cpl), motif
                )

            self.async_set_updated_data(dict(self._state))
        except Exception as err:
            _LOGGER.debug("Move analysis failed for ply %d: %s", ply_index, err)

    async def _maybe_announce_classification(
        self, classification: str, cpl: int, motif: str
    ) -> None:
        """TTS for move quality.

        Default (training_wheels OFF): announce only mistake/blunder.
        Training wheels ON: announce every classification.
        """
        from .lichess_analysis import (
            CLASSIFICATION_BEST, CLASSIFICATION_GOOD,
            CLASSIFICATION_BLUNDER, CLASSIFICATION_MISTAKE,
            CLASSIFICATION_INACCURACY,
        )
        verbose = False
        try:
            tw = self.hass.states.get("input_boolean.phantom_chess_training_wheels")
            if tw is not None and tw.state == "on":
                verbose = True
        except Exception:
            pass

        # Mate-transition handling: classify_move clamps loss to 9999 cp,
        # which divided by 100 yields up to ~100 pawns — absurd as a real
        # pawn count. A CPL anywhere near the clamp ceiling means the move
        # bridged a mate sentinel (±10000 cp), not a 100-pawn material loss.
        # Describe it as a mate event rather than a numeric loss.
        # Threshold 9000 cp ≈ 90 pawns leaves a comfortable margin between
        # real-world max losses (~30 pawns for a hung queen + position) and
        # the mate range. Added 2026-05-17.
        mate_transition = cpl >= 9000
        pawns = round(cpl / 100.0, 1)
        msg: str | None = None
        if classification == CLASSIFICATION_BLUNDER:
            if mate_transition:
                msg = "Blunder. You allowed a forced mate."
            else:
                msg = f"Blunder. You lost about {pawns} pawns."
        elif classification == CLASSIFICATION_MISTAKE:
            if mate_transition:
                msg = "Mistake. You allowed a forced mate."
            else:
                msg = f"Mistake. You lost about {pawns} pawns."
        elif verbose and classification == CLASSIFICATION_INACCURACY:
            msg = f"Slight inaccuracy. About {pawns} pawns."
        elif verbose and classification == CLASSIFICATION_BEST:
            msg = "Best move."
        elif verbose and classification == CLASSIFICATION_GOOD:
            msg = "Good move."

        if motif == "fork" and msg is not None:
            msg = msg.rstrip(".") + ". Watch for forks here."

        if msg:
            await self._announce_via_tts(msg)

    async def _build_post_game_review(self) -> None:
        """Post-game review payload: top 3 mistakes by the user's color,
        plus accuracy for both sides. Called when the game ends."""
        try:
            history = list(self._state.get("move_history_moves") or [])
            if not history:
                self._state["lichess_review_ready"] = False
                self.async_set_updated_data(dict(self._state))
                return

            our_color_str = "white" if self._our_color == chess.WHITE else "black"

            # Top 3 mistakes by CPL, only for the user's color, only
            # mistake/blunder/inaccuracy. Skip anything not yet analyzed.
            user_moves = [m for m in history if m.get("side") == our_color_str]
            scored = [
                m for m in user_moves
                if m.get("classification") in (
                    "inaccuracy", "mistake", "blunder"
                )
            ]
            scored.sort(key=lambda m: -int(m.get("cpl") or 0))
            top_n = scored[:3]
            # Augment each with best-move SAN and a description.
            top_with_best: list[dict[str, Any]] = []
            for m in top_n:
                # Best SAN at the time isn't stored per-move yet — for v1,
                # leave it as a placeholder. v2 will retain best_uci per ply.
                top_with_best.append({
                    **m,
                    "best_san": m.get("best_san") or "—",
                    "description": self._describe_mistake(m),
                })
            self._state["last_game_top_mistakes"] = top_with_best

            # Accuracy — we don't store per-ply pre/post evals yet, so derive
            # a coarse estimate by averaging absolute |cpl| → approx
            # winning-pct loss. v1 placeholder; v2 retains per-ply evals so
            # we can call compute_game_accuracy properly.
            wlosses = [int(m.get("cpl") or 0) for m in history if m.get("side") == "white"]
            blosses = [int(m.get("cpl") or 0) for m in history if m.get("side") == "black"]
            self._state["last_game_accuracy_white"] = self._coarse_accuracy(wlosses)
            self._state["last_game_accuracy_black"] = self._coarse_accuracy(blosses)

            self._state["lichess_review_ready"] = True
            self.async_set_updated_data(dict(self._state))
        except Exception as err:
            _LOGGER.debug("Post-game review build failed: %s", err)

    @staticmethod
    def _coarse_accuracy(cpls: list[int]) -> float | None:
        """Coarse per-side accuracy estimate from CPL list.

        v1: 100 - mean(cpl)/2, clamped 0-100. Not the real Lichess metric
        but order-preserving — a player with mean CPL 50 reads ~75%, mean
        CPL 200 reads ~0%. Replace with proper accuracy in v2 once we
        retain per-ply pre/post evals.
        """
        if not cpls:
            return None
        mean = sum(cpls) / len(cpls)
        return round(max(0.0, min(100.0, 100.0 - mean / 2.0)), 1)

    @staticmethod
    def _describe_mistake(m: dict[str, Any]) -> str:
        """One-line description of what went wrong.

        v1.1 (2026-05-15): if we have the engine's preferred move (best_san)
        retained per ply, weave it into the description so the user gets a
        concrete alternative — "Best was Nf3 — kept the diagonal" reads
        much better than "Sub-optimal".
        """
        cls = m.get("classification") or "unknown"
        motif = m.get("motif") or ""
        best = m.get("best_san") or ""
        cpl = int(m.get("cpl") or 0)

        # See _maybe_announce_classification for the rationale on the 9000-cp
        # mate-transition threshold. Same convention here for the post-game
        # review text.
        mate_transition = cpl >= 9000

        if motif == "fork":
            base = "Allowed a fork by the opponent."
        elif mate_transition:
            # Note: phrased without claiming this lost the game, because the
            # opponent may not have found the mating line — especially at
            # lower AI levels which deliberately limit search depth. The
            # analysis pipeline uses Lichess cloud-eval (strong) so it
            # surfaces tactical losses regardless of whether the on-board
            # opponent actually punished them. That gap is part of the
            # teaching value.
            base = "Allowed a forced mate — losing against stronger play."
        elif cls == "blunder":
            base = f"Major drop in evaluation (~{round(cpl/100, 1)} pawns) — likely hung material."
        elif cls == "mistake":
            base = f"Lost material or initiative (~{round(cpl/100, 1)} pawns)."
        elif cls == "inaccuracy":
            base = "A sharper plan was available."
        else:
            base = ""

        # If we know the engine's preferred move, append it. We don't try to
        # explain *why* the engine prefers it (that needs PV analysis); we
        # just name it.
        if best:
            base = (base + f" Engine preferred {best}.").strip()
        return base

    async def async_dismiss_review(self) -> None:
        """Return-to-menu service.

        Clears the lichess_active and lichess_review_ready flags, cancels
        any still-running Lichess stream task, and forgets the current
        game ID. This is invoked by the dashboard's "Back to modes" button
        from EITHER the rich in-game view OR the post-game review view —
        either way the user is asking to get back to the picker.

        We don't touch the underlying review payload (top_mistakes,
        accuracy, move_history) — those stay populated in the sensor
        attributes so they can still be inspected after dismissal.
        """
        self._state["lichess_active"] = False
        self._state["lichess_review_ready"] = False
        # If the Lichess stream task is alive, cancel it. The next
        # async_start_game call will spawn a fresh one.
        if self._lichess_task is not None and not self._lichess_task.done():
            try:
                self._lichess_task.cancel()
            except Exception:
                pass
            self._lichess_task = None
        self._game_id = None
        self.async_set_updated_data(dict(self._state))

    async def async_request_hint(self) -> None:
        """Refresh the engine recommendation for the current position.

        Service handler for phantom_chess.request_hint. The dashboard's Hint
        tile already shows best_move_san pulled from the running stream
        analysis — this service forces a re-fetch of the cloud-eval for the
        position-as-currently-known, useful when the cache has changed or
        when the dashboard wants a deliberate refresh.
        """
        if self._analysis_client is None:
            return
        try:
            ev = await self._analysis_client.get_eval(self._board.fen())
            if ev is None:
                _LOGGER.info("Hint: no cloud-eval data for current position")
                return
            self._state["eval_cp"] = ev.cp
            self._state["eval_mate"] = ev.mate
            self._state["eval_depth"] = ev.depth
            self._state["eval_source"] = ev.source
            self._state["best_move_san"] = None
            if ev.best_uci:
                try:
                    m = chess.Move.from_uci(ev.best_uci)
                    if m in self._board.legal_moves:
                        self._state["best_move_san"] = self._board.san(m)
                except (ValueError, chess.IllegalMoveError):
                    pass
            self.async_set_updated_data(dict(self._state))
        except Exception as err:
            _LOGGER.debug("Hint request failed: %s", err)

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

        session = async_get_clientsession(self.hass)
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

    # ── Local AI game (no Lichess required) ──────────────────────────────────

    async def async_start_local_game(self) -> None:
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
                auto_correct_wrong_move=True, advanced_capture=False, strict_gameplay=False,
            )
        except Exception as _ga_err:
            _LOGGER.warning("Local game: GAME_ASSISTANCE write failed: %s", _ga_err)

        # Announce game start via TTS.
        you_color = "White" if self._our_color == chess.WHITE else "Black"
        self.hass.async_create_task(self._announce_via_tts(
            f"Starting local Stockfish game at level {self.ai_level}. You play {you_color}."
        ))

        # Activation sequence. The matrix-`side` letter ("W"/"B") is who moves
        # first by color. The SIDE *opcode* ("0"/"1"/"2") is whether the board
        # (human) or BLE (AI) side is doing that move. If user is white,
        # they move first → SIDE "1". If user is black, AI moves first → "2".
        side_letter = "W"  # white always moves first in a fresh game
        side_opcode = "1" if self._our_color == chess.WHITE else "2"
        await self.async_phantom_start_game(side=side_letter, side_opcode=side_opcode)

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

    def _record_and_analyze_local_move(self, move: chess.Move, mover_is_white: bool) -> None:
        """Fire the analysis pipeline for a single local-game move.

        Mirrors what _process_move_list does inline for each Lichess move:
        records a history stub, snapshots the analysis board before/after,
        and schedules the async _analyze_move task. Without this, local
        Stockfish games render an empty rich-learning-view shell — the
        move history stays blank, eval bar stays null, classifications
        never fire. Added 2026-05-16 (Task #9).
        """
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
        self.hass.async_create_task(
            self._analyze_move(
                ply_index,
                board_before,
                board_after,
                move,
                mover_is_white,
            ),
            name=f"{DOMAIN}_analyze_local_ply_{ply_index}",
        )

    async def _push_move_to_local_ai(self, uci: str) -> None:
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

        if self._board.is_checkmate():
            self._state["game_status"] = STATUS_CHECKMATE
            self._local_game_active = False
            self._state["local_game_active"] = False
            self.async_set_updated_data(self._state)
            return
        elif self._board.is_stalemate() or self._board.is_insufficient_material():
            self._state["game_status"] = STATUS_DRAW
            self._local_game_active = False
            self._state["local_game_active"] = False
            self.async_set_updated_data(self._state)
            return

        self._state["game_status"] = STATUS_PLAYING
        self.async_set_updated_data(self._state)

        # Schedule AI response via the serialized replacement helper
        # (audit §1.4) so a concurrent dashboard / discovery-callback
        # path can't end up running two AI turns at once.
        await self._replace_local_game_task(name=f"{DOMAIN}_local_ai")

    async def _replace_local_game_task(self, *, name: str) -> None:
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

    async def _local_ai_turn(self) -> None:
        """Calculate and execute the AI's response move.

        Routes the engine's move through async_phantom_apply_ai_move so the
        magnet physically moves the piece on the proven cc68a66e path.
        apply_ai_move handles both the BLE writes and the self._board push,
        so this function does NOT push the move itself — it only computes,
        validates legality, dispatches, and then reads game-end state from
        the post-push board.
        """
        await asyncio.sleep(0.5)  # Brief pause so board can settle
        ai_uci = await self._get_ai_move(self._board)
        if not ai_uci:
            _LOGGER.error("Local AI: failed to get AI move")
            return

        try:
            move = chess.Move.from_uci(ai_uci)
        except ValueError:
            _LOGGER.error("Local AI: AI returned invalid UCI %s", ai_uci)
            return

        if move not in self._board.legal_moves:
            _LOGGER.error("Local AI: AI returned illegal move %s", ai_uci)
            return

        # Dispatch via the canonical AI-move path. apply_ai_move issues the
        # BLE triplet (movementVerify → side → movement), pushes the move onto
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
        move_landed = False
        try:
            await self.async_phantom_apply_ai_move(ai_uci)
            # apply_ai_move pushes on success.
            move_landed = True
        except Exception as err:
            _LOGGER.warning("Local AI: apply_ai_move raised: %s", err)
            # Fall back to local-only push so the game continues in HA even if
            # the magnet didn't fire. The user will see drift between HA state
            # and physical board, but the game loop won't deadlock. Only
            # mark the move as landed if the push actually succeeded — if
            # the move isn't legal in the current self._board (race with
            # discovery callback), neither apply nor fallback ran, and the
            # post-push bookkeeping must be skipped.
            if move in self._board.legal_moves:
                self._board.push(move)
                self._state["last_move"] = ai_uci
                move_landed = True
            else:
                _LOGGER.warning(
                    "Local AI: fallback push of %s skipped — not legal in "
                    "current board (%s). Skipping analysis + game-end "
                    "derivation to avoid corrupt state.",
                    ai_uci, self._board.fen(),
                )

        if not move_landed:
            # Don't run analysis or derive game_status from a board state
            # that doesn't contain the AI move. Just push whatever state
            # already exists so the UI doesn't go stale, then bail.
            self.async_set_updated_data(dict(self._state))
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

        # Derive game-end status from the now-post-push board state.
        if self._board.is_checkmate():
            self._state["game_status"] = STATUS_CHECKMATE
            self._local_game_active = False
            self._state["local_game_active"] = False
        elif self._board.is_stalemate() or self._board.is_insufficient_material():
            self._state["game_status"] = STATUS_DRAW
            self._local_game_active = False
            self._state["local_game_active"] = False
        elif self._board.is_check():
            self._state["game_status"] = "check"
        else:
            self._state["game_status"] = STATUS_PLAYING

        self.async_set_updated_data(dict(self._state))

    async def _get_ai_move(self, board: chess.Board) -> str | None:
        """Get best move for a local-Stockfish game.

        Cascade:
          1. Local Stockfish via LichessAnalysisClient.best_move_for_ai_level —
             handles libc-aware download, ARM support, engine lifecycle.
          2. Lichess cloud eval (free, anonymous, internet required).
          3. Random legal move (last-resort no-op).

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
                uci = await self._analysis_client.best_move_for_ai_level(
                    board, self.ai_level
                )
                if uci:
                    _LOGGER.debug(
                        "AI move via local Stockfish (level %d): %s",
                        self.ai_level, uci,
                    )
                    return uci
            except Exception as sf_err:
                _LOGGER.warning("Local Stockfish move failed: %s — falling back", sf_err)

        # 2. Try Lichess cloud eval (no auth required for cloud-eval endpoint).
        try:
            session = async_get_clientsession(self.hass)
            url = f"https://lichess.org/api/cloud-eval?fen={fen}&multiPv=1"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pvs = data.get("pvs", [])
                    if pvs:
                        moves = pvs[0].get("moves", "").split()
                        if moves:
                            _LOGGER.debug("AI move via Lichess cloud eval: %s", moves[0])
                            return moves[0]
        except Exception as ce_err:
            _LOGGER.warning("Lichess cloud eval failed: %s — using random", ce_err)

        # 3. Fall back to random legal move (always works; gameplay is
        # weak but the game-loop doesn't deadlock).
        legal = list(board.legal_moves)
        if legal:
            chosen = random.choice(legal)
            _LOGGER.debug("AI move via random: %s", chosen.uci())
            return chosen.uci()

        return None

    async def async_stop_local_game(self) -> None:
        """Stop the local AI game and return board to idle.

        Also clears `_ai_vs_ai_active` so the AI-vs-AI loop halts on
        its next iteration.
        """
        self._local_game_active = False
        self._ai_vs_ai_active = False
        if self._local_game_task and not self._local_game_task.done():
            self._local_game_task.cancel()
        self._state["game_status"] = STATUS_IDLE
        self._state["lichess_game_id"] = None
        self._state["local_game_active"] = False  # Task #9
        self._game_id = None
        self.async_set_updated_data(self._state)
        try:
            await self._ble_write(UUID_SELECT_MODE, b"3")  # pause/idle mode
        except Exception:
            pass

    async def async_start_ai_vs_ai_game(
        self,
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
                advanced_capture=False, strict_gameplay=False,
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

    async def _ai_vs_ai_await_reconnect(self, timeout: float = 30.0) -> bool:
        """Block (bounded) until the BLE maintain loop restores the link.

        AI-vs-AI pushes a full GAME_START snapshot every ply, keeping the
        steppers near-continuously active; under that load the board's BLE
        link occasionally drops mid-game (observed ply-9 disconnect,
        2026-05-31). ``_ble_loop`` reconnects on its own; this helper just
        waits for ``_ble_connected`` to come back so the spectator game can
        resume instead of dying on the first transient blip.

        Honors ``_ai_vs_ai_active`` for prompt shutdown. On reconnect it
        waits a short settle so the board can finish connect-time service
        discovery before the caller writes the re-drive snapshot. Returns
        True once reconnected (and still active), False on timeout or if the
        game was stopped while waiting.
        """
        deadline = self.hass.loop.time() + timeout
        while self.hass.loop.time() < deadline:
            if not self._ai_vs_ai_active:
                return False
            if self._ble_connected:
                await asyncio.sleep(2.0)
                return self._ble_connected and self._ai_vs_ai_active
            await asyncio.sleep(1.0)
        return False

    async def _ai_vs_ai_loop(self) -> None:
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
            await asyncio.sleep(self._ai_vs_ai_move_delay)

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

                # Apply via the canonical AI-move path so the snapshot
                # protocol + echo suppression + analysis pipeline all
                # behave identically to a normal local game.
                try:
                    await self.async_phantom_apply_ai_move(uci)
                except Exception as err:
                    # A transient BLE drop (common under sustained stepper
                    # load) shouldn't kill the whole spectator game. Wait for
                    # the maintain loop to reconnect, then RE-DRIVE the current
                    # position. We must NOT re-call apply_ai_move: it already
                    # pushed this move onto self._board before the failed write
                    # and does not roll back (see its docstring), so a second
                    # call would find the move illegal-because-already-played
                    # and no-op, leaving the physical board a move behind.
                    # _phantom_execute_position drives the magnet to an
                    # absolute target FEN (snapshot model, idempotent), so
                    # re-driving self._board.fen() reproduces exactly what the
                    # failed apply_ai_move would have done.
                    _LOGGER.warning(
                        "AI-vs-AI: apply_ai_move raised %s at ply %d; "
                        "waiting for reconnect to re-drive",
                        err, len(self._board.move_stack),
                    )
                    if not await self._ai_vs_ai_await_reconnect():
                        _LOGGER.warning(
                            "AI-vs-AI: board did not reconnect; stopping loop"
                        )
                        break
                    try:
                        ok = await self._phantom_execute_position(
                            fen=self._board.fen(),
                            side="W" if self._our_color == chess.WHITE else "B",
                            timeout_s=30.0,
                            side_opcode="1",
                        )
                        if not ok:
                            _LOGGER.warning(
                                "AI-vs-AI: re-drive after reconnect timed out "
                                "at ply %d; continuing",
                                len(self._board.move_stack),
                            )
                    except Exception as err2:
                        _LOGGER.warning(
                            "AI-vs-AI: re-drive after reconnect failed (%s) at "
                            "ply %d; stopping loop",
                            err2, len(self._board.move_stack),
                        )
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

                # Brief settle gap before the next move.
                await asyncio.sleep(self._ai_vs_ai_move_delay)

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

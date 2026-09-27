"""DataUpdateCoordinator for Phantom Chess Board — BLE ↔ Lichess/local-AI bridge."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .game_review import ReviewManager
    from .drill_mode import DrillSession
    from .puzzles import PuzzleSession
    from .lichess_analysis import LichessAnalysisClient

import aiohttp
import chess

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.issue_registry import async_delete_issue
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .drill_mode import DrillModeMixin
from .puzzle_mode import PuzzleModeMixin
from .sessions import LocalSessionMixin
from . import autoplay
from . import coaching
from . import local_game
from . import online_session
from . import protocol
from . import two_player
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
    AI_ECHO_BACKSTOP_SECONDS,
    AI_ECHO_MOVE_DONE_GRACE_SECONDS,
    BLE_MAX_RETRY_SECONDS,
    BLE_RETRY_SECONDS,
    CLASSIFICATION_UNKNOWN,
    CONF_BLE_ADDRESS,
    CONF_LICHESS_TOKEN,
    DOMAIN,
    MODE_CHESS_PLAY,
    MOVE_DEDUP_WINDOW_SECONDS,
    PENDING_FRAME_MAX_AGE_SECONDS,
    POST_GAME_MIN_ANALYZED_FRACTION,
    SETTLE_MODE_TRIM_SECONDS,
    STATUS_IDLE,
    STATUS_PAUSED,
    STATUS_PLAYING,
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


class PhantomChessCoordinator(DrillModeMixin, PuzzleModeMixin, LocalSessionMixin, DataUpdateCoordinator[dict[str, Any]]):
    """Manages BLE connection to the Phantom board and the Lichess Board API game."""

    # ── Methods defined in session modules (bound here; see each module) ──
    # protocol.py
    _ble_write = protocol._ble_write
    async_debug_ble_write = protocol.async_debug_ble_write
    _game_channel_write_diag = protocol._game_channel_write_diag
    _game_start_length_error = protocol._game_start_length_error
    async_diagnose_game_start = protocol.async_diagnose_game_start
    _diagnose_game_start_variants = protocol._diagnose_game_start_variants
    _phantom_send_game_start = protocol._phantom_send_game_start
    _set_route_issue = protocol._set_route_issue
    _phantom_send_side = protocol._phantom_send_side
    _phantom_send_movement_verify = protocol._phantom_send_movement_verify
    _phantom_send_game_end = protocol._phantom_send_game_end
    _phantom_send_game_assistance = protocol._phantom_send_game_assistance
    _phantom_send_check_sound = protocol._phantom_send_check_sound
    _phantom_send_reset_detection = protocol._phantom_send_reset_detection
    _phantom_drop_to_home = protocol._phantom_drop_to_home
    _phantom_execute_position = protocol._phantom_execute_position
    _execute_position_unlocked = protocol._execute_position_unlocked
    async_move_piece = protocol.async_move_piece
    _phantom_send_ai_move = protocol._phantom_send_ai_move
    _fw_at_least = protocol._fw_at_least
    _phantom_select_chess_play_mode = protocol._phantom_select_chess_play_mode
    async_phantom_start_game = protocol.async_phantom_start_game
    async_phantom_apply_ai_move = protocol.async_phantom_apply_ai_move
    # online_session.py
    async_start_game = online_session.async_start_game
    _async_start_online_game = online_session._async_start_online_game
    _lichess_task_done_cb = online_session._lichess_task_done_cb
    async_reconcile_lichess_state = online_session.async_reconcile_lichess_state
    async_resume_from_phone = online_session.async_resume_from_phone
    async_start_lichess_configured = online_session.async_start_lichess_configured
    _post_resign_once = online_session._post_resign_once
    _clear_lichess_game_session = online_session._clear_lichess_game_session
    async_resign = online_session.async_resign
    _lichess_stream_loop = online_session._lichess_stream_loop
    _handle_lichess_event = online_session._handle_lichess_event
    _on_game_full = online_session._on_game_full
    _on_game_state = online_session._on_game_state
    _on_game_finish = online_session._on_game_finish
    _update_clocks_from_event = online_session._update_clocks_from_event
    _process_move_list = online_session._process_move_list
    _drain_physical_move_queue = online_session._drain_physical_move_queue
    _push_move_to_lichess = online_session._push_move_to_lichess
    # autoplay.py
    _load_sculpture_games_blocking = autoplay._load_sculpture_games_blocking
    _async_get_sculpture_games = autoplay._async_get_sculpture_games
    async_play_selected_sculpture = autoplay.async_play_selected_sculpture
    _async_play_selected_sculpture = autoplay._async_play_selected_sculpture
    _sculpture_loop = autoplay._sculpture_loop
    async_start_sculpture = autoplay.async_start_sculpture
    _async_start_sculpture = autoplay._async_start_sculpture
    async_start_ai_vs_ai_game = autoplay.async_start_ai_vs_ai_game
    _async_start_ai_vs_ai_game = autoplay._async_start_ai_vs_ai_game
    _ai_vs_ai_await_reconnect = autoplay._ai_vs_ai_await_reconnect
    _notify_wedge_circuit_breaker = autoplay._notify_wedge_circuit_breaker
    _ai_vs_ai_loop = autoplay._ai_vs_ai_loop
    # coaching.py
    _build_move_speech = coaching._build_move_speech
    _post_move_event_speech = coaching._post_move_event_speech
    _announce_via_tts = coaching._announce_via_tts
    _should_announce_active_game = coaching._should_announce_active_game
    _record_history_stub = coaching._record_history_stub
    _analyze_starting_position = coaching._analyze_starting_position
    _analyze_move = coaching._analyze_move
    _maybe_announce_classification = coaching._maybe_announce_classification
    _build_post_game_review = coaching._build_post_game_review
    async_dismiss_review = coaching.async_dismiss_review
    async_request_hint = coaching.async_request_hint
    # two_player.py
    async_start_two_player_game = two_player.async_start_two_player_game
    _async_start_two_player_game = two_player._async_start_two_player_game
    _flag_two_player_out_of_sync = two_player._flag_two_player_out_of_sync
    _clear_two_player_out_of_sync = two_player._clear_two_player_out_of_sync
    async_resync_two_player = two_player.async_resync_two_player
    _finalize_two_player_game = two_player._finalize_two_player_game
    _save_two_player_pgn = two_player._save_two_player_pgn
    # local_game.py
    async_start_local_game = local_game.async_start_local_game
    _async_start_local_game = local_game._async_start_local_game
    _record_and_analyze_local_move = local_game._record_and_analyze_local_move
    _push_move_to_local_ai = local_game._push_move_to_local_ai
    _replace_local_game_task = local_game._replace_local_game_task
    _local_ai_turn = local_game._local_ai_turn
    _finish_local_game = local_game._finish_local_game
    _get_ai_move = local_game._get_ai_move
    async_stop_local_game = local_game.async_stop_local_game


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
        # Blank for local-only entries; online play is refused with guidance.
        self._lichess_token: str = entry_data.get(CONF_LICHESS_TOKEN) or ""

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
            DEFAULT_VOICE_ANNOUNCEMENTS,
            DEFAULT_STUDY_VIEW,
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
        # v0.4-beta3: master mute for the HA-side play-by-play TTS. When
        # False, _announce_via_tts skips the direct tts.speak call (the
        # spoken voiceover) while still firing the phantom_chess_announce
        # event so event-driven automations can decide for themselves.
        self.voice_announcements: bool = DEFAULT_VOICE_ANNOUNCEMENTS
        # Study-mode display toggle (Luke, 2026-07-08). DISPLAY-only: gates
        # whether the dashboard renders the full-width board ("board status")
        # or the rich learning layout, across every active-game mode. No
        # gameplay behaviour hangs off it — the switch entity flips this and
        # the dashboard conditionals read it. Default False (board-only base
        # state); RestoreEntity makes the user's choice sticky.
        self.study_view: bool = DEFAULT_STUDY_VIEW
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
        # v0.4-beta2: two-human recording mode (board in SIDE-0 2-local-player).
        self._two_player_active: bool = False
        self._local_game_task: asyncio.Task | None = None
        self._local_start_lock = asyncio.Lock()
        self._physical_operation_lock = asyncio.Lock()
        self._play_revision = 0
        self._reviews: ReviewManager | None = None
        self._library = None
        self._saved_game_id = None
        self._saved_revision = 0
        self._journal_tasks = set()
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
        # Sculpture playback: the integration drives a specific historic
        # game move-by-move over the SAME snapshot protocol AI-vs-AI uses
        # (chess-play mode, SELECT_MODE 2), then STOPS — exactly one game,
        # no firmware playlist loop. `_sculpture_active` gates the loop so
        # stop_local_game / back_to_modes can halt it cleanly. Move data is
        # bundled in sculpture_games.json (keyed by const.SCULPTURE_GAMES)
        # and lazy-loaded into `_sculpture_games_cache` on first use.
        self._sculpture_active: bool = False
        self._sculpture_move_delay: float = 2.0
        # Puzzle mode (puzzle_mode.py): the active or last-finished puzzle.
        self._puzzle: PuzzleSession | None = None
        # Endgame drill mode (drill_mode.py): the active or last-finished drill.
        self._drill: DrillSession | None = None
        self._sculpture_games_cache: dict | None = None
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
        # Fix A3 (2026-07-08): a `(payload_str, loop.time())` tuple for the most
        # recent human move frame that was SUPPRESSED by the settle window, or
        # None. Held so the frame can be REPLAYED through the full apply path once
        # the window clears (0x0c) or trims to expiry (Board Playing / execute
        # timeout). Without this, a human move made during the dead zone vanished
        # (live c4-d5, 2026-07-08). One-shot: only the latest suppressed frame is
        # kept; replayed frames older than PENDING_FRAME_MAX_AGE_SECONDS are
        # discarded. Loop-affine — only touched on the marshalled apply path.
        self._pending_settle_frame: tuple[str, float] | None = None
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
        # M9: loop-time deadline after which the AI echo set is cleared. Armed
        # (to now + AI_ECHO_MOVE_DONE_GRACE_SECONDS) when BLE_MOVE_DONE resolves
        # the move future — the natural "magnet sequence complete" boundary —
        # and consumed lazily by `_is_ai_echo`. 0.0 = disarmed. The grace lets a
        # castle's trailing rook echo (which can land just after BLE_MOVE_DONE)
        # stay suppressed. See AI_ECHO_MOVE_DONE_GRACE_SECONDS.
        self._ai_echo_move_done_expire_at: float = 0.0
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

        # M2: the UCI + loop-monotonic timestamp of the most-recent APPLIED
        # human move (set in the discovery move-apply path). Used to drop a
        # firmware double-fire — a second `\x03M` for one physical slide that
        # resolves to the same UCI (or its 180° rotation) within
        # MOVE_DEDUP_WINDOW_SECONDS. Only human-move applies write these;
        # AI moves go through `async_phantom_apply_ai_move` and are handled by
        # the echo set instead.
        self._last_applied_move_uci: str | None = None
        self._last_applied_move_ts: float = 0.0

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
        # One-shot guard so the "0.3.2 diag" line (negotiated MTU + UUID_GAME
        # write limits) is emitted at INFO once per BLE session on the first
        # GAME_START, then at DEBUG thereafter. Reset on each connect. Added
        # 2026-06-14 to disambiguate the fw0.3.2 GAME_START length rejection.
        self._game_start_diag_logged: bool = False
        # One-shot guard so the fw0.3.2 "CCCD subscribe disallowed; using poll
        # fallback" note is logged once (not on every reconnect).
        self._subscribe_degraded_logged: set[str] = set()

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
                # B1 thread-safety: _handle_matrix_bytes runs on the bleak
                # notify thread, but `async_add_executor_job` is a loop-thread-
                # only API (it touches the loop's executor + creates a Future).
                # Marshal the scheduling onto the loop; the file write itself
                # still runs in the executor. Fire-and-forget — the debug log is
                # best-effort, so the returned Future is intentionally dropped.
                self.hass.loop.call_soon_threadsafe(
                    self.hass.async_add_executor_job, self._append_matrix_log, line
                )
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
        from datetime import datetime, timezone

        # Error/status payload with no parseable matrix (e.g. the
        # "Chessboard and sensor matrix do not match" wedge sometimes arrives
        # with no usable trailing grid). Surface the status + message so the
        # user/dashboard can see the board is in an error state, but skip all
        # the grid-dependent computation (FEN, consistency, piece count,
        # mismatch diff) since there's no grid to work with. v0.4-beta3.
        if parsed.get("piece_grid") is None:
            if (self._state.get("matrix_status") == parsed["status"]
                    and self._state.get("matrix_status_message")
                    == parsed["status_message"]):
                return
            self._state["matrix_raw"] = parsed["raw"]
            self._state["matrix_status"] = parsed["status"]
            self._state["matrix_status_message"] = parsed["status_message"]
            self._state["matrix_last_updated"] = datetime.now(timezone.utc).isoformat()
            self.async_set_updated_data(dict(self._state))
            return

        # Dedup check (now on loop thread, no read race).
        if (self._state.get("piece_grid") == parsed["piece_grid"]
                and self._state.get("sensor_bitmap") == parsed["sensor_bitmap"]
                and self._state.get("matrix_status") == parsed["status"]
                and self._state.get("matrix_status_message") == parsed["status_message"]):
            return
        fen_board = _grid_to_fen(parsed["piece_grid"])
        consistent, mismatches = _check_consistency(
            parsed["piece_grid"], parsed["sensor_bitmap"]
        )
        # Count actual pieces (non-dot characters in the 100-char grid).
        piece_count = sum(1 for c in parsed["piece_grid"] if c != ".")
        self._state["matrix_raw"] = parsed["raw"]
        self._state["piece_grid"] = parsed["piece_grid"]
        self._state["sensor_bitmap"] = parsed["sensor_bitmap"]
        # fen_board is None when the grid is valid but not FEN-expressible
        # (fw0.3.2 'X'/'Z' promoted-pawn markers, doc §9.2 — side mapping
        # undocumented). Keep the last-known-good FEN rather than clearing
        # or corrupting the live board view.
        if fen_board is not None:
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
        # Fix A2: when the firmware itself declares play-readiness, trim the
        # (600s) settle window rather than waiting out the 0x0c that may never
        # come. This runs on the loop (marshalled from _handle_firmware_mode_bytes),
        # so the loop-affine _activation_settle_until read/write is safe. Trim —
        # do NOT clear to zero: the 2026-05-25 spurious `M 1 e8-g8` arrived DURING
        # activation, BEFORE any Board Playing transition, so a short post-transition
        # grace still suppresses that class while releasing genuine human moves.
        if (
            text in ("Board Playing", "BLE Playing")
            and self._activation_settle_until > self.hass.loop.time()
        ):
            self._activation_settle_until = min(
                self._activation_settle_until,
                self.hass.loop.time() + SETTLE_MODE_TRIM_SECONDS,
            )
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
                    # fw0.3.2 battery fallback: the BATTERY_INFO CCCD subscribe
                    # is rejected, so poll-read it here (doc §5.7: battery is a
                    # read-only characteristic). Harmless on 0.3.0 where notify
                    # also works. Runs on the loop, so apply state directly.
                    try:
                        data = await client.read_gatt_char(UUID_BATTERY_INFO)
                        parsed = self._parse_battery_payload(bytes(data))
                        if parsed is not None:
                            self._apply_battery_state(*parsed)
                    except Exception as err:
                        _LOGGER.debug("battery poll read failed: %s", err)
                await rt._sleep(2)
            except asyncio.CancelledError:
                return
            except Exception as err:
                _LOGGER.debug("matrix_poll_loop error: %s", err)
                await rt._sleep(2)

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
            "two_player_active": False,
            "two_player_out_of_sync": False,
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
        from homeassistant.helpers.storage import Store
        from .game_library import GameLibrary
        self._library = GameLibrary(Store(self.hass, 1, f"{DOMAIN}_games_{self._ble_address.replace(':', '').lower()}"))
        await self._library.load()
        self._refresh_library_state()
        # Lichess analysis client (cloud-eval + opening explorer +
        # local Stockfish fallback). Created here because the client
        # acquires the HA aiohttp session lazily; instantiating it during
        # __init__ is fine but its first call needs the event loop.
        # Stockfish binary is cached in /config/phantom_chess/bin/ so it
        # survives integration reloads. First evaluate() call after a fresh
        # install triggers a one-time platform-specific engine download.
        from pathlib import Path
        from .lichess_analysis import LichessAnalysisClient
        sf_bin_dir = Path(self.hass.config.path("phantom_chess")) / "bin"
        self._analysis_client = LichessAnalysisClient(
            self.hass, stockfish_bin_dir=sf_bin_dir
        )
        entry = getattr(self, "_entry", None)
        self._analysis_client.allow_cloud = bool(
            ((entry.options if entry is not None else {}) or {}).get("cloud_analysis", True)
        )
        from .game_review import ReviewManager
        assert self._analysis_client._stockfish is not None
        self._analysis_client._stockfish.status_callback = self._publish_engine_state
        self._reviews = ReviewManager(
            Store(self.hass, 1, f"{DOMAIN}_reviews_{self._ble_address.replace(':', '').lower()}"),
            self._library, self._analysis_client._stockfish.evaluate,
            lambda: bool(self._local_start_lock.locked() or self._local_game_active or self._game_id
                         or self._two_player_active or self._ai_vs_ai_active or self._sculpture_active),
            self._publish_review_state,
        )
        await self._reviews.load()
        self._ble_task = self.hass.loop.create_task(
            self._ble_loop(), name=f"{DOMAIN}_ble"
        )

    async def async_shutdown(self) -> None:
        """Stop background tasks — called from __init__.async_unload_entry."""
        self._stop_event.set()
        if self._reviews is not None:
            await self._reviews.cancel()
        self.paused = True
        self._play_revision += 1
        if self._local_game_task and not self._local_game_task.done():
            self._local_game_task.cancel()
            try:
                await self._local_game_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._local_game_active:
            try:
                await self.async_checkpoint("uncertain" if self._state.get("physical_operation") == "uncertain" else "paused")
            except (ValueError, OSError):
                pass  # Saving already reports its failure; shutdown must still clean up.
        await self._flush_journal()
        self._stop_event.set()
        self._local_game_active = False
        self._ai_vs_ai_active = False
        self._sculpture_active = False
        self._two_player_active = False
        for task in (self._local_game_task, self._ble_task, self._lichess_task, self._matrix_poll_task):
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
        """Called by the coordinator on its polling interval — return cached state.

        B7: return a snapshot copy, not the live mutable dict, so
        ``coordinator.data`` is a stable value between pushes and entities
        (e.g. image.py's change-detection) never observe a half-mutated state.
        """
        return dict(self._state)

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
                self.async_set_updated_data(dict(self._state))

            await rt._sleep(retry_delay)
            retry_delay = min(retry_delay * 2, BLE_MAX_RETRY_SECONDS)

    def _apply_move_frame(
        self, payload_str: str, u: str, client: Any, opcode_byte: int | None
    ) -> None:
        """Apply one movementVerify move frame. MUST run on the event loop.

        Extracted from the discovery callback's ``_apply_move`` closure so a
        frame stashed during the post-activation settle window can be REPLAYED
        through the identical path (echo -> settle -> reset-mode -> dedup ->
        legality). ``u``/``client``/``opcode_byte`` are the ambient discovery
        context; the replay reuses the current callback's values (same GATT
        UUID + BLE client), so only ``payload_str`` needs stashing.
        """
        if self.paused or self._stop_event.is_set():
            return
        # A3 replay: before handling THIS frame, flush any frame that was
        # stashed while the settle window was armed and is now releasable —
        # covers the case where BLE_MOVE_DONE (0x0c) never arrived but the
        # window has since trimmed to expiry (the live c4-d5 dead zone).
        self._maybe_replay_pending_settle_frame(u, client, opcode_byte)
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
            # A3 pending-frame replay: stash the suppressed frame so it can be
            # re-applied when the window clears/trims to expiry (0x0c handler or
            # the lazy check at the top of this method). Live 2026-07-08: the
            # 0x0c BLE_MOVE_DONE for the c4-d5 drive never arrived, so the 600s
            # backstop ate the human's frame outright — it must not vanish.
            # One-shot: only the most recent suppressed frame is held.
            self._pending_settle_frame = (payload_str, self.hass.loop.time())
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
        # M2: drop a firmware double-fire. A slow
        # physical slide can emit a SECOND `\x03M`
        # for the SAME move — sometimes as a distinct
        # placement string that is legal in the new
        # position and would be applied as a phantom
        # second move. If this frame resolves to the
        # last APPLIED move's UCI (or its 180°
        # rotation) within MOVE_DEDUP_WINDOW_SECONDS,
        # it's a refire — drop it WITHOUT touching
        # firmware_last_move. Distinct moves inside
        # the window (blitz premoves) fall through.
        # Echo suppression already ran above, so this
        # only handles the HUMAN-move double-fire.
        if self._is_double_fire_refire(payload_str):
            _LOGGER.debug(
                "DISCOVERY: dropping double-fire refire on uuid=%s  "
                "payload=%r (matches last applied move=%s within %.0fms)",
                u, payload_str, self._last_applied_move_uci,
                MOVE_DEDUP_WINDOW_SECONDS * 1000,
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
                    # M2: remember this applied move so
                    # a firmware double-fire arriving in
                    # the next MOVE_DEDUP_WINDOW_SECONDS
                    # is recognised as a refire.
                    self._last_applied_move_uci = uci
                    self._last_applied_move_ts = self.hass.loop.time()
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
                        self._record_and_analyze_local_move(mv, not self._board.turn)
                        if self._board.is_game_over():
                            self._finish_local_game()
                            return
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
                    elif self._two_player_active:
                        # v0.4-beta2 two-player recording: analyze the just-pushed move
                        # (eval meter, classification glyphs, move history via the shared
                        # learning-view pipeline) and check for game end. No AI or Lichess
                        # response — both sides are humans moving the physical pieces.
                        _mover_white = (self._board.turn == chess.BLACK)
                        self.hass.loop.call_soon_threadsafe(
                            self._record_and_analyze_local_move, mv, _mover_white
                        )
                        if self._board.is_game_over():
                            self.hass.loop.call_soon_threadsafe(
                                lambda: self.hass.loop.create_task(
                                    self._finalize_two_player_game(),
                                    name=f"{DOMAIN}_two_player_finalize",
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
                    # v0.4-beta2: in two-player recording an
                    # illegal physical move was silently dropped
                    # (2026-06-03 live-test gap — the player's
                    # later checkmate never registered because
                    # self._board had stalled). Surface it so the
                    # player knows to undo the piece, and offer
                    # the resync action.
                    if self._two_player_active:
                        self.hass.loop.call_soon_threadsafe(
                            self._flag_two_player_out_of_sync,
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
                    UUID_GAME, b"\x031", response=True
                )
                _LOGGER.debug("DISCOVERY: movementVerify ack sent for %r", move_str)
            except Exception as ack_err:
                _LOGGER.warning("DISCOVERY: movementVerify ack failed: %s", ack_err)
        self.hass.loop.call_soon_threadsafe(
            lambda: self.hass.loop.create_task(_ack())
        )

    def _maybe_replay_pending_settle_frame(
        self, u: str, client: Any, opcode_byte: int | None
    ) -> None:
        """Replay a frame stashed during the settle window, if releasable.

        MUST run on the event loop. Fires from two release points (Fix A3): the
        0x0c BLE_MOVE_DONE handler (window cleared to 0) and lazily at the top of
        every ``_apply_move_frame`` (window trimmed to expiry). One-shot: the
        stash is cleared BEFORE the replay so the re-entrant ``_apply_move_frame``
        call sees ``None`` (no recursion) and, because the window is now clear,
        cannot re-stash it — guaranteeing a single board push. A frame older than
        ``PENDING_FRAME_MAX_AGE_SECONDS`` is discarded, not applied (a stale human
        intent shouldn't land minutes later).
        """
        pending = self._pending_settle_frame
        if pending is None:
            return
        # Only release once the window has genuinely cleared/trimmed to expiry;
        # while still armed, keep holding (a later real frame or 0x0c releases it).
        if self.hass.loop.time() < self._activation_settle_until:
            return
        payload_str, stashed_at = pending
        self._pending_settle_frame = None  # one-shot: clear before replay
        age = self.hass.loop.time() - stashed_at
        if age > PENDING_FRAME_MAX_AGE_SECONDS:
            _LOGGER.debug(
                "DISCOVERY: discarding stale pending settle frame %r (age %.1fs > %.0fs)",
                payload_str, age, PENDING_FRAME_MAX_AGE_SECONDS,
            )
            return
        _LOGGER.debug(
            "DISCOVERY: replaying pending settle frame %r (stashed %.1fs ago, window clear)",
            payload_str, age,
        )
        self._apply_move_frame(payload_str, u, client, opcode_byte)

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
            # Fresh session → re-arm the one-shot "0.3.2 diag" INFO line.
            self._game_start_diag_logged = False
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
                        # fw0.3.2: the CCCD subscribe for SEND_MATRIX and
                        # BATTERY_INFO is rejected with WRITE_NOT_PERMITTED, yet
                        # the data still reaches us — matrix via the UUID_GAME
                        # opcode-0x08 notify path AND the 2s poll loop; battery
                        # via the 2s poll-read fallback (see _matrix_poll_loop).
                        # So these two are non-fatal: log once at INFO (not on
                        # every reconnect) and never at WARNING.
                        if label in ("SEND_MATRIX", "BATTERY_INFO"):
                            if label not in self._subscribe_degraded_logged:
                                self._subscribe_degraded_logged.add(label)
                                _LOGGER.info(
                                    "Subscribe %s (%s) not permitted on this "
                                    "firmware (fw0.3.2 CCCD change) — degrading "
                                    "to poll/game-channel fallback: %s",
                                    label, uuid, sub_err,
                                )
                            else:
                                _LOGGER.debug(
                                    "Subscribe %s still not permitted (using "
                                    "fallback): %s", label, sub_err,
                                )
                        else:
                            _LOGGER.warning(
                                "Subscribe %s (%s) failed: %s", label, uuid, sub_err
                            )
                else:
                    _LOGGER.info("Characteristic %s not present on this firmware — skipping", label)

            # ── DISCOVERY MODE ────────────────────────────────────────────────
            # Read all readable characteristics and write to file (avoids log
            # deduplication). C1: this full GATT read sweep is a debug-only
            # diagnostic — gate the whole loop (not just the file write) on
            # debug_dump. In production it forced dozens of sequential GATT
            # round-trips through the ESPHome proxy on EVERY reconnect, exactly
            # when the link should come back fast (e.g. an AI-vs-AI mid-game
            # reconnect → re-drive).
            if self._debug_dump_enabled():
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

                            # M4: scope the raw-payload dedup to non-actionable
                            # chatter. Classify the frame BEFORE de-duplicating
                            # so a legitimately repeated move or a repeated
                            # BLE_MOVE_DONE (0x0c) is never swallowed here:
                            #  - move frames: a firmware double-fire is handled
                            #    downstream by the ~400 ms window (M2), which can
                            #    tell a refire from a legit back-to-back move;
                            #    last_seen cannot, and would also drop a real
                            #    repeated move (e.g. Ng1-f3-g1).
                            #  - 0x0c: each BLE_MOVE_DONE must reach its handler
                            #    (future resolve + settle/echo release); dropping
                            #    a repeat could starve a waiter.
                            # Everything else (HOME heartbeats, status/matrix
                            # chatter) is still de-duplicated as before.
                            _is_move = _is_move_frame(payload_str)
                            _is_move_done = (
                                u.lower() == UUID_GAME and opcode_byte == 0x0c
                            )
                            if not _is_move and not _is_move_done:
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
                            if _is_move_done:
                                # B1 thread-safety: bleak can deliver this 0x0c
                                # off the event loop. asyncio Futures are NOT
                                # thread-safe (a cross-thread set_result races
                                # `asyncio.wait_for` in _phantom_execute_position
                                # — lost wakeup / InvalidStateError), and both
                                # `_activation_settle_until` and
                                # `_ai_echo_move_done_expire_at` are loop-affine
                                # fields the on-loop M9 lazy-expiry logic reads.
                                # Marshal the whole handler onto the loop (same
                                # call_soon_threadsafe discipline as _apply_move
                                # below); FIFO ordering is preserved.
                                def _apply_move_done() -> None:
                                    if (
                                        self._move_done_future is not None
                                        and not self._move_done_future.done()
                                    ):
                                        self._move_done_future.set_result(True)
                                    if self._activation_settle_until > 0:
                                        self._activation_settle_until = 0.0
                                    # M9: BLE_MOVE_DONE is the authoritative
                                    # "magnet sequence complete" boundary. Arm a
                                    # short grace, then let `_is_ai_echo` clear
                                    # the AI echo set — so a genuinely new human
                                    # move matching a stale echo UCI stops being
                                    # suppressed once the AI's magnet motion is
                                    # done, instead of waiting out the (now
                                    # settle-aligned) time backstop. The grace
                                    # holds the set long enough for a castle's
                                    # trailing rook echo — its `\x03M` can land
                                    # just AFTER move-done — to stay suppressed.
                                    # Only arm while an echo set exists.
                                    if self._last_ai_echo_ucis:
                                        self._ai_echo_move_done_expire_at = (
                                            self.hass.loop.time()
                                            + AI_ECHO_MOVE_DONE_GRACE_SECONDS
                                        )
                                    # A3: the window is now clear — flush any human
                                    # move frame stashed while it was armed (the
                                    # g1-e2 class) through the full apply path.
                                    self._maybe_replay_pending_settle_frame(
                                        u, client, opcode_byte
                                    )
                                self.hass.loop.call_soon_threadsafe(_apply_move_done)

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
                                # B1 thread-safety: the live_fen write and the
                                # fan-out both belong on the loop (this branch
                                # runs on the bleak notify thread). AUDIT FIX
                                # (beta5 audit U1, 2026-07-08): read
                                # _last_target_fen at EXECUTION time, inside the
                                # marshalled closure — NOT snapshotted at
                                # schedule time. call_soon_threadsafe is FIFO,
                                # so when a move frame and a CLEAN: Match frame
                                # arrive back-to-back, _apply_move runs first
                                # and updates _last_target_fen to the post-move
                                # board; a schedule-time snapshot captured the
                                # STALE pre-move target and reverted live_fen
                                # (matrix pushes don't fire during Board
                                # Playing, so the revert persisted until the
                                # next move). B7: fan out a dict copy.
                                def _apply_clean_match() -> None:
                                    fen = self._last_target_fen
                                    if fen is None:
                                        return
                                    self._state["live_fen"] = fen
                                    self.async_set_updated_data(dict(self._state))
                                self.hass.loop.call_soon_threadsafe(_apply_clean_match)

                            # Detect physical moves. In firmware 0.3.0 on cc68a66e, the
                            # human-move notification looks like:
                            #     b"\x03M 1 e2-e4"
                            # — i.e. opcode 0x03 (movementVerify) prefix, then "M <n> <from>-<to>".
                            # `_is_move` was resolved above (via _is_move_frame) so the
                            # dedup gate and this apply branch share one definition (M4).
                            if _is_move:
                                # Suppress echoes of OUR last AI move using content-based
                                # detection. This replaces the old time-window kludge that
                                # incorrectly assumed the board had an internal AI —
                                # confirmed via Efraín's doc that the board has no chess
                                # intelligence, so every \\x03M notification is either an
                                # echo of our snapshot OR a real human move.
                                # M5 race fix (audit 2026-06-09 §M5): everything below reads and
                                # mutates self._board / self._state / self._last_target_fen —
                                # loop-affine objects. bleak can deliver notifications on a
                                # non-loop thread, so the whole apply (echo/settle/reset
                                # filtering, legality check, board push, state fan-out, ack) is
                                # marshalled onto the event loop, serializing it with entity
                                # reads and every other loop-side mutator — the same pattern
                                # _on_battery uses (audit §1.5). call_soon_threadsafe preserves
                                # FIFO order, so rapid successive moves apply in arrival order.
                                def _apply_move() -> None:
                                    self._apply_move_frame(
                                        payload_str, u, client, opcode_byte
                                    )
                                self.hass.loop.call_soon_threadsafe(_apply_move)
                        return _cb

                    try:
                        await client.start_notify(char.uuid, _make_discovery_cb(char.uuid))
                        _LOGGER.debug("DISCOVERY subscribed to %s (props: %s)", char.uuid, char.properties)
                    except Exception as sub_err:
                        _LOGGER.debug("DISCOVERY subscribe failed %s: %s", char.uuid, sub_err)
            # ── END DISCOVERY MODE ────────────────────────────────────────────

            self.async_set_updated_data(dict(self._state))

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
                    await rt._sleep(1)
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

    @staticmethod
    def _reject_move_done_future(fut: "asyncio.Future") -> None:
        """Reject a pending BLE_MOVE_DONE waiter — runs on the event loop.

        Re-checks ``done()`` because the future may have been resolved (by a
        late 0x0c handler) between the disconnect callback scheduling this and
        the loop running it. (B1)
        """
        if not fut.done():
            fut.set_exception(
                ConnectionError("BLE disconnected before BLE_MOVE_DONE")
            )

    def _on_ble_disconnect(self, client: BleakClient) -> None:
        self._ble_connected = False
        self._ble_client = None
        # Reset session flag so the next reconnect re-initialises via GAME_END.
        self._phantom_session_initialized = False
        # Cancel any pending BLE_MOVE_DONE waiter — it'll never arrive now.
        # B1: bleak disconnect callbacks are not guaranteed to run on the event
        # loop, and asyncio Futures are not thread-safe, so reject via the loop
        # (call_soon_threadsafe) rather than calling set_exception inline.
        fut = self._move_done_future
        if fut is not None and not fut.done():
            self.hass.loop.call_soon_threadsafe(self._reject_move_done_future, fut)
        _LOGGER.warning("Phantom board disconnected")
        self.hass.loop.call_soon_threadsafe(
            self.async_set_updated_data, dict(self._state)
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

    @staticmethod
    def _parse_battery_payload(data: bytes) -> tuple[int, bool] | None:
        """Parse 'percent,wallStatus,charging,doneCharging' → (percent, charging).

        Returns None on malformed/empty input. Shared by the notify callback
        (_on_battery) and the 2s poll-read fallback in _matrix_poll_loop — on
        fw0.3.2 the BATTERY_INFO CCCD subscribe is rejected, so the poll read
        is the live source.

        The percent is clamped to 0–100: fw0.3.3 reports raw values above
        100 on wall power (105–106 observed live 2026-07-02), which leaks a
        nonsensical battery reading into the sensor.
        """
        try:
            parts = data.decode().strip().split(",")
            return max(0, min(100, int(parts[0]))), parts[2] == "1"
        except Exception:
            return None

    def _on_battery(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Parse battery notification: 'percent,wallStatus,charging,doneCharging'.

        Runs on a non-loop thread (notify callback). Parses without
        touching shared state, then marshals the apply onto the loop
        so the self._state mutation can't race against entity reads.
        Audit §1.5, 2026-05-19.
        """
        parsed = self._parse_battery_payload(bytes(data))
        if parsed is None:
            # Malformed payload — nothing we can apply.
            return
        percent, charging = parsed
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

    # ── fw0.3.2 GAME_START length-rejection diagnostics ──────────────────────
    # Background: on firmware 0.3.2 the 103-byte GAME_START write (opcode 0 +
    # 100-char matrix + ",W") is rejected by the board's GATT server with
    # INVALID_ATTRIBUTE_VALUE_LENGTH (ATT error 0x0D). The payload already
    # matches the 0.3.2 doc §2.1 byte-for-byte, so this is NOT a wire-format
    # bug — 0x0D is a *server-side* length rejection. Two candidate causes:
    #   (1) the negotiated ATT MTU is too small for a single 103-byte write, or
    #   (2) the firmware declared UUID_GAME with attr_max_len < 103 (an ESP32
    #       GATT-table regression in 0.3.2).
    # These helpers capture the data needed to tell them apart on the live
    # board without guessing. See FABLE5_DEBUG_BRIEF.md / IMPROVEMENTS.md.

    @staticmethod
    def _is_invalid_attr_value_length(err: BaseException) -> bool:
        """True if ``err`` is the BLE 'Invalid Attribute Value Length' (0x0D).

        Detected by string match rather than importing a backend-specific
        bleak exception class, so it works on every bleak version and in the
        minimal test env where bleak isn't installed. The two stable textual
        forms both appear in the captured fault:
          - the enum name  "INVALID_ATTRIBUTE_VALUE_LENGTH"
          - the human text "Invalid Attribute Value Length"
        """
        blob = f"{err!r} {err}"
        return (
            "INVALID_ATTRIBUTE_VALUE_LENGTH" in blob
            or "Invalid Attribute Value Length" in blob
        )

    # ── Phantom 0.3.0 protocol — confirmed via HCI capture 2026-05-09 ────────
    # See PROTOCOL.md for the full activation sequence and game-loop details.

    # Pure FEN→matrix conversion lives in matrix.py now (Task #21 step 1,
    # 2026-05-16). The staticmethod alias preserves every `self.
    # _build_phantom_matrix_from_fen(fen)` call-site in the rest of this
    # file without code changes.
    _build_phantom_matrix_from_fen = staticmethod(_build_matrix_from_fen_module)

    # ── Game services (called from HA services) ───────────────────────────────

    def _assert_no_active_game(self) -> None:
        """Reject mode changes before touching the current game's state."""
        if self._state.get("physical_operation") in ("moving", "undoing", "uncertain"):
            raise RuntimeError("The board is not ready. Wait for movement to finish or check and reset the board.")
        if (self._game_id or self._local_game_active or self._two_player_active
                or self._ai_vs_ai_active or self._sculpture_active):
            raise RuntimeError("A chess game is already running. End it before starting another.")
        # A new start replaces the result card of a finished puzzle.
        if getattr(self, "_puzzle", None) is not None:
            self._clear_puzzle()
        if getattr(self, "_drill", None) is not None:
            self._clear_drill()

    async def async_resync_detection(self) -> None:
        """Recover a wedged board by re-seeding the firmware's expected matrix.

        For the "Snapping Pieces" / "Chessboard and sensor matrix do not
        match" wedge (firmware's internal expected-matrix gets corrupted even
        though the physical pieces are correct). Unlike `resume_from_phone`,
        this does NOT require an active game — it's usable at idle, which is
        exactly when the board gets stuck after powering on.

        Sends RESET_DETECTION (opcode 14) with the integration's current
        board FEN (the standard starting position when idle), telling the
        firmware "this IS the current position." This is a data re-sync, not
        a magnet move — no pieces are driven, so it's safe to invoke while the
        board is wedged. v0.4-beta3 (finding C1).
        """
        if not self._ble_connected:
            raise RuntimeError("Board not connected via Bluetooth")
        fen = self._board.board_fen()
        _LOGGER.info(
            "resync_detection: re-seeding firmware expected matrix to FEN %s "
            "(board_fen at current model state)", fen,
        )
        await self._phantom_send_reset_detection(fen)
        # Best-effort: clear any mismatch notification now that we've told the
        # firmware to accept the current position.
        for notif_id in (
            "phantom_chess_matrix_mismatch",
            "phantom_chess_ai_move_failed",
        ):
            try:
                await self.hass.services.async_call(
                    "persistent_notification", "dismiss",
                    {"notification_id": notif_id},
                )
            except Exception:  # noqa: BLE001 — dismiss is best-effort
                pass
        self.hass.async_create_task(self._announce_via_tts(
            "Re-syncing the board's piece detection."
        ))

    async def async_back_to_modes(self) -> None:
        """Reset the dashboard to its mode-picker state AND re-home the board.

        Replaces the v0.3 script `phantom_back_to_modes`. Effects:
        clear the post-game review flag (so the "review" conditional
        card hides), reset `setup_mode` to "Choose a mode" (so the mode
        picker re-renders), and drive the physical board back to the
        starting position.

        The board re-home was added 2026-06-03 (v0.4-beta2) per Luke's
        request that "Back to modes should always reset the board" — so
        returning to the picker leaves the physical board clean for the
        next mode. `async_reset_position` also finalizes and saves a
        two-player recording if one is active. The UI reset always
        happens first; the physical re-home is best-effort so a
        disconnected board can never trap the dashboard in a game view.
        Added 2026-05-26 (v0.4-alpha3).
        """
        from .const import DEFAULT_SETUP_MODE
        # Halt an in-flight sculpture playback (or AI-vs-AI loop) first, so its
        # move loop can't fight the re-home snapshot below. The loop honors
        # these flags and exits on its next iteration; cancel the task too so a
        # mid-`apply_ai_move` await returns promptly.
        stop_local = self._sculpture_active or self._ai_vs_ai_active or self._local_game_active
        self._sculpture_active = False
        self._ai_vs_ai_active = False
        if stop_local:
            self._local_game_active = False
            self._state["local_game_active"] = False
            self._state["lichess_game_id"] = None
            if self._local_game_task and not self._local_game_task.done():
                self._local_game_task.cancel()
            # Idle the firmware (SELECT_MODE 3) so it isn't left in a
            # playlist/chess-play state that ignores the re-home.
            try:
                await self._ble_write(UUID_SELECT_MODE, b"3")
            except Exception:  # noqa: BLE001
                pass

        # Fix C (live 2026-07-08): a "Back to modes" tap during an active
        # Lichess game means "I'm done" — but it used to leave the server game
        # running (lichess_active stuck ON, _game_id set), a zombie that needed
        # a full config-entry reload to clear. Best-effort resign (single POST,
        # DON'T block on failure — no retry), then force-tear-down the session
        # regardless so the dashboard can never trap a live game. `_game_id` is
        # only set for real Lichess games (local/sculpture/two-player use
        # _state["lichess_game_id"] instead), so this branch is Lichess-only.
        if self._game_id:
            game_id = self._game_id
            ok, status, body = await self._post_resign_once(game_id)
            if not ok:
                _LOGGER.warning(
                    "back_to_modes: best-effort resign failed (HTTP %s): %s — "
                    "tearing down local session anyway", status, body,
                )
            self._state["game_status"] = STATUS_IDLE
            self._clear_lichess_game_session()

        self.setup_mode = DEFAULT_SETUP_MODE
        self._state["lichess_review_ready"] = False
        self.async_set_updated_data(dict(self._state))

        # Re-home the physical board (drives to the starting FEN via the
        # snapshot protocol — a no-op of motion if pieces are already
        # home). Best-effort: a BLE failure must not block the UI return.
        try:
            await self.async_reset_position()
        except Exception as err:  # noqa: BLE001 — re-home is best-effort
            _LOGGER.warning("back_to_modes: board re-home skipped (%s)", err)

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

        if self.paused:
            raise RuntimeError("Resume the game before making a move")

        # Drive the magnet + push onto self._board. apply_ai_move handles
        # the BLE writes, the TTS announcement, the retry loop, and the
        # internal board state — exactly what we want for a dashboard
        # move except the TTS speech is "AI move" framing. That's fine
        # for now (the move announcement is informational either way);
        # if it bothers users we can plumb a "suppress_speech" flag.
        if not await self.async_phantom_apply_ai_move(uci):
            raise RuntimeError("The board did not confirm this move; play is paused")

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

            if self._board.is_game_over():
                self._finish_local_game()
                return

            self._state["game_status"] = STATUS_PLAYING
            self.async_set_updated_data(dict(self._state))

            # Trigger AI response via the serialized replacement helper
            # (audit §1.4) so we can't race against an in-flight turn
            # already scheduled by the discovery callback path.
            await self._replace_local_game_task(name=f"{DOMAIN}_local_ai_dashboard")

    async def async_takeback(self, count: int = 1) -> None:
        """Serialize undo with physical moves and preserve state until confirmed."""
        if self._physical_operation_lock.locked():
            raise RuntimeError("The board is still moving. Wait before undoing a move.")
        async with self._physical_operation_lock:
            completed = await self._async_takeback(count)
        if (completed and self._local_game_active and not self.paused
                and self._our_color is not None and self._board.turn != self._our_color):
            await self._replace_local_game_task(name=f"{DOMAIN}_local_ai_after_undo")

    async def _await_takeback_completion(self) -> None:
        """An acknowledged write is not a completed physical rearrangement."""
        future = self._move_done_future
        if future is None:
            raise RuntimeError("No physical completion is pending")
        await asyncio.wait_for(future, timeout=60.0)

    async def _async_takeback(self, count: int = 1) -> bool:
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
            session = rt.async_get_clientsession(self.hass)
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
                        return False
            except Exception as err:
                _LOGGER.warning(
                    "Takeback: Lichess request raised %s — aborting "
                    "without BLE write so state stays in sync",
                    err,
                )
                return False

        # Prepare a target without mutating the current rules position.
        target = self._board.copy()
        popped = 0
        for _ in range(count):
            if not target.move_stack:
                break
            try:
                target.pop()
                popped += 1
            except IndexError:
                break
        if popped == 0:
            _LOGGER.debug(
                "Takeback: internal board has no moves to undo; "
                "nothing to do",
            )
            return False

        # Determine who plays next per opcode 5 semantics.
        # "1" = board side (human) moves next; "0" = BLE side (AI) moves.
        if self._our_color is not None:
            side = "1" if target.turn == self._our_color else "0"
        else:
            # No resolved color (rare — e.g. takeback issued before any
            # game started, or during a session that bypassed Lichess
            # color resolution). Default to "1" so the firmware hands
            # the next move to the physical board, which is the least
            # disruptive failure mode (user can just move a piece).
            side = "1"

        fen = target.fen()
        payload = b"\x05" + f"{popped},{fen},{side}".encode("utf-8")
        was_paused = self.paused
        self.paused = True
        self._move_done_future = self.hass.loop.create_future()
        self._state["physical_operation"] = "undoing"
        self._state["position_confirmed"] = False
        self.async_set_updated_data(dict(self._state))
        try:
            await self._ble_write(UUID_GAME, payload)
            await self._await_takeback_completion()
            _LOGGER.info(
                "Takeback: opcode 5 sent (count=%d, side=%s, fen=%s)",
                popped, side, fen,
            )
        except (Exception, asyncio.CancelledError):
            self.paused = True
            self._state["game_status"] = STATUS_PAUSED
            self._state["physical_operation"] = "uncertain"
            self.async_set_updated_data(dict(self._state))
            raise
        finally:
            if self._move_done_future is not None and not self._move_done_future.done():
                self._move_done_future.cancel()
            self._move_done_future = None

        self.paused = was_paused
        self._board = target
        self._analysis_board = target.copy()
        self._processed_moves = len(target.move_stack)
        history = list(self._state.get("move_history_moves") or [])
        self._state["move_history_moves"] = history[:-popped] if popped <= len(history) else []
        for key in ("eval_cp", "eval_mate", "eval_depth", "eval_source",
                    "best_move_san", "threat_san", "last_move_classification",
                    "last_move_cpl", "last_move_motif"):
            self._state[key] = None
        self._state["position_confirmed"] = True
        self._state["physical_operation"] = "idle"
        # Sync sensor-visible state with the rolled-back position.
        self._state["live_fen"] = self._board.board_fen()
        self._state["last_move"] = (
            self._board.peek().uci() if target.move_stack else None
        )
        grid = self._build_phantom_matrix_from_fen(self._board.fen())
        self._state["piece_grid"] = grid
        self._state["piece_count"] = sum(1 for c in grid if c != ".")
        # CLEAN: Match parser cache must track the new target FEN, else
        # the next firmware "match" notification would revert live_fen
        # to whatever the pre-takeback target was.
        self._last_target_fen = self._board.board_fen()
        self.async_set_updated_data(dict(self._state))
        if self._local_game_active:
            await self.async_checkpoint()
        return True

    async def async_set_pause(self, paused: bool) -> None:
        """Pause or resume the board mechanism."""
        if not paused and self._state.get("physical_operation") == "uncertain":
            raise RuntimeError("The last movement is unconfirmed. Check and reset the board before resuming.")
        self._play_revision += 1
        self.paused = paused
        self._state["game_status"] = STATUS_PAUSED if paused else STATUS_PLAYING
        # Mode 3 = pause, mode 2 = chess play
        mode = 3 if paused else MODE_CHESS_PLAY
        await self._ble_write(UUID_SELECT_MODE, str(mode).encode())
        self.async_set_updated_data(dict(self._state))
        if (not paused and self._local_game_active
                and self._our_color is not None and self._board.turn != self._our_color
                and not self._physical_operation_lock.locked()):
            await self._replace_local_game_task(name=f"{DOMAIN}_local_ai_resumed")

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
        The dashboard position is committed only after physical completion.

        Physical drive happens via _phantom_execute_position with the
        standard starting FEN. The session-init flag is reset first so a
        clean GAME_END → HOME cycle precedes the new snapshot.
        """
        if self._physical_operation_lock.locked():
            raise RuntimeError("The board is still moving. Wait for it to finish.")
        if not self._ble_connected:
            raise RuntimeError("BLE not connected")

        if self._game_id:
            raise RuntimeError("An online game is active. End it before resetting the board.")
        if self._local_game_active:
            await self.async_stop_local_game()

        # v0.4-beta2: a manual reset during a two-player recording ends and
        # saves the game first (the dashboard Reset action doubles as
        # "end recording").
        if self._two_player_active:
            await self._finalize_two_player_game()

        # Force a fresh GAME_END → HOME cycle and drive physical reset.
        self._phantom_session_initialized = False
        _LOGGER.debug("Phantom reset_position: driving physical board to starting FEN")
        ok = await self._phantom_execute_position(
            fen=chess.STARTING_FEN, side="W", timeout_s=60.0,
        )
        if not ok:
            raise TimeoutError("The board did not confirm the reset. Check the pieces before continuing.")

        # Reset internal python-chess state.
        self._board = chess.Board()
        self._state["live_fen"] = self._board.board_fen()
        self._state["last_move"] = None
        grid = self._build_phantom_matrix_from_fen(self._board.fen())
        self._state["piece_grid"] = grid
        self._state["piece_count"] = sum(1 for c in grid if c != ".")
        self._last_target_fen = self._board.board_fen()
        self.async_set_updated_data(dict(self._state))

        # Fix D: after the re-home drive, verify all 32 pieces are back on the
        # 8×8. Pieces captured into the border tray during a game can't be
        # returned by the snapshot drive (firmware graveyard bookkeeping is lost
        # after a mismatch episode — live 2026-07-08), so surface a one-shot
        # notification asking the user to replace them by hand. Best-effort.
        await self._check_graveyard_shortfall()

    async def _check_graveyard_shortfall(self) -> None:
        """Notify if a re-home left fewer than 32 pieces on the playing area.

        Reads the sensor matrix once; if the on-board piece count is short, the
        missing pieces are sitting in the border tray/graveyard where the magnet
        can't reach them. Raises a one-shot persistent notification (no magnet
        heroics). Called after re-home drives (reset_position / back_to_modes).
        """
        client = self._ble_client
        if client is None or not client.is_connected:
            return
        try:
            data = await client.read_gatt_char(UUID_SEND_MATRIX)
        except Exception as err:  # noqa: BLE001 — the check is best-effort
            _LOGGER.debug("graveyard check: matrix read failed: %s", err)
            return
        parsed = _parse_matrix_notification(bytes(data))
        grid = parsed.get("piece_grid") if parsed is not None else None
        if grid is None:
            return
        piece_count = sum(1 for c in grid if c != ".")
        if piece_count >= 32:
            return
        missing = 32 - piece_count
        _LOGGER.info(
            "graveyard check: only %d/32 pieces on board after re-home; %d in tray",
            piece_count, missing,
        )
        try:
            await self.hass.services.async_call(
                "persistent_notification", "create",
                {
                    "title": "Phantom Chess: Pieces in tray",
                    "message": (
                        f"{missing} piece(s) are still in the border tray — place "
                        f"them on their starting squares by hand."
                    ),
                    "notification_id": "phantom_chess_graveyard_shortfall",
                },
            )
        except Exception:  # noqa: BLE001 — notification is best-effort
            pass

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

    def _is_double_fire_refire(self, payload_str: str) -> bool:
        """M2: True if this move frame is a firmware double-fire of the last
        APPLIED human move.

        A slow physical piece-slide can make the firmware emit two `\\x03M`
        movementVerify notifications for one move; the second sometimes arrives
        as a distinct placement string that is legal in the new position and
        would be pushed as a phantom second move. We treat a frame as a refire
        when it resolves (via ``_phantom_to_uci``) to the last applied move's
        UCI — or its 180° rotation — AND lands within
        ``MOVE_DEDUP_WINDOW_SECONDS`` of that apply (loop-monotonic clock).

        Distinct moves inside the window (legitimate blitz premoves) do NOT
        match, so they're still applied. Only the discovery move-apply path
        records ``_last_applied_move_uci`` (human moves); AI-move echoes are
        handled separately by ``_is_ai_echo``, which runs first.
        """
        last = self._last_applied_move_uci
        if not last or self._last_applied_move_ts <= 0.0:
            return False
        if (self.hass.loop.time() - self._last_applied_move_ts) >= MOVE_DEDUP_WINDOW_SECONDS:
            return False
        try:
            raw = _phantom_to_uci(payload_str)
        except Exception:
            return False
        if not raw or len(raw) < 4:
            return False
        # {raw, rotate(raw)} vs last covers both report orientations, because
        # rotate_180 is an involution.
        return raw == last or _rotate_uci_180(raw) == last

    def _is_ai_echo(self, payload_str: str) -> bool:
        """True if the firmware notification matches any of the UCIs in the
        last AI move's echo set (primary, rotated, plus castle-rook
        variants if applicable). Used by the discovery callback to drop
        magnet-driven sensor events that arrive after every AI move.
        """
        # M9: move-done-driven expiry. When BLE_MOVE_DONE fired, the discovery
        # callback armed `_ai_echo_move_done_expire_at` (now + grace). Once that
        # grace has elapsed the AI's magnet sequence — including a castle's
        # trailing rook echo — is complete, so clear the echo set: a genuinely
        # new human move that happens to match a stale echo UCI must no longer
        # be suppressed. During the grace window we still suppress (the rook
        # echo can straddle move-done). This is the fast path; the time backstop
        # below only bites when BLE_MOVE_DONE never arrives.
        expire_at = getattr(self, "_ai_echo_move_done_expire_at", 0.0)
        if expire_at and self.hass.loop.time() >= expire_at:
            self._last_ai_echo_ucis = set()
            self._ai_echo_move_done_expire_at = 0.0
            return False
        if not getattr(self, "_last_ai_echo_ucis", None):
            # Backward-compat: fall through to legacy single-UCI check
            # if the new set hasn't been populated yet.
            if self._last_ai_uci is None:
                return False
        import time as _time
        if _time.monotonic() - self._last_ai_uci_set_at > AI_ECHO_BACKSTOP_SECONDS:
            # Time backstop expired — assume all echoes arrived or none will.
            # Aligned to the 600 s activation-settle window (M9); this only
            # applies when no BLE_MOVE_DONE was ever received (else the
            # move-done expiry above clears the set within a few seconds).
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


        # If it's the AI's turn first (we're black), wait for the gameState event
        # which will arrive with the AI's first move.

    # ── Lichess analysis pipeline (added 2026-05-14) ────────────────────────
    # See lichess_analysis.py for cloud-eval client + classification logic.
    # See phantom_chess_research/IN_GAME_DASHBOARD_SPEC_2026-05-14.md for the
    # contract these methods fulfill toward the dashboard.

    @staticmethod
    def _move_is_analyzed(m: dict[str, Any]) -> bool:
        """True if a move-history entry carries a real analysis result rather
        than the ``_record_history_stub`` placeholder. The stub sets
        ``classification="unknown"``; ``_analyze_move`` overwrites it with a
        real label + integer CPL once the eval resolves."""
        cls = m.get("classification")
        return bool(cls) and cls != CLASSIFICATION_UNKNOWN

    @classmethod
    def _accuracy_for_side(
        cls, history: list[dict[str, Any]], side: str
    ) -> float | None:
        """Per-side accuracy from the ANALYZED plies only (Task 5).

        Returns None (sensor → "unknown") when that colour has no analyzed
        plies, or when the analyzed plies cover less than
        ``POST_GAME_MIN_ANALYZED_FRACTION`` of the colour's total plies — too
        sparse to be meaningful, and the case that used to fabricate a 0.0.
        Otherwise defers to ``_coarse_accuracy`` over the analyzed CPLs, so a
        fully-graded game reads exactly as before.
        """
        side_moves = [m for m in history if m.get("side") == side]
        if not side_moves:
            return None
        analyzed = [m for m in side_moves if cls._move_is_analyzed(m)]
        if not analyzed:
            return None
        if len(analyzed) / len(side_moves) < POST_GAME_MIN_ANALYZED_FRACTION:
            return None
        return cls._coarse_accuracy([int(m.get("cpl") or 0) for m in analyzed])

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

    # ── Local AI game (no Lichess required) ──────────────────────────────────



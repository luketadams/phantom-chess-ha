"""Sensor entities for Phantom Chess Board."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

import chess

from .const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NAME,
    DOMAIN,
    ENTITY_BATTERY,
    ENTITY_BEST_MOVE_SAN,
    ENTITY_EVAL_CP,
    ENTITY_EVAL_DEPTH,
    ENTITY_EVAL_MATE,
    ENTITY_EVAL_SOURCE,
    ENTITY_FIRMWARE_LAST_MOVE,
    ENTITY_FIRMWARE_MODE,
    ENTITY_LAST_GAME_ACCURACY_B,
    ENTITY_LAST_GAME_ACCURACY_W,
    ENTITY_LAST_GAME_RESULT,
    ENTITY_LAST_GAME_REVIEW,
    ENTITY_LAST_MOVE_CLASSIFICATION,
    ENTITY_LAST_MOVE_CPL,
    ENTITY_LAST_MOVE_MOTIF,
    ENTITY_LICHESS_BLACK_CLOCK,
    ENTITY_LICHESS_BLACK_CLOCK_DISP,
    ENTITY_LICHESS_BLACK_NAME,
    ENTITY_LICHESS_ID,
    ENTITY_LICHESS_WHITE_CLOCK,
    ENTITY_LICHESS_WHITE_CLOCK_DISP,
    ENTITY_LICHESS_WHITE_NAME,
    ENTITY_LIVE_POSITION,
    ENTITY_MATRIX_STATUS,
    ENTITY_MOVE_HISTORY,
    ENTITY_OPENING_NAME,
    ENTITY_PIECE_COUNT,
    ENTITY_THREAT_SAN,
)
from .coordinator import PhantomChessCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PhantomChessCoordinator = entry.runtime_data
    address = entry.data[CONF_BLE_ADDRESS]
    name = entry.data.get(CONF_DEVICE_NAME, "Phantom Chess Board")

    async_add_entities(
        [
            PhantomBatterySensor(coordinator, entry, address, name),
            PhantomLichessIdSensor(coordinator, entry, address, name),
            PhantomLivePositionSensor(coordinator, entry, address, name),
            PhantomPieceCountSensor(coordinator, entry, address, name),
            PhantomFirmwareModeSensor(coordinator, entry, address, name),
            PhantomMatrixStatusSensor(coordinator, entry, address, name),
            PhantomFirmwareLastMoveSensor(coordinator, entry, address, name),
            # ── Learning-dashboard sensors (added 2026-05-14) ───────────────
            PhantomOpeningNameSensor(coordinator, entry, address, name),
            PhantomLichessWhiteNameSensor(coordinator, entry, address, name),
            PhantomLichessBlackNameSensor(coordinator, entry, address, name),
            PhantomLichessWhiteClockSensor(coordinator, entry, address, name),
            PhantomLichessBlackClockSensor(coordinator, entry, address, name),
            PhantomLichessWhiteClockDisplaySensor(coordinator, entry, address, name),
            PhantomLichessBlackClockDisplaySensor(coordinator, entry, address, name),
            PhantomEvalCpSensor(coordinator, entry, address, name),
            PhantomEvalMateSensor(coordinator, entry, address, name),
            PhantomEvalSourceSensor(coordinator, entry, address, name),
            PhantomEvalDepthSensor(coordinator, entry, address, name),
            PhantomBestMoveSanSensor(coordinator, entry, address, name),
            PhantomLastMoveClassificationSensor(coordinator, entry, address, name),
            PhantomLastMoveCplSensor(coordinator, entry, address, name),
            PhantomLastMoveMotifSensor(coordinator, entry, address, name),
            PhantomThreatSanSensor(coordinator, entry, address, name),
            PhantomMoveHistorySensor(coordinator, entry, address, name),
            PhantomLastGameResultSensor(coordinator, entry, address, name),
            PhantomLastGameAccuracyWhiteSensor(coordinator, entry, address, name),
            PhantomLastGameAccuracyBlackSensor(coordinator, entry, address, name),
            PhantomLastGameReviewSensor(coordinator, entry, address, name),
        ]
    )


class PhantomBaseSensor(CoordinatorEntity[PhantomChessCoordinator], SensorEntity):
    """Base class for Phantom sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PhantomChessCoordinator,
        entry: ConfigEntry,
        address: str,
        device_name: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._address = address
        self._device_name = device_name
        self._attr_unique_id = f"{address}_{unique_suffix}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, address)},
            "name": device_name,
            "manufacturer": "Phantom",
            "model": "Phantom Chess Board",
        }


class PhantomBatterySensor(PhantomBaseSensor):
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_BATTERY)

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("battery_percent")


class PhantomLichessIdSensor(PhantomBaseSensor):
    """Lichess game ID for cross-referencing with lichess.org/<id>.

    Diagnostic-only — useful for support / debug, not main play UX.
    Recategorized from disabled-by-default to DIAGNOSTIC 2026-05-17.
    """
    _attr_name = "Lichess Game ID"
    _attr_icon = "mdi:identifier"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_ID)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("lichess_game_id")


class PhantomLivePositionSensor(PhantomBaseSensor):
    """Live board state derived from UUID_SEND_MATRIX (firmware 0.3.0).

    State = board-only FEN (8 ranks separated by /, no side-to-move).
    Attributes carry the raw 10x10 grid, the sensor bitmap, and timestamps so
    a Lovelace card can render either form.

    Marked as DIAGNOSTIC entity_category (2026-05-17) — the FEN state is
    typically 40+ characters which overflows HA's More Info dialog column
    layout, making the row appear visually broken (the state value wraps
    under the name "Live Position" so only the leading "L" appears on the
    row line). Moving to the Diagnostic section both tidies the main
    entity list AND signals to users that this is developer-facing data,
    not something they typically need to interact with — the image entity
    is what most users will actually render.
    """

    _attr_name = "Live Position"
    _attr_icon = "mdi:chess-board"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LIVE_POSITION)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("live_fen")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data or {}
        # `our_color` is the resolved color the user is playing — random has
        # already been picked at game-start. Falls back to the configured
        # selection if no game is active.
        try:
            _our = self.coordinator._our_color
            if _our is not None:
                our_color_resolved = "white" if _our == chess.WHITE else "black"
            else:
                pref = self.coordinator.player_color
                our_color_resolved = pref if pref in ("white", "black") else "white"
        except AttributeError:
            pref = getattr(self.coordinator, "player_color", None)
            our_color_resolved = pref if pref in ("white", "black") else "white"
        # Side to move tracked via the internal board's turn attribute, which
        # is the source of truth (the board-only FEN we expose lacks the
        # side-to-move field).
        try:
            side_to_move = (
                "white" if self.coordinator._board.turn else "black"
            )
        except AttributeError:
            side_to_move = None
        return {
            "piece_grid": data.get("piece_grid"),
            "sensor_bitmap": data.get("sensor_bitmap"),
            "matrix_raw": data.get("matrix_raw"),
            "matrix_last_updated": data.get("matrix_last_updated"),
            "matrix_mismatches": data.get("matrix_mismatches"),
            # ── Dashboard interactive-board fields (Task #27) ──────────────
            # Frontends embedding /phantom_chess_static/board.html read these
            # to gate input (enable drag-drop only when it's our turn AND a
            # game is active) and to orient the board (flip when playing black).
            "our_color": our_color_resolved,
            "side_to_move": side_to_move,
            "last_move": data.get("last_move"),
            "lichess_active": bool(data.get("lichess_active")),
            "local_game_active": bool(data.get("local_game_active")),
            "game_status": data.get("game_status"),
        }


class PhantomPieceCountSensor(PhantomBaseSensor):
    """Total pieces detected by the firmware on the board (0–32 normally).
    Counts non-empty cells in the 10×10 piece grid."""

    _attr_name = "Piece Count"
    _attr_icon = "mdi:chess-pawn"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_PIECE_COUNT)

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("piece_count")


class PhantomFirmwareModeSensor(PhantomBaseSensor):
    """Firmware operating mode — Running, Paused, Snapping Pieces, etc.
    Read from UUID acb6543c."""

    _attr_name = "Firmware Mode"
    _attr_icon = "mdi:chip"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_FIRMWARE_MODE)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("firmware_mode")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data or {}
        return {"last_updated": data.get("firmware_mode_last_updated")}


class PhantomMatrixStatusSensor(PhantomBaseSensor):
    """Whether the firmware's expected board state matches what its sensors
    detect. Values: 'Clean' (all expected pieces in place), 'Error' (mismatch
    detected — check status_message attribute), or unknown."""

    _attr_name = "Matrix Status"
    _attr_icon = "mdi:check-network"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_MATRIX_STATUS)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("matrix_status")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data or {}
        return {"status_message": data.get("matrix_status_message")}


class PhantomFirmwareLastMoveSensor(PhantomBaseSensor):
    """Last move emitted by the firmware on UUID_SET_STATE in M-format
    (e.g. 'K e1-a4', 'p a7-g6'). Fires during chess play and sculpture
    playback. This is the firmware-authoritative move source."""

    _attr_name = "Firmware Last Move"
    _attr_icon = "mdi:chess-knight"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_FIRMWARE_LAST_MOVE)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("firmware_last_move")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data or {}
        return {"last_updated": data.get("firmware_last_move_updated")}


# ── Learning-dashboard sensors (added 2026-05-14) ────────────────────────────
# All stubbed: native_value reads from self.coordinator.data with sensible
# defaults. Coordinator populates these keys when Lichess analysis runs.
# See phantom_chess_research/IN_GAME_DASHBOARD_SPEC_2026-05-14.md.


class PhantomOpeningNameSensor(PhantomBaseSensor):
    """Opening name from the Lichess explorer (e.g. 'Sicilian Defense, Najdorf')."""
    _attr_name = "Opening Name"
    _attr_icon = "mdi:book-open-page-variant"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_OPENING_NAME)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("opening_name")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data or {}
        return {"eco": data.get("opening_eco")}


class PhantomLichessWhiteNameSensor(PhantomBaseSensor):
    _attr_name = "Lichess White Name"
    _attr_icon = "mdi:account"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_WHITE_NAME)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("lichess_white_name")


class PhantomLichessBlackNameSensor(PhantomBaseSensor):
    _attr_name = "Lichess Black Name"
    _attr_icon = "mdi:account-outline"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_BLACK_NAME)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("lichess_black_name")


class PhantomLichessWhiteClockSensor(PhantomBaseSensor):
    _attr_name = "Lichess White Clock"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "s"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_WHITE_CLOCK)

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("lichess_white_clock")


class PhantomLichessBlackClockSensor(PhantomBaseSensor):
    _attr_name = "Lichess Black Clock"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "s"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_BLACK_CLOCK)

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("lichess_black_clock")


def _clock_display(seconds: int | float | None) -> str | None:
    """Render seconds as mm:ss (e.g. 9:34) for clock-display sensors."""
    if seconds is None:
        return None
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return None
    if s < 0:
        s = 0
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


class PhantomLichessWhiteClockDisplaySensor(PhantomBaseSensor):
    _attr_name = "Lichess White Clock Display"
    _attr_icon = "mdi:timer"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_WHITE_CLOCK_DISP)

    @property
    def native_value(self) -> str | None:
        return _clock_display((self.coordinator.data or {}).get("lichess_white_clock"))


class PhantomLichessBlackClockDisplaySensor(PhantomBaseSensor):
    _attr_name = "Lichess Black Clock Display"
    _attr_icon = "mdi:timer"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_BLACK_CLOCK_DISP)

    @property
    def native_value(self) -> str | None:
        return _clock_display((self.coordinator.data or {}).get("lichess_black_clock"))


class PhantomEvalCpSensor(PhantomBaseSensor):
    """Centipawn evaluation, white-positive. Null when mate is forced."""
    _attr_name = "Eval CP"
    _attr_icon = "mdi:gauge"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_EVAL_CP)

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("eval_cp")


class PhantomEvalMateSensor(PhantomBaseSensor):
    """Plies-to-mate, signed (positive = white mates). Null if no mate."""
    _attr_name = "Eval Mate"
    _attr_icon = "mdi:chess-king"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_EVAL_MATE)

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("eval_mate")


class PhantomEvalSourceSensor(PhantomBaseSensor):
    """Where the eval came from: 'lichess-cloud', 'stockfish-local', etc.

    Diagnostic-only — useful for debugging "why didn't eval update?"
    not for normal play. Recategorized DIAGNOSTIC 2026-05-17.
    """
    _attr_name = "Eval Source"
    _attr_icon = "mdi:server-network"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_EVAL_SOURCE)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("eval_source")


class PhantomEvalDepthSensor(PhantomBaseSensor):
    _attr_name = "Eval Depth"
    _attr_icon = "mdi:layers-search"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_EVAL_DEPTH)

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("eval_depth")


class PhantomBestMoveSanSensor(PhantomBaseSensor):
    _attr_name = "Best Move SAN"
    _attr_icon = "mdi:lightbulb-on"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_BEST_MOVE_SAN)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("best_move_san")


class PhantomLastMoveClassificationSensor(PhantomBaseSensor):
    """One of: brilliant/best/good/book/inaccuracy/mistake/blunder/unknown."""
    _attr_name = "Last Move Classification"
    _attr_icon = "mdi:label-multiple"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LAST_MOVE_CLASSIFICATION)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("last_move_classification")


class PhantomLastMoveCplSensor(PhantomBaseSensor):
    """Centipawns lost on the last move (≥0)."""
    _attr_name = "Last Move CPL"
    _attr_icon = "mdi:trending-down"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LAST_MOVE_CPL)

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("last_move_cpl")


class PhantomLastMoveMotifSensor(PhantomBaseSensor):
    """Tactical motif on the last move (fork/pin/skewer/empty)."""
    _attr_name = "Last Move Motif"
    _attr_icon = "mdi:vector-triangle"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LAST_MOVE_MOTIF)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("last_move_motif")


class PhantomThreatSanSensor(PhantomBaseSensor):
    """Best threat move for the side-to-move *if* it could move now.
    Empty when no notable threat exists."""
    _attr_name = "Threat SAN"
    _attr_icon = "mdi:alert"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_THREAT_SAN)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("threat_san")


class PhantomMoveHistorySensor(PhantomBaseSensor):
    """State is ply count; attribute 'moves' is the rich per-ply list
    (san, color, classification, cpl, glyph, motif)."""
    _attr_name = "Move History"
    _attr_icon = "mdi:history"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_MOVE_HISTORY)

    @property
    def native_value(self) -> int | None:
        moves = (self.coordinator.data or {}).get("move_history_moves") or []
        return len(moves)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data or {}
        return {"moves": data.get("move_history_moves") or []}


class PhantomLastGameResultSensor(PhantomBaseSensor):
    """'1-0', '0-1', '1/2-1/2', or termination reason."""
    _attr_name = "Last Game Result"
    _attr_icon = "mdi:trophy-outline"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LAST_GAME_RESULT)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get("last_game_result")


class PhantomLastGameAccuracyWhiteSensor(PhantomBaseSensor):
    _attr_name = "Last Game Accuracy White"
    _attr_icon = "mdi:target"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LAST_GAME_ACCURACY_W)

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get("last_game_accuracy_white")


class PhantomLastGameAccuracyBlackSensor(PhantomBaseSensor):
    _attr_name = "Last Game Accuracy Black"
    _attr_icon = "mdi:target"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LAST_GAME_ACCURACY_B)

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get("last_game_accuracy_black")


class PhantomLastGameReviewSensor(PhantomBaseSensor):
    """State is count of top mistakes; attribute 'top_mistakes' is the rich list."""
    _attr_name = "Last Game Review"
    _attr_icon = "mdi:magnify-scan"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LAST_GAME_REVIEW)

    @property
    def native_value(self) -> int | None:
        mistakes = (self.coordinator.data or {}).get("last_game_top_mistakes") or []
        return len(mistakes)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        data = self.coordinator.data or {}
        return {"top_mistakes": data.get("last_game_top_mistakes") or []}

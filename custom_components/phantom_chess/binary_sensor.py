"""Binary sensor entities for Phantom Chess Board."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BOARD_IDLE_THRESHOLD_SECONDS,
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NAME,
    DOMAIN,
    ENTITY_BOARD_IDLE,
    ENTITY_CONNECTED,
    ENTITY_LEARNING_VIEW_ACTIVE,
    ENTITY_LICHESS_ACTIVE,
    ENTITY_LICHESS_REVIEW_READY,
    ENTITY_PICKER_AVAILABLE,
    PICKER_FIRMWARE_MODES,
)
from .coordinator import PhantomChessCoordinator

# Read-only platform — entity updates are driven by the coordinator,
# which centralises the BLE notification stream. No parallel-request
# concern (Silver quality scale rule `parallel-updates`).
PARALLEL_UPDATES = 0


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
            PhantomConnectedSensor(coordinator, entry, address, name),
            # ── Learning-dashboard signals (added 2026-05-14) ───────────────
            PhantomLichessActiveSensor(coordinator, entry, address, name),
            PhantomLichessReviewReadySensor(coordinator, entry, address, name),
            # ── Mode-agnostic rich-view gate (added 2026-05-16, Task #9) ─────
            PhantomLearningViewActiveSensor(coordinator, entry, address, name),
            # ── v0.4-alpha3: integration-owned 60s-idle gate ────────────────
            PhantomBoardIdleSensor(coordinator, entry, address, name),
            # ── C3 (2026-07-06): integration-owned mode-picker gate ─────────
            PhantomPickerAvailableSensor(coordinator, entry, address, name),
        ]
    )


def _board_is_idle(data: dict) -> bool:
    """Return True when the firmware has been stable for >= the idle
    threshold (or has never reported a move).

    Shared by :class:`PhantomBoardIdleSensor` and
    :class:`PhantomPickerAvailableSensor` so the 60s-stability semantics
    live in exactly one place.
    """
    ts_iso = data.get("firmware_last_move_updated")
    if not ts_iso:
        # Never seen a move (board just connected, fresh install). Treat
        # as idle so the mode picker renders rather than the (empty)
        # live-game cards.
        return True
    try:
        ts = datetime.fromisoformat(ts_iso)
    except (TypeError, ValueError):
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
    return elapsed > BOARD_IDLE_THRESHOLD_SECONDS


class PhantomBaseBinary(CoordinatorEntity[PhantomChessCoordinator], BinarySensorEntity):
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
        self._attr_unique_id = f"{address}_{unique_suffix}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, address)},
            "name": device_name,
            "manufacturer": "Phantom",
            "model": "Phantom Chess Board",
        }

    @property
    def available(self) -> bool:
        """Base availability: DataUpdateCoordinator last_update_success gate only.

        B4 fix: the BLE gate was here, blanking Lichess/analysis signals
        (lichess_active, lichess_review_ready, learning_view_active) on
        any proxy blip — collapsing the learning view mid-game and killing
        post-game review if the board powers off after the game. Only
        board-hardware sensors need the BLE gate; Lichess/analysis sensors
        read coordinator state populated by the Lichess stream/Stockfish.
        The connected sensor overrides available to True unconditionally.
        """
        return super().available


class PhantomConnectedSensor(PhantomBaseBinary):
    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_CONNECTED)

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_ble_connected

    @property
    def available(self) -> bool:
        """Always available — its state IS the BLE-connection signal.

        Overriding ``PhantomBaseBinary.available`` so this sensor stays
        visible when the board is disconnected; otherwise the user
        would lose the very indicator they need to diagnose connectivity.
        """
        return True


# ── Learning-dashboard binary signals (added 2026-05-14) ─────────────────────

class PhantomLichessActiveSensor(PhantomBaseBinary):
    """True while a Lichess Board API game is in progress.
    Drives conditional 6 (in-game rich view) on the chess dashboard."""

    _attr_translation_key = "lichess_active"
    _attr_icon = "mdi:chess-king"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_ACTIVE)

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("lichess_active"))


class PhantomLichessReviewReadySensor(PhantomBaseBinary):
    """True after a Lichess game ends, while the review payload is available.
    Drives conditional 7 (post-game review) on the chess dashboard.
    Cleared when a new game starts."""

    _attr_translation_key = "lichess_review_ready"
    _attr_icon = "mdi:magnify-scan"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_REVIEW_READY)

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("lichess_review_ready"))


class PhantomLearningViewActiveSensor(PhantomBaseBinary):
    """True when EITHER a Lichess game OR a local-Stockfish game is in
    progress. Gates the rich learning-dashboard view so it renders for
    both gameplay modes, not just Lichess. Added 2026-05-16 (Task #9)."""

    _attr_translation_key = "learning_view_active"
    _attr_icon = "mdi:school"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LEARNING_VIEW_ACTIVE)

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        # Lichess path uses the state-dict flag; local-game path mirrors its
        # instance attribute into the same dict via coordinator updates.
        return bool(
            data.get("lichess_active")
            or data.get("local_game_active")
            or data.get("two_player_active")
        )


# ── v0.4-alpha3: integration-owned 60s-idle gate ──────────────────────────

class _PhantomPeriodicBinary(PhantomBaseBinary):
    """Base for binary sensors whose `is_on` depends on elapsed wall-clock
    time (the 60s idle threshold) and must therefore re-evaluate on a timer
    even when no coordinator update arrives.

    Re-evaluates every 5 seconds via `async_track_time_interval` so a
    time-driven True/False transition fires within ~5s, well under the 60s
    threshold. Cancels the interval on entity removal.

    NOTE: the callback MUST be @callback-decorated. In HA 2026+, plain
    lambdas passed to `async_track_time_interval` are scheduled as executor
    jobs (worker thread), and calling `async_write_ha_state` from there
    raises a RuntimeError (thread-safety violation). The decorator marks the
    function as event-loop-safe so HA invokes it inline on the loop.
    v0.4-alpha6 fix.
    """

    def __init__(self, coord, entry, address, name, unique_suffix):
        super().__init__(coord, entry, address, name, unique_suffix)
        self._unsub_interval = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._async_periodic_update,
            timedelta(seconds=5),
        )

    @callback
    def _async_periodic_update(self, _now) -> None:
        """Push the freshly-evaluated is_on state to HA. @callback-decorated
        so HA schedules it on the event loop, not the executor."""
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        await super().async_will_remove_from_hass()


class PhantomBoardIdleSensor(_PhantomPeriodicBinary):
    """True only when the firmware has been stable for >= 60 seconds.

    Replaces the template binary_sensor.phantom_chess_board_idle that
    v0.3's examples/helpers.yaml required users to create via the
    template-integration helper. The dashboard uses this as a gate to
    decide when to render the mode picker (idle) vs the live-game cards
    (mid-move). Without the 60s stability check the dashboard flickers
    during sculpture playback because the firmware emits per-piece-move
    state notifications every couple seconds.

    Tracks `firmware_last_move_updated` (set by the coordinator on every
    \x03M / firmware-mode move event). Semantically matches v0.3's
    template:
        {% set s = states.sensor.phantom_..._firmware_last_move %}
        {% if s is none or s.last_changed is none %}true
        {% else %}{{ (now() - s.last_changed).total_seconds() > 60 }}{% endif %}

    D-block: no device_class. It previously carried BinarySensorDeviceClass
    .RUNNING, which HA renders as "Running" when *on* — i.e. an idle board
    read as "Running", the exact opposite of the truth.
    """

    _attr_translation_key = "board_idle"
    _attr_icon = "mdi:sleep"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_BOARD_IDLE)

    @property
    def is_on(self) -> bool:
        return _board_is_idle(self.coordinator.data or {})


# ── C3 (2026-07-06): integration-owned mode-picker gate ────────────────────

class PhantomPickerAvailableSensor(_PhantomPeriodicBinary):
    """True when the dashboard's mode picker (and per-mode setup views)
    should render: the board is connected, has been idle >= 60s, is in a
    standby/home firmware mode, and no integration-driven game or post-game
    review is in progress.

    C3 (deep-dive 2026-07-06): this collapses the firmware_mode OR-block +
    idle/connected/no-active-game gate that was copy-pasted into six
    dashboard conditional cards. Adding or renaming a firmware label is now a
    one-line edit to ``PICKER_FIRMWARE_MODES`` in const.py, unit-tested
    against every label, instead of a six-place YAML edit with a silent
    blank-screen regression risk. The four transient "magnet rearranging"
    labels are deliberately excluded (owned by the stand-by interstitials) —
    see ``PICKER_FIRMWARE_MODES``.

    ``firmware_mode`` of ``None`` (freshly connected / not yet reported) is
    treated as a picker-eligible standby state, mirroring the dashboard's
    old ``unknown``/``unavailable`` OR-branches. The dashboard still adds the
    per-view ``setup_mode`` check (a Lovelace helper the integration doesn't
    own), so this sensor deliberately does NOT encode which mode is selected.
    """

    _attr_translation_key = "picker_available"
    _attr_icon = "mdi:view-dashboard-variant"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_PICKER_AVAILABLE)

    @property
    def is_on(self) -> bool:
        if not self.coordinator.is_ble_connected:
            return False
        data = self.coordinator.data or {}
        # No active game (any mode) and no pending review — mirrors the
        # learning_view_active signals so the picker/setup views never
        # overlap the live learning view, even if a game stalls for >60s.
        if (
            data.get("lichess_active")
            or data.get("local_game_active")
            or data.get("two_player_active")
            or data.get("lichess_review_ready")
        ):
            return False
        if not _board_is_idle(data):
            return False
        mode = data.get("firmware_mode")
        return mode is None or mode in PICKER_FIRMWARE_MODES

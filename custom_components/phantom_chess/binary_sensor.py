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
        ]
    )


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
        """Silver quality scale rule `entity-unavailable`.

        Mark unavailable when the BLE connection drops. The connected
        sensor itself overrides this back to True so the user can still
        see the disconnected state.
        """
        return super().available and self.coordinator.is_ble_connected


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

class PhantomBoardIdleSensor(PhantomBaseBinary):
    """True only when the firmware has been stable for >= 60 seconds.

    Replaces the template binary_sensor.phantom_chess_board_idle that
    v0.3's examples/helpers.yaml required users to create via the
    template-integration helper. The dashboard uses this as a gate to
    decide when to render the mode picker (idle) vs the live-game cards
    (mid-move). Without the 60s stability check the dashboard flickers
    during sculpture playback because the firmware emits per-piece-move
    state notifications every couple seconds.

    Implementation: tracks `firmware_last_move_updated` (set by the
    coordinator on every \x03M / firmware-mode move event); re-evaluates
    every 5 seconds via `async_track_time_interval` so the True
    transition fires within ~5s of the threshold elapsing, even without
    any new coordinator events. Cancels the interval on entity removal.
    Semantically matches v0.3's template:
        {% set s = states.sensor.phantom_..._firmware_last_move %}
        {% if s is none or s.last_changed is none %}true
        {% else %}{{ (now() - s.last_changed).total_seconds() > 60 }}{% endif %}
    """

    _attr_translation_key = "board_idle"
    _attr_icon = "mdi:sleep"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_BOARD_IDLE)
        self._unsub_interval = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Periodic re-eval so the True transition fires on time even when
        # the coordinator isn't pushing updates. 5s interval = 5s
        # worst-case delay on the threshold crossing, well under the 60s
        # threshold itself so it doesn't matter visually.
        #
        # NOTE: callback MUST be @callback-decorated. In HA 2026+, plain
        # lambdas passed to `async_track_time_interval` are scheduled as
        # executor jobs (worker thread), and calling `async_write_ha_state`
        # from there raises a RuntimeError (thread-safety violation). The
        # decorator marks the function as event-loop-safe so HA invokes
        # it inline on the loop. v0.4-alpha6 fix.
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

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
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

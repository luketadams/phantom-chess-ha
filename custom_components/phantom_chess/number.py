"""Number entities for Phantom Chess Board."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NAME,
    DOMAIN,
    ENTITY_LICHESS_CLOCK_INCREMENT,
    ENTITY_LICHESS_CLOCK_MINUTES,
    ENTITY_MECH_SPEED,
    ENTITY_SOUND_LEVEL,
)
from .coordinator import PhantomChessCoordinator

# Action-issuing platform — each set_native_value triggers a BLE
# characteristic write. Serialize so we don't overlap writes on the
# single GATT client (Silver quality scale rule `parallel-updates`).
PARALLEL_UPDATES = 1


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
            PhantomMechanismSpeedNumber(coordinator, entry, address, name),
            PhantomSoundLevelNumber(coordinator, entry, address, name),
            PhantomLichessClockMinutesNumber(coordinator, entry, address, name),
            PhantomLichessClockIncrementNumber(coordinator, entry, address, name),
        ]
    )


class PhantomBaseNumber(CoordinatorEntity[PhantomChessCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    # Gold quality scale rule `entity-category`: every number on this
    # integration is user-tunable configuration (mechanism speed, sound
    # level, Lichess clock minutes / increment).
    _attr_entity_category = EntityCategory.CONFIG

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

        Default for number entities is "BLE write required" — most
        number-platform entries on this device are mechanism speed /
        sound level, which need an active GATT session. Lichess clock
        controls (which are pure-local game-start config storage)
        override this back to always-available below.
        """
        return super().available and self.coordinator.is_ble_connected


class PhantomMechanismSpeedNumber(PhantomBaseNumber):
    """Mechanism speed on a 1..5 scale (firmware native).

    Per xouxou's `phantom.js` and the official Phantom Chess user manual,
    the firmware exposes mechanism speed as an integer 1..5 where 1 is the
    slowest (manual "SILENCE") and 5 is fastest ("FAST"). Default is 3
    (manual "NORMAL"). The integration was previously exposing a 0..100
    slider which the firmware likely clamped silently.
    """
    _attr_translation_key = "mechanism_speed"
    _attr_icon = "mdi:speedometer"
    _attr_native_min_value = 1
    _attr_native_max_value = 5
    _attr_native_step = 1

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_MECH_SPEED)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.mechanism_speed)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_mechanism_speed(int(value))
        self.async_write_ha_state()


class PhantomSoundLevelNumber(PhantomBaseNumber):
    _attr_translation_key = "sound_level"
    _attr_icon = "mdi:volume-high"
    # Firmware accepts volume 0-32 (per Efraín's gameplay doc 2026-05-14).
    _attr_native_min_value = 0
    _attr_native_max_value = 32
    _attr_native_step = 1

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_SOUND_LEVEL)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.sound_level)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_sound_level(int(value))
        self.async_write_ha_state()


# ─── v0.4-alpha2: integration-owned Lichess clock controls ──────────────
# These replace `input_number.phantom_chess_lichess_clock_minutes` and
# `input_number.phantom_chess_lichess_clock_increment` from v0.3's
# examples/helpers.yaml. State persists across HA restarts via
# RestoreEntity. The dashboard's "Start Lichess game" tile previously
# called `script.phantom_start_lichess_configured` which read the
# input_numbers and passed minutes*60 + increment to
# phantom_chess.start_game. With these fields owned by the integration,
# that script can be simplified or replaced by a service call that reads
# the values from the coordinator directly. Service-side cleanup is
# coming in a later alpha.


class _PhantomRestorableNumber(PhantomBaseNumber, RestoreEntity):
    """Base for numbers whose state must survive HA restarts. The
    coordinator field name is given by `_coord_attr`; on first
    state-restore the field is re-populated from the last persisted
    value so the dashboard's clock-control sliders don't reset to
    default on every reload.

    These fields are pure-local config storage (Lichess clock minutes /
    increment used only at start-game time). They don't write to BLE,
    so we override the BLE-availability check from PhantomBaseNumber
    back to always-available — the user must be able to pre-configure
    the clock before the board is reachable.
    """

    _coord_attr: str  # subclass sets; e.g. "lichess_clock_minutes"

    @property
    def available(self) -> bool:
        """Always available — local config storage, no BLE dependency."""
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (None, "unknown", "unavailable"):
            return
        try:
            setattr(self.coordinator, self._coord_attr, int(float(last.state)))
        except (TypeError, ValueError):
            pass


class PhantomLichessClockMinutesNumber(_PhantomRestorableNumber):
    """Lichess clock 'limit' field, in minutes. The dashboard exposes
    this as a slider 1..60; the start-game flow multiplies by 60 to get
    Lichess's seconds-based clock.limit. Default 30 = standard rapid.
    """
    _attr_translation_key = "lichess_clock_minutes"
    _attr_icon = "mdi:timer"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 180   # 3 hours upper bound
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "min"
    _coord_attr = "lichess_clock_minutes"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_CLOCK_MINUTES)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.lichess_clock_minutes)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.lichess_clock_minutes = int(value)
        self.async_write_ha_state()


class PhantomLichessClockIncrementNumber(_PhantomRestorableNumber):
    """Lichess clock 'increment' field, in seconds per move. Default 0.
    Combined with clock_minutes this forms the full Lichess time-control
    spec (e.g. "30+0", "10+5", "15+10").
    """
    _attr_translation_key = "lichess_clock_increment"
    _attr_icon = "mdi:timer-plus"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 180
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _coord_attr = "lichess_clock_increment"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LICHESS_CLOCK_INCREMENT)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.lichess_clock_increment)

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.lichess_clock_increment = int(value)
        self.async_write_ha_state()

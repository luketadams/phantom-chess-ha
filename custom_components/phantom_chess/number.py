"""Number entities for Phantom Chess Board."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NAME,
    DOMAIN,
    ENTITY_MECH_SPEED,
    ENTITY_SOUND_LEVEL,
)
from .coordinator import PhantomChessCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PhantomChessCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_BLE_ADDRESS]
    name = entry.data.get(CONF_DEVICE_NAME, "Phantom Chess Board")

    async_add_entities(
        [
            PhantomMechanismSpeedNumber(coordinator, entry, address, name),
            PhantomSoundLevelNumber(coordinator, entry, address, name),
        ]
    )


class PhantomBaseNumber(CoordinatorEntity[PhantomChessCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1

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


class PhantomMechanismSpeedNumber(PhantomBaseNumber):
    """Mechanism speed on a 1..5 scale (firmware native).

    Per xouxou's `phantom.js` and the official Phantom Chess user manual,
    the firmware exposes mechanism speed as an integer 1..5 where 1 is the
    slowest (manual "SILENCE") and 5 is fastest ("FAST"). Default is 3
    (manual "NORMAL"). The integration was previously exposing a 0..100
    slider which the firmware likely clamped silently.
    """
    _attr_name = "Mechanism Speed"
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
    _attr_name = "Sound Level"
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

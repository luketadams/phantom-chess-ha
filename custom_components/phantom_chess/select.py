"""Select entities for Phantom Chess Board."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NAME,
    DEFAULT_AI_LEVEL,
    DEFAULT_PLAYER_COLOR,
    DOMAIN,
    ENTITY_AI_LEVEL,
    ENTITY_PLAYER_COLOR,
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
            PhantomAiLevelSelect(coordinator, entry, address, name),
            PhantomPlayerColorSelect(coordinator, entry, address, name),
        ]
    )


class PhantomBaseSelect(CoordinatorEntity[PhantomChessCoordinator], SelectEntity):
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


class PhantomAiLevelSelect(PhantomBaseSelect):
    _attr_name = "AI Level"
    _attr_icon = "mdi:robot"
    _attr_options = ["1", "2", "3", "4", "5", "6", "7", "8"]

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_AI_LEVEL)

    @property
    def current_option(self) -> str:
        return str(self.coordinator.ai_level)

    async def async_select_option(self, option: str) -> None:
        self.coordinator.ai_level = int(option)
        self.async_write_ha_state()


class PhantomPlayerColorSelect(PhantomBaseSelect):
    _attr_name = "Player Color"
    _attr_icon = "mdi:chess-pawn"
    _attr_options = ["white", "black", "random"]

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_PLAYER_COLOR)

    @property
    def current_option(self) -> str:
        return self.coordinator.player_color

    async def async_select_option(self, option: str) -> None:
        self.coordinator.player_color = option
        self.async_write_ha_state()

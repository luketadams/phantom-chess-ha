"""Switch entities for Phantom Chess Board."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BLE_ADDRESS, CONF_DEVICE_NAME, DOMAIN, ENTITY_PAUSE
from .coordinator import PhantomChessCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PhantomChessCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_BLE_ADDRESS]
    name = entry.data.get(CONF_DEVICE_NAME, "Phantom Chess Board")

    async_add_entities([PhantomPauseSwitch(coordinator, entry, address, name)])


class PhantomPauseSwitch(CoordinatorEntity[PhantomChessCoordinator], SwitchEntity):
    """Pause/resume the board mechanism (pieces stop moving)."""

    _attr_has_entity_name = True
    _attr_name = "Paused"
    _attr_icon = "mdi:pause-circle"

    def __init__(
        self,
        coordinator: PhantomChessCoordinator,
        entry: ConfigEntry,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{address}_{ENTITY_PAUSE}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, address)},
            "name": device_name,
            "manufacturer": "Phantom",
            "model": "Phantom Chess Board",
        }

    @property
    def is_on(self) -> bool:
        return self.coordinator.paused

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_pause(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_pause(False)

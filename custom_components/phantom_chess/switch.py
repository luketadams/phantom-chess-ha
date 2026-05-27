"""Switch entities for Phantom Chess Board."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
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
    ENTITY_PAUSE,
    ENTITY_TRAINING_WHEELS,
)
from .coordinator import PhantomChessCoordinator

# Mixed platform — PhantomPauseSwitch writes BLE (pause/resume the
# mechanism); PhantomTrainingWheelsSwitch is pure-local config storage
# for the training-wheels glyph overlay. Serializing BLE writes against
# any other concurrent BLE work is cheap insurance (Silver quality
# scale rule `parallel-updates`).
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PhantomChessCoordinator = entry.runtime_data
    address = entry.data[CONF_BLE_ADDRESS]
    name = entry.data.get(CONF_DEVICE_NAME, "Phantom Chess Board")

    async_add_entities([
        PhantomPauseSwitch(coordinator, entry, address, name),
        PhantomTrainingWheelsSwitch(coordinator, entry, address, name),
    ])


class PhantomPauseSwitch(CoordinatorEntity[PhantomChessCoordinator], SwitchEntity):
    """Pause/resume the board mechanism (pieces stop moving)."""

    _attr_has_entity_name = True
    _attr_translation_key = "paused"
    _attr_icon = "mdi:pause-circle"
    # Gold quality scale rule `entity-category`: pause is a user-tunable
    # control (not a primary game-state surface).
    _attr_entity_category = EntityCategory.CONFIG

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

    @property
    def available(self) -> bool:
        """Silver quality scale rule `entity-unavailable`.

        Pause/resume drives a BLE write (UUID_PAUSE) — useless when the
        board isn't connected.
        """
        return super().available and self.coordinator.is_ble_connected

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_pause(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_pause(False)


class PhantomTrainingWheelsSwitch(
    CoordinatorEntity[PhantomChessCoordinator], SwitchEntity, RestoreEntity
):
    """Training-wheels toggle: when ON, the TTS and dashboard glyph
    overlay surface ALL move classifications (best / good / inaccuracy /
    mistake / blunder). When OFF (default), only mistake-and-worse fire
    so the player isn't constantly told about merely-acceptable moves.

    v0.4-alpha2: replaces `input_boolean.phantom_chess_training_wheels`
    that v0.3 required in `examples/helpers.yaml`. State persists across
    HA restarts via `RestoreEntity`.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "training_wheels"
    _attr_icon = "mdi:school"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: PhantomChessCoordinator,
        entry: ConfigEntry,
        address: str,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{address}_{ENTITY_TRAINING_WHEELS}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, address)},
            "name": device_name,
            "manufacturer": "Phantom",
            "model": "Phantom Chess Board",
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (None, "unknown", "unavailable"):
            return
        self.coordinator.training_wheels = last.state == "on"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.training_wheels)

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.training_wheels = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.training_wheels = False
        self.async_write_ha_state()

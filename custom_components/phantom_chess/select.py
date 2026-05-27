"""Select entities for Phantom Chess Board."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NAME,
    DEFAULT_AI_LEVEL,
    DEFAULT_PLAYER_COLOR,
    DEFAULT_SCULPTURE_GAME,
    DEFAULT_SETUP_MODE,
    DOMAIN,
    ENTITY_AI_LEVEL,
    ENTITY_PLAYER_COLOR,
    ENTITY_SCULPTURE_GAME,
    ENTITY_SETUP_MODE,
    SCULPTURE_GAMES,
    SETUP_MODE_OPTIONS,
)
from .coordinator import PhantomChessCoordinator

# No BLE writes on any select-platform entity — every option stored
# here is pure-local UI / game-start config (AI level, player color,
# setup mode, sculpture game choice). No concurrency to coordinate,
# and (importantly) entities stay available when the board is offline
# so the user can pre-configure game settings (Silver quality scale
# rule `parallel-updates`).
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
            PhantomAiLevelSelect(coordinator, entry, address, name),
            PhantomPlayerColorSelect(coordinator, entry, address, name),
            PhantomSetupModeSelect(coordinator, entry, address, name),
            PhantomSculptureGameSelect(coordinator, entry, address, name),
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


# ─── v0.4-alpha1: integration-owned mode + sculpture pickers ────────────
# These replace the input_select.phantom_chess_setup_mode and
# input_select.phantom_chess_sculpture_game helpers that v0.3 required
# users to create by hand. State persists across restarts via
# RestoreEntity (re-applied to the coordinator on first state-restore
# callback). The dashboard's mode-picker logic moves from
# `input_select.select_option {entity_id: input_select.phantom_chess_setup_mode}`
# to `select.select_option {entity_id: select.<DEVICE>_setup_mode}`.


class _PhantomRestorableSelect(PhantomBaseSelect, RestoreEntity):
    """Base for selects whose state must survive HA restarts. The
    coordinator field name is given by `_coord_attr`; on first
    state-restore the coordinator field is re-populated from the last
    persisted state so the dashboard's mode picker doesn't reset to
    default on every reload.
    """

    _coord_attr: str  # subclass must set; e.g. "setup_mode"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or last.state in (None, "unknown", "unavailable"):
            return
        if last.state in self._attr_options:
            setattr(self.coordinator, self._coord_attr, last.state)


class PhantomSetupModeSelect(_PhantomRestorableSelect):
    _attr_name = "Setup Mode"
    _attr_icon = "mdi:view-list"
    _attr_options = SETUP_MODE_OPTIONS
    _coord_attr = "setup_mode"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_SETUP_MODE)

    @property
    def current_option(self) -> str:
        return self.coordinator.setup_mode or DEFAULT_SETUP_MODE

    async def async_select_option(self, option: str) -> None:
        self.coordinator.setup_mode = option
        self.async_write_ha_state()


class PhantomSculptureGameSelect(_PhantomRestorableSelect):
    _attr_name = "Sculpture Game"
    _attr_icon = "mdi:chess-rook"
    _attr_options = SCULPTURE_GAMES
    _coord_attr = "selected_sculpture"

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_SCULPTURE_GAME)

    @property
    def current_option(self) -> str:
        return self.coordinator.selected_sculpture or DEFAULT_SCULPTURE_GAME

    async def async_select_option(self, option: str) -> None:
        self.coordinator.selected_sculpture = option
        self.async_write_ha_state()

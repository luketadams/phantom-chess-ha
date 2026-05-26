"""Binary sensor entities for Phantom Chess Board."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NAME,
    DOMAIN,
    ENTITY_CONNECTED,
    ENTITY_LEARNING_VIEW_ACTIVE,
    ENTITY_LICHESS_ACTIVE,
    ENTITY_LICHESS_REVIEW_READY,
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
            PhantomConnectedSensor(coordinator, entry, address, name),
            # ── Learning-dashboard signals (added 2026-05-14) ───────────────
            PhantomLichessActiveSensor(coordinator, entry, address, name),
            PhantomLichessReviewReadySensor(coordinator, entry, address, name),
            # ── Mode-agnostic rich-view gate (added 2026-05-16, Task #9) ─────
            PhantomLearningViewActiveSensor(coordinator, entry, address, name),
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


class PhantomConnectedSensor(PhantomBaseBinary):
    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_CONNECTED)

    @property
    def is_on(self) -> bool:
        return self.coordinator.is_ble_connected


# ── Learning-dashboard binary signals (added 2026-05-14) ─────────────────────

class PhantomLichessActiveSensor(PhantomBaseBinary):
    """True while a Lichess Board API game is in progress.
    Drives conditional 6 (in-game rich view) on the chess dashboard."""

    _attr_name = "Lichess Active"
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

    _attr_name = "Lichess Review Ready"
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

    _attr_name = "Learning View Active"
    _attr_icon = "mdi:school"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coord, entry, address, name):
        super().__init__(coord, entry, address, name, ENTITY_LEARNING_VIEW_ACTIVE)

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        # Lichess path uses the state-dict flag; local-game path mirrors its
        # instance attribute into the same dict via coordinator updates.
        return bool(data.get("lichess_active") or data.get("local_game_active"))

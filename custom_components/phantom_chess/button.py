"""Button entities for Phantom Chess Board.

Adds in-UI buttons for actions that previously required calling services manually.
Both buttons are diagnostic-grade (developer-facing protocol primitives,
not user game flow) and are disabled-by-default in the entity registry.
The user-facing game-start path is the `phantom_chess.start_local_game`
and `phantom_chess.start_game` services, surfaced as dashboard tiles in
`examples/dashboard.yaml`.
"""
from __future__ import annotations

import logging

import chess

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BLE_ADDRESS, CONF_DEVICE_NAME, DOMAIN
from .coordinator import PhantomChessCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PhantomChessCoordinator = entry.runtime_data
    address = entry.data[CONF_BLE_ADDRESS]
    name = entry.data.get(CONF_DEVICE_NAME, "Phantom Chess Board")

    async_add_entities([
        PhantomStartGameButton(coordinator, entry, address, name),
        PhantomMovementVerifyButton(coordinator, entry, address, name),
    ])


class _PhantomBaseButton(CoordinatorEntity[PhantomChessCoordinator], ButtonEntity):
    """Shared device-info plumbing for Phantom buttons."""

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
        self._attr_unique_id = f"{address}_{unique_suffix}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, address)},
            "name": device_name,
            "manufacturer": "Phantom",
            "model": "Phantom Chess Board",
        }


class PhantomStartGameButton(_PhantomBaseButton):
    """Run the confirmed BLE activation sequence (SELECT_MODE 2 → gameStart → side).

    **Developer / diagnostic** — this button drives just the firmware-side
    snapshot protocol and does NOT wire up a Lichess or local-Stockfish
    AI game on top of it. Pressing it in normal operation leaves the
    firmware in "BLE Playing" mode with no game backend; the dashboard's
    in-game cards have nothing to render and may appear blank. For
    actual gameplay use the `phantom_chess.start_local_game` or
    `phantom_chess.start_game` services (surfaced as dashboard tiles in
    examples/dashboard.yaml).

    Also requires the user to lift and replace any piece on the board
    after pressing so the firmware can break out of Setting Up
    oscillation — another reason this isn't part of the user flow.

    Disabled-by-default in the entity registry as of 2026-05-25; can be
    re-enabled per-device when debugging the snapshot path.
    """

    _attr_name = "Start Game"
    _attr_icon = "mdi:play-circle"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, entry, address, device_name) -> None:
        super().__init__(coordinator, entry, address, device_name, "start_game_button")

    async def async_press(self) -> None:
        _LOGGER.info(
            "Start Game (diagnostic) button pressed: running "
            "phantom_start_game with starting FEN, side=W. This does "
            "NOT start a Lichess or local AI game — use the "
            "phantom_chess.start_local_game / start_game services for "
            "actual play."
        )
        # Reset internal board to starting position so live_position tracks correctly.
        self.coordinator._board = chess.Board()
        try:
            self.coordinator._state["live_fen"] = self.coordinator._board.board_fen()
            grid = self.coordinator._build_phantom_matrix_from_fen(
                self.coordinator._board.fen()
            )
            self.coordinator._state["piece_grid"] = grid
            self.coordinator._state["piece_count"] = sum(1 for c in grid if c != ".")
            self.coordinator._state["last_move"] = None
            self.coordinator._state["firmware_last_move"] = None
            self.coordinator.async_set_updated_data(dict(self.coordinator._state))
        except Exception as e:
            _LOGGER.debug("Start Game button: failed to seed live_position: %s", e)

        await self.coordinator.async_phantom_start_game(
            fen=chess.STARTING_FEN, side="W", wait_for_running_timeout_s=120.0
        )


class PhantomMovementVerifyButton(_PhantomBaseButton):
    """Send GameOPCode 3 (movementVerify '1') on cc68a66e.

    Useful as a manual probe when the firmware seems stuck after a BLE
    reconnect — it acks any pending move and often nudges the firmware
    into emitting fresh state.
    """

    _attr_name = "Movement Verify"
    _attr_icon = "mdi:check-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False  # diagnostic; off by default

    def __init__(self, coordinator, entry, address, device_name) -> None:
        super().__init__(coordinator, entry, address, device_name, "movement_verify_button")

    async def async_press(self) -> None:
        _LOGGER.info("Movement Verify (diagnostic) button: sending hex:03 31 to cc68a66e")
        await self.coordinator._phantom_send_movement_verify("1")

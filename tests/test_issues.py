"""Repair issues, user-facing service errors and their translations."""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir

from custom_components.phantom_chess import issues
from custom_components.phantom_chess import _user_facing
from custom_components.phantom_chess.const import DOMAIN

from .ble_mock import make_coordinator

PKG = Path(__file__).parent.parent / "custom_components" / "phantom_chess"
STRINGS = json.loads((PKG / "strings.json").read_text(encoding="utf-8"))


def _has(hass, issue_id: str) -> bool:
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_engine_unavailable_raises_and_ready_clears(hass) -> None:
    issues.sync_engine_issue(hass, {"status": "unavailable", "error": "glibc/aarch64"})
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issues.ISSUE_ENGINE_UNSUPPORTED)
    assert issue is not None
    assert issue.translation_placeholders == {"detail": "glibc/aarch64"}
    issues.sync_engine_issue(hass, {"status": "ready", "error": None})
    assert not _has(hass, issues.ISSUE_ENGINE_UNSUPPORTED)


async def test_engine_error_survives_transitional_states(hass) -> None:
    issues.sync_engine_issue(hass, {"status": "error", "error": "digest mismatch"})
    assert _has(hass, issues.ISSUE_ENGINE_FAILED)
    for transitional in ("downloading", "verifying", "not_checked", None):
        issues.sync_engine_issue(hass, {"status": transitional})
        assert _has(hass, issues.ISSUE_ENGINE_FAILED)
    issues.sync_engine_issue(hass, {"status": "ready"})
    assert not _has(hass, issues.ISSUE_ENGINE_FAILED)


async def test_unavailable_replaces_failed(hass) -> None:
    issues.sync_engine_issue(hass, {"status": "error", "error": "x"})
    issues.sync_engine_issue(hass, {"status": "unavailable", "error": "y"})
    assert not _has(hass, issues.ISSUE_ENGINE_FAILED)
    assert _has(hass, issues.ISSUE_ENGINE_UNSUPPORTED)


async def test_ble_route_issue_is_per_board(hass) -> None:
    issues.raise_ble_route_issue(hass, "C8:C9:A3:F2:7C:0A", "0.3.3")
    issues.raise_ble_route_issue(hass, "AA:BB:CC:DD:EE:FF", None)
    first = ir.async_get(hass).async_get_issue(DOMAIN, "ble_route_unsupported_c8c9a3f27c0a")
    assert first is not None
    assert first.severity == ir.IssueSeverity.ERROR
    assert first.translation_key == "ble_route_unsupported"
    assert first.translation_placeholders == {"address": "C8:C9:A3:F2:7C:0A", "firmware": "0.3.3"}
    issues.clear_ble_route_issue(hass, "C8:C9:A3:F2:7C:0A")
    assert not _has(hass, "ble_route_unsupported_c8c9a3f27c0a")
    assert _has(hass, "ble_route_unsupported_aabbccddeeff")


async def test_legacy_frontend_issue_is_removed(hass) -> None:
    ir.async_create_issue(
        hass, DOMAIN, "missing_frontend_deps", is_fixable=False,
        severity=ir.IssueSeverity.WARNING, translation_key="missing_frontend_deps",
    )
    issues.clear_legacy_issues(hass)
    assert not _has(hass, "missing_frontend_deps")


def test_every_issue_has_strings() -> None:
    for key in (issues.ISSUE_ENGINE_UNSUPPORTED, issues.ISSUE_ENGINE_FAILED, issues.ISSUE_BLE_ROUTE):
        entry = STRINGS["issues"][key]
        assert entry["title"] and entry["description"]
    assert STRINGS == json.loads((PKG / "translations" / "en.json").read_text(encoding="utf-8"))


def test_every_translation_key_in_code_exists() -> None:
    """Static guard: a raise with an unknown translation_key shows a blank error."""
    used: set[str] = set()
    for path in PKG.glob("*.py"):
        used |= set(re.findall(r'translation_key="([a-z0-9_]+)"', path.read_text(encoding="utf-8")))
    known = set(STRINGS["exceptions"]) | set(STRINGS["issues"])
    for platform in STRINGS.get("entity", {}).values():
        known |= set(platform)
    assert used - known == set()


def test_no_untranslated_service_errors() -> None:
    for path in PKG.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r'raise (ServiceValidationError|HomeAssistantError)\(\s*["f]', text), path.name
        assert "ServiceValidationError(str(err))" not in text, path.name


async def test_user_facing_maps_refusal_to_validation_error() -> None:
    wrapped = _user_facing(AsyncMock(side_effect=RuntimeError("A chess game is already running.")))
    with pytest.raises(ServiceValidationError) as info:
        await wrapped(MagicMock())
    assert info.value.translation_key == "request_rejected"
    assert info.value.translation_placeholders == {"error": "A chess game is already running."}
    assert isinstance(info.value.__cause__, RuntimeError)


async def test_user_facing_maps_bluetooth_failure() -> None:
    wrapped = _user_facing(AsyncMock(side_effect=BleakError("not connected")))
    with pytest.raises(HomeAssistantError) as info:
        await wrapped(MagicMock())
    assert not isinstance(info.value, ServiceValidationError)
    assert info.value.translation_key == "board_communication_failed"


async def test_user_facing_passes_ha_errors_and_results() -> None:
    original = ServiceValidationError(translation_domain=DOMAIN, translation_key="no_board_configured")
    with pytest.raises(ServiceValidationError) as info:
        await _user_facing(AsyncMock(side_effect=original))(MagicMock())
    assert info.value is original
    assert await _user_facing(AsyncMock(return_value={"ok": 1}))(MagicMock()) == {"ok": 1}
    with pytest.raises(ServiceValidationError):
        await _user_facing(AsyncMock(side_effect=ValueError("")))(MagicMock())


async def test_game_start_length_rejection_raises_route_issue() -> None:
    coordinator = make_coordinator()
    coordinator._state["firmware_version"] = "0.3.3"
    coordinator._ble_write = AsyncMock(side_effect=BleakError("ATT error: 0x0d (INVALID_ATTRIBUTE_VALUE_LENGTH)"))
    with patch("custom_components.phantom_chess.coordinator.raise_ble_route_issue") as raised, \
         patch("custom_components.phantom_chess.coordinator.clear_ble_route_issue") as cleared:
        with pytest.raises(Exception):
            await coordinator._phantom_send_game_start()
    raised.assert_called_once_with(coordinator.hass, coordinator._ble_address, "0.3.3")
    cleared.assert_not_called()


async def test_game_start_success_clears_route_issue() -> None:
    coordinator = make_coordinator()
    coordinator._ble_write = AsyncMock()
    with patch("custom_components.phantom_chess.coordinator.clear_ble_route_issue") as cleared:
        await coordinator._phantom_send_game_start()
    cleared.assert_called_once_with(coordinator.hass, coordinator._ble_address)


async def test_game_start_other_ble_errors_do_not_raise_issue() -> None:
    coordinator = make_coordinator()
    coordinator._ble_write = AsyncMock(side_effect=BleakError("disconnected"))
    with patch("custom_components.phantom_chess.coordinator.raise_ble_route_issue") as raised:
        with pytest.raises(BleakError):
            await coordinator._phantom_send_game_start()
    raised.assert_not_called()


async def test_route_issue_failure_never_breaks_game_start() -> None:
    coordinator = make_coordinator()
    coordinator._ble_write = AsyncMock()
    with patch("custom_components.phantom_chess.coordinator.clear_ble_route_issue",
               side_effect=KeyError("registry not loaded")):
        await coordinator._phantom_send_game_start()
    coordinator._ble_write.assert_awaited_once()


async def test_engine_state_publisher_syncs_issue() -> None:
    coordinator = make_coordinator()
    with patch("custom_components.phantom_chess.sessions.sync_engine_issue") as synced:
        coordinator._publish_engine_state({"status": "ready", "error": None})
    synced.assert_called_once_with(coordinator.hass, {"status": "ready", "error": None})
    assert coordinator._state["engine_health"] == {"status": "ready", "error": None}

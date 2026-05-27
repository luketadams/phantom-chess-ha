"""Tests for the Phantom Chess config flow.

These tests require `pytest-homeassistant-custom-component` which provides
the `hass` fixture and the full HA test loop:

    pip install pytest-homeassistant-custom-component
    pytest custom_components/phantom_chess/tests/test_config_flow.py

Placeholder scaffolding — fill in as the project's test infrastructure
matures. The patterns below are the standard HA config-flow test idioms.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Skip the entire module if the HA test plugin isn't installed.
pytest_homeassistant = pytest.importorskip("pytest_homeassistant_custom_component")


from homeassistant import config_entries  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402

from custom_components.phantom_chess.const import (  # noqa: E402
    CONF_BLE_ADDRESS,
    CONF_LICHESS_TOKEN,
    CONF_LICHESS_USER,
    DOMAIN,
)


# ─── Bluetooth discovery → confirm → token → entry creation ─────────────


async def test_user_flow_with_valid_token(
    hass: HomeAssistant,
    mock_lichess_account_response_valid,
    mock_aiohttp_session_factory,
) -> None:
    """End-to-end happy path: user enters MAC, valid token, entry created."""
    valid_session = mock_aiohttp_session_factory(
        status=200, json_data=mock_lichess_account_response_valid
    )

    with patch(
        "custom_components.phantom_chess.config_flow.async_get_clientsession",
        return_value=valid_session,
    ), patch(
        "custom_components.phantom_chess.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        # Step 1: launch the user flow
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"

        # Step 2: provide MAC address
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        # Should advance to lichess_token step
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "lichess_token"

        # Step 3: provide Lichess token; expect entry creation
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_LICHESS_TOKEN: "test-token-abc123"},
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_BLE_ADDRESS] == "AA:BB:CC:DD:EE:FF"
        assert result["data"][CONF_LICHESS_TOKEN] == "test-token-abc123"
        assert result["data"][CONF_LICHESS_USER] == "TestUser"


async def test_user_flow_with_invalid_token(
    hass: HomeAssistant,
    mock_aiohttp_session_factory,
) -> None:
    """Invalid Lichess token should surface as a form error."""
    invalid_session = mock_aiohttp_session_factory(status=401, json_data={})

    with patch(
        "custom_components.phantom_chess.config_flow.async_get_clientsession",
        return_value=invalid_session,
    ), patch(
        "custom_components.phantom_chess.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_LICHESS_TOKEN: "bad-token"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {CONF_LICHESS_TOKEN: "invalid_lichess_token"}


# ─── Reauth flow ────────────────────────────────────────────────────────


async def test_reauth_flow_success(
    hass: HomeAssistant,
    mock_lichess_account_response_valid,
    mock_aiohttp_session_factory,
) -> None:
    """Reauth: existing entry, user enters new valid token, entry updated."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_LICHESS_TOKEN: "old-expired-token",
            CONF_LICHESS_USER: "OldUser",
        },
    )
    entry.add_to_hass(hass)

    valid_session = mock_aiohttp_session_factory(
        status=200, json_data=mock_lichess_account_response_valid
    )
    with patch(
        "custom_components.phantom_chess.config_flow.async_get_clientsession",
        return_value=valid_session,
    ):
        result = await entry.start_reauth_flow(hass)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_LICHESS_TOKEN: "new-valid-token"},
        )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert entry.data[CONF_LICHESS_TOKEN] == "new-valid-token"
        assert entry.data[CONF_LICHESS_USER] == "TestUser"


# ─── Options flow ───────────────────────────────────────────────────────


async def test_options_flow_round_trip(hass: HomeAssistant) -> None:
    """Options flow: open, set TTS overrides + debug toggle, save."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_LICHESS_TOKEN: "some-token",
            CONF_LICHESS_USER: "TestUser",
        },
        options={},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "tts_service": "tts.example",
            "tts_media_player_entity_id": "media_player.example",
            "debug_dump": True,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    # auto_provision_dashboard is required-with-default-True in the schema
    # (added in v0.4.0-alpha4 along with the auto-provisioned dashboard),
    # so the saved options always include it even when the user doesn't
    # touch the toggle.
    assert entry.options == {
        "tts_service": "tts.example",
        "tts_media_player_entity_id": "media_player.example",
        "debug_dump": True,
        "auto_provision_dashboard": True,
    }

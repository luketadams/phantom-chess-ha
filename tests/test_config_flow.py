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


# ─── Bluetooth-discovery flow ───────────────────────────────────────────


def _phantom_bluetooth_service_info(address: str = "AA:BB:CC:DD:EE:FF",
                                     name: str = "Phantom 1234"):
    """Build a BluetoothServiceInfoBleak fixture for a discovered Phantom.

    Imports here (not at module top) so the file still loads in the
    matrix-tests minimal environment, where homeassistant.components is
    stubbed and the bluetooth import would fail before
    pytest.importorskip can save us.
    """
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=["fd31a840-22e7-11eb-adc1-0242ac120002"],
        source="local",
        device=MagicMock(),
        advertisement=MagicMock(),
        connectable=True,
        time=0.0,
        tx_power=-127,
    )


async def test_bluetooth_discovery_creates_entry(
    hass: HomeAssistant,
    mock_lichess_account_response_valid,
    mock_aiohttp_session_factory,
) -> None:
    """Bluetooth discovery → confirm → token → entry creation."""
    valid_session = mock_aiohttp_session_factory(
        status=200, json_data=mock_lichess_account_response_valid
    )

    with patch(
        "custom_components.phantom_chess.config_flow.async_get_clientsession",
        return_value=valid_session,
    ):
        # Step 1: bluetooth discovery initiates the flow at bluetooth_confirm.
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=_phantom_bluetooth_service_info(),
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "bluetooth_confirm"
        assert result["description_placeholders"]["address"] == "AA:BB:CC:DD:EE:FF"

        # Step 2: confirming advances to the Lichess-token step (no token in
        # this submission — bluetooth_confirm has an empty form).
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "lichess_token"

        # Step 3: provide token → entry is created with the discovered MAC.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_LICHESS_TOKEN: "bt-discovered-token"},
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_BLE_ADDRESS] == "AA:BB:CC:DD:EE:FF"
        assert result["data"][CONF_LICHESS_TOKEN] == "bt-discovered-token"
        assert result["data"][CONF_LICHESS_USER] == "TestUser"


async def test_bluetooth_discovery_aborts_when_already_configured(
    hass: HomeAssistant,
) -> None:
    """A second bluetooth discovery of an already-configured board aborts."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_LICHESS_TOKEN: "already-set-token",
            CONF_LICHESS_USER: "ExistingUser",
        },
    )
    entry.add_to_hass(hass)

    # No need to mock Lichess — the abort fires before any HTTP call.
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=_phantom_bluetooth_service_info(),
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ─── User-flow edge cases ───────────────────────────────────────────────


async def test_user_flow_invalid_mac_format(hass: HomeAssistant) -> None:
    """Malformed MAC (not six hex pairs) surfaces an inline form error."""
    with patch(
        "custom_components.phantom_chess.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_BLE_ADDRESS: "not-a-mac-address"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {CONF_BLE_ADDRESS: "invalid_ble_address"}


async def test_user_flow_lichess_network_error_falls_back_to_error(
    hass: HomeAssistant,
    mock_aiohttp_session_factory,
) -> None:
    """If the Lichess token-validation HTTP call raises, the form returns
    the same `invalid_lichess_token` error as a 401 response would.
    """
    from unittest.mock import MagicMock

    # Build a session whose .get raises aiohttp.ClientError on entry.
    import aiohttp
    session = MagicMock()
    error_cm = MagicMock()
    error_cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("network down"))
    error_cm.__aexit__ = AsyncMock(return_value=None)
    session.get = MagicMock(return_value=error_cm)

    with patch(
        "custom_components.phantom_chess.config_flow.async_get_clientsession",
        return_value=session,
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
            {CONF_LICHESS_TOKEN: "some-token"},
        )
        # Network error is treated as invalid token — same UX.
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {CONF_LICHESS_TOKEN: "invalid_lichess_token"}


async def test_user_flow_lichess_timeout_falls_back_to_error(
    hass: HomeAssistant,
) -> None:
    """C7: a token-validation TIMEOUT (asyncio.TimeoutError, e.g. the
    ClientTimeout total elapsing) must land on the same `invalid_lichess_token`
    form as a ClientError — not a generic "Unknown error" page.

    Falsifiable: pre-C7 the except caught only aiohttp.ClientError, so the
    TimeoutError escaped _validate_lichess_token and the flow raised / showed
    the unknown-error page instead of this handled form."""
    import asyncio
    from unittest.mock import MagicMock

    session = MagicMock()
    timeout_cm = MagicMock()
    timeout_cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
    timeout_cm.__aexit__ = AsyncMock(return_value=None)
    session.get = MagicMock(return_value=timeout_cm)

    with patch(
        "custom_components.phantom_chess.config_flow.async_get_clientsession",
        return_value=session,
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
            {CONF_LICHESS_TOKEN: "some-token"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {CONF_LICHESS_TOKEN: "invalid_lichess_token"}


# ─── Reauth-flow edge cases ─────────────────────────────────────────────


async def test_reauth_flow_rejects_invalid_new_token(
    hass: HomeAssistant,
    mock_aiohttp_session_factory,
) -> None:
    """Reauth with an invalid replacement token shows the form error and
    leaves the existing entry untouched.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_LICHESS_TOKEN: "old-token",
            CONF_LICHESS_USER: "OldUser",
        },
    )
    entry.add_to_hass(hass)

    invalid_session = mock_aiohttp_session_factory(status=401, json_data={})
    with patch(
        "custom_components.phantom_chess.config_flow.async_get_clientsession",
        return_value=invalid_session,
    ):
        result = await entry.start_reauth_flow(hass)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_LICHESS_TOKEN: "still-bad-token"},
        )
        # Form remains open with the inline error.
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {CONF_LICHESS_TOKEN: "invalid_lichess_token"}
        # Original entry data untouched.
        assert entry.data[CONF_LICHESS_TOKEN] == "old-token"
        assert entry.data[CONF_LICHESS_USER] == "OldUser"


# ─── Migration v1 → v3 (MAC canonicalisation) ──────────────────────────


async def test_migrate_entry_canonicalises_mac_to_uppercase(
    hass: HomeAssistant,
) -> None:
    """A pre-v3 entry stored with a mixed-case MAC is upgraded to upper-case.

    The migration runs at setup time but we call it directly here to avoid
    spinning up the full coordinator + platform stack. This was the cause
    of the "perpetually Discovered" bug fixed in v0.3.0; the test guards
    against regression of the canonicalisation logic itself.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.phantom_chess import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id="c8:C9:a3:F2:7C:0a",
        data={
            CONF_BLE_ADDRESS: "c8:C9:a3:F2:7C:0a",
            CONF_LICHESS_TOKEN: "tok",
            CONF_LICHESS_USER: "user",
        },
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)
    assert result is True

    # After migration:
    # - unique_id is upper-case
    # - CONF_BLE_ADDRESS is upper-case
    # - entry version is bumped
    assert entry.unique_id == "C8:C9:A3:F2:7C:0A"
    assert entry.data[CONF_BLE_ADDRESS] == "C8:C9:A3:F2:7C:0A"
    assert entry.version >= 3


# ─── Reconfigure flow (Gold rule, alpha19) ──────────────────────────────


async def test_reconfigure_flow_updates_lichess_token(
    hass: HomeAssistant,
    mock_lichess_account_response_valid,
    mock_aiohttp_session_factory,
) -> None:
    """Reconfigure: existing entry, user pastes a fresh token, entry updated."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_LICHESS_TOKEN: "old-token",
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
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
                CONF_LICHESS_TOKEN: "fresh-rotated-token",
            },
        )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.data[CONF_LICHESS_TOKEN] == "fresh-rotated-token"
        # Username auto-refreshed from the validated token's /api/account response
        assert entry.data[CONF_LICHESS_USER] == "TestUser"


async def test_reconfigure_flow_blank_token_is_no_op(
    hass: HomeAssistant,
) -> None:
    """Submitting the form with the BLE address unchanged and the token
    field blank aborts cleanly without changing anything."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_LICHESS_TOKEN: "kept-token",
            CONF_LICHESS_USER: "KeptUser",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_LICHESS_TOKEN: "",
        },
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_LICHESS_TOKEN] == "kept-token"
    assert entry.data[CONF_LICHESS_USER] == "KeptUser"


async def test_reconfigure_flow_rejects_ble_address_change(
    hass: HomeAssistant,
) -> None:
    """A reconfigure flow that tries to change the BLE address aborts with
    `ble_address_mismatch` — that would be a different physical board,
    which should be a new entry, not a reconfigure."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_LICHESS_TOKEN: "tok",
            CONF_LICHESS_USER: "User",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BLE_ADDRESS: "11:22:33:44:55:66",  # different MAC
            CONF_LICHESS_TOKEN: "",
        },
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "ble_address_mismatch"
    # Original entry data unchanged
    assert entry.data[CONF_BLE_ADDRESS] == "AA:BB:CC:DD:EE:FF"


async def test_reconfigure_flow_rejects_invalid_token(
    hass: HomeAssistant,
    mock_aiohttp_session_factory,
) -> None:
    """A reconfigure flow with an invalid new token shows the form error
    and leaves the entry untouched."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_LICHESS_TOKEN: "good-token",
            CONF_LICHESS_USER: "GoodUser",
        },
    )
    entry.add_to_hass(hass)

    invalid_session = mock_aiohttp_session_factory(status=401, json_data={})
    with patch(
        "custom_components.phantom_chess.config_flow.async_get_clientsession",
        return_value=invalid_session,
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
                CONF_LICHESS_TOKEN: "still-bad-token",
            },
        )
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {CONF_LICHESS_TOKEN: "invalid_lichess_token"}
        # Original data untouched
        assert entry.data[CONF_LICHESS_TOKEN] == "good-token"


async def test_migrate_entry_v3_to_v4_bumps_version(
    hass: HomeAssistant,
) -> None:
    """A v3 entry migrates to v4 (the entity-id collision-rename path).

    With no _2-suffixed entities in the registry the migration's main work
    is a no-op, but the version field gets bumped so HA stops re-running
    the migration on every load.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.phantom_chess import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_LICHESS_TOKEN: "tok",
            CONF_LICHESS_USER: "user",
        },
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version >= 4
    # MAC was already canonical — should stay that way.
    assert entry.unique_id == "AA:BB:CC:DD:EE:FF"

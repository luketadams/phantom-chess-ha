"""Tests for the diagnostics module.

Covers:
- ``_mask_ble_address``: redaction of MAC's middle bytes.
- ``_mask_username``: redaction of Lichess username's middle chars.
- ``async_get_config_entry_diagnostics``: the full dump shape, with the
  coordinator's runtime_data both set (loaded=True) and absent (loaded=False).

Run:
    pytest tests/test_diagnostics.py
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.phantom_chess.diagnostics import (
    _mask_ble_address,
    _mask_username,
    async_get_config_entry_diagnostics,
)


def _run(coro):
    """asyncio.run wrapper so tests don't need pytest-asyncio."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _stub_async_get_integration():
    """Make ``async_get_integration`` hermetic in the ha-tests env.

    ``async_get_config_entry_diagnostics`` calls the real HA loader helper
    ``homeassistant.loader.async_get_integration(hass, DOMAIN)`` to read the
    manifest version. These tests pass a ``MagicMock()`` for ``hass`` (they
    only exercise the redaction/shape logic, not a live HA instance). When HA
    is actually installed (the ha-tests job / this sandbox), that helper
    dives into loader internals with the mock and never returns — the test
    hangs. Patching it to an ``AsyncMock`` keeps the tests fast and
    deterministic while still exercising the ``str(integration.version)``
    branch. In the minimal matrix-tests env ``homeassistant.loader`` isn't
    importable, the function's own ``except Exception`` already handles it
    (version -> "unknown"), and this fixture no-ops. See
    FINDINGS_TEST_PORTABILITY.md.
    """
    try:
        import homeassistant.loader  # noqa: F401
    except Exception:
        yield
        return
    integration = MagicMock()
    integration.version = "0.4.0b3"
    with patch(
        "homeassistant.loader.async_get_integration",
        AsyncMock(return_value=integration),
    ):
        yield


# ─── _mask_ble_address ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address,expected",
    [
        ("C8:C9:A3:F2:7C:0A", "C8:**:**:**:**:0A"),
        ("AA:BB:CC:DD:EE:FF", "AA:**:**:**:**:FF"),
        # Case preserved (no canonicalisation here)
        ("aa:bb:cc:dd:ee:ff", "aa:**:**:**:**:ff"),
    ],
)
def test_mask_ble_address_correct_form(address: str, expected: str) -> None:
    assert _mask_ble_address(address) == expected


def test_mask_ble_address_none_passes_through() -> None:
    assert _mask_ble_address(None) is None


def test_mask_ble_address_empty_passes_through() -> None:
    assert _mask_ble_address("") == ""


@pytest.mark.parametrize(
    "malformed",
    [
        "no-colons",
        "AA:BB:CC",         # too few octets
        "AA:BB:CC:DD:EE:FF:GG",  # too many octets
    ],
)
def test_mask_ble_address_malformed_returns_marker(malformed: str) -> None:
    assert _mask_ble_address(malformed) == "[malformed]"


# ─── _mask_username ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "username,expected",
    [
        # First 2 chars + last char visible, middle masked
        ("lukeadams", "lu******s"),
        ("TestUser", "Te*****r"),
        # 4-char usernames: first 2 + 1 star + last = 4 chars
        ("abcd", "ab*d"),
    ],
)
def test_mask_username_correct_form(username: str, expected: str) -> None:
    assert _mask_username(username) == expected


def test_mask_username_too_short_returns_redacted_marker() -> None:
    """Usernames shorter than 4 chars are fully redacted (no useful
    correlation prefix possible)."""
    assert _mask_username("abc") == "[redacted]"
    assert _mask_username("ab") == "[redacted]"
    assert _mask_username("") == "[redacted]"


def test_mask_username_none_returns_redacted_marker() -> None:
    assert _mask_username(None) == "[redacted]"


# ─── async_get_config_entry_diagnostics ─────────────────────────────────


def _stub_entry(*, runtime_data=None, options=None):
    """A minimal entry-like object for the diagnostics function."""
    entry = MagicMock()
    entry.entry_id = "01HMOCKENTRYID"
    entry.title = "Phantom Chess Board (mocked)"
    entry.data = {
        # Match the actual const.py keys: lichess_username (not lichess_user).
        "ble_address": "C8:C9:A3:F2:7C:0A",
        "lichess_token": "secret-token-do-not-leak",
        "lichess_username": "TestUser",
        "device_name": "Phantom Test",
    }
    entry.options = options or {}
    entry.runtime_data = runtime_data
    return entry


def test_diagnostics_with_no_coordinator_marks_unloaded() -> None:
    """When runtime_data is unset, runtime.loaded is False."""
    entry = _stub_entry(runtime_data=None)
    out = _run(async_get_config_entry_diagnostics(hass=MagicMock(), entry=entry))

    assert out["integration"]["domain"] == "phantom_chess"
    assert out["integration"]["entry_id"] == entry.entry_id
    assert out["runtime"]["loaded"] is False


def test_diagnostics_redacts_lichess_token() -> None:
    """The Lichess token is in TO_REDACT — async_redact_data masks it."""
    entry = _stub_entry(runtime_data=None)
    out = _run(async_get_config_entry_diagnostics(hass=MagicMock(), entry=entry))

    assert out["entry_data"]["lichess_token"] != "secret-token-do-not-leak"
    # async_redact_data replaces with a placeholder like "**REDACTED**"
    # (don't pin the exact form — just verify the secret isn't present).
    assert "secret-token-do-not-leak" not in str(out)


def test_diagnostics_masks_ble_address_and_username() -> None:
    """BLE address and Lichess username are partially masked, not fully
    redacted — so users can correlate multi-report bugs without leaking
    the device-uniquely-identifying middle bytes."""
    entry = _stub_entry(runtime_data=None)
    out = _run(async_get_config_entry_diagnostics(hass=MagicMock(), entry=entry))

    assert out["entry_data"]["ble_address"] == "C8:**:**:**:**:0A"
    assert out["entry_data"]["lichess_username"] == "Te*****r"


def test_diagnostics_with_loaded_coordinator() -> None:
    """When runtime_data carries a coordinator, runtime.loaded is True and
    the keys-of-interest from coordinator.data are surfaced."""
    coord = MagicMock()
    coord.is_ble_connected = True
    coord.ai_level = 5
    coord.player_color = "white"
    coord.paused = False
    coord._phantom_session_initialized = True
    coord._game_id = None
    coord._analysis_client = None
    coord.data = {
        "firmware_version": "0.3.0",
        "firmware_mode": "Running",
        "lichess_active": False,
        "piece_count": 32,
        # A noisy key NOT in keys_of_interest — should be excluded.
        "move_history_moves": ["lots", "of", "moves"],
    }

    entry = _stub_entry(runtime_data=coord)
    out = _run(async_get_config_entry_diagnostics(hass=MagicMock(), entry=entry))

    runtime = out["runtime"]
    assert runtime["loaded"] is True
    assert runtime["ble_connected"] is True
    assert runtime["ai_level"] == 5
    assert runtime["player_color"] == "white"
    assert runtime["paused"] is False
    assert runtime["lichess_game_id_set"] is False
    # Surfaced state fields
    assert runtime["state"]["firmware_version"] == "0.3.0"
    assert runtime["state"]["firmware_mode"] == "Running"
    assert runtime["state"]["piece_count"] == 32
    # Filtered-out noisy field
    assert "move_history_moves" not in runtime["state"]


def test_diagnostics_environment_block_populated() -> None:
    """The environment block has the architecture detection useful for
    Stockfish-download triage."""
    entry = _stub_entry(runtime_data=None)
    out = _run(async_get_config_entry_diagnostics(hass=MagicMock(), entry=entry))

    env = out["environment"]
    assert "platform_machine" in env
    assert "platform_system" in env
    assert "python_version" in env
    assert isinstance(env["musl_linker_present"], bool)


def test_diagnostics_stockfish_block_empty_when_client_absent() -> None:
    """stockfish.client_initialized=False when there's no analysis client."""
    entry = _stub_entry(runtime_data=None)
    out = _run(async_get_config_entry_diagnostics(hass=MagicMock(), entry=entry))
    assert out["stockfish"] == {"client_initialized": False}

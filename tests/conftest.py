"""Shared pytest fixtures for Phantom Chess tests.

Most tests rely on `pytest-homeassistant-custom-component` which provides
the `hass` fixture and enables HA's test loop. To run:

    pip install pytest-homeassistant-custom-component
    pytest custom_components/phantom_chess/tests/
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_lichess_account_response_valid() -> dict:
    """A successful /api/account response shape."""
    return {
        "id": "testuser",
        "username": "TestUser",
        "perfs": {"blitz": {"rating": 1400}},
    }


@pytest.fixture
def mock_aiohttp_session_factory():
    """Factory for an async-context-manager session mock.

    Usage:
        session = mock_aiohttp_session_factory(status=200, json_data={...})
        with patch("...async_get_clientsession", return_value=session):
            ...
    """
    def _factory(status: int = 200, json_data: dict | None = None) -> MagicMock:
        resp_cm = MagicMock()
        resp_cm.__aenter__ = AsyncMock(
            return_value=MagicMock(
                status=status,
                json=AsyncMock(return_value=json_data or {}),
                text=AsyncMock(return_value=""),
            )
        )
        resp_cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=resp_cm)
        session.post = MagicMock(return_value=resp_cm)
        return session
    return _factory


@pytest.fixture
def mock_ble_device():
    """Minimal mock of a discovered BLE device."""
    device = MagicMock()
    device.address = "AA:BB:CC:DD:EE:FF"
    device.name = "Phantom 1234"
    return device

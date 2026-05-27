"""Shared pytest fixtures for Phantom Chess tests.

Most tests rely on `pytest-homeassistant-custom-component` which provides
the `hass` fixture and enables HA's test loop. To run:

    pip install pytest-homeassistant-custom-component
    pytest custom_components/phantom_chess/tests/

Pure-function tests (matrix, dashboard_provision text passes) should
still run in a minimal CI environment without the full HA dependency
tree. The package's ``__init__.py`` imports ``voluptuous`` and a big
slice of ``homeassistant.*``, so loading any submodule normally drags
in HA. The stub below detects the minimal environment (no
``voluptuous`` installed) and stages the pure submodules into
``sys.modules`` directly, bypassing ``__init__.py``. Tests can keep
using the natural ``from custom_components.phantom_chess.matrix
import ...`` syntax in both environments.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PC_DIR = _REPO_ROOT / "custom_components" / "phantom_chess"


def _stage_pure_module(qualified_name: str, file_path: Path) -> None:
    """Load a single .py file into sys.modules under ``qualified_name``."""
    spec = importlib.util.spec_from_file_location(qualified_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified_name] = module
    spec.loader.exec_module(module)


try:  # noqa: SIM105
    import voluptuous  # noqa: F401
except ImportError:
    # Minimal environment — pre-stage the pure-function submodules so
    # `from custom_components.phantom_chess.X import ...` works without
    # running the package's HA-heavy __init__.py.
    _cc_stub = sys.modules.setdefault(
        "custom_components", types.ModuleType("custom_components")
    )
    _cc_stub.__path__ = [str(_REPO_ROOT / "custom_components")]
    _pc_stub = types.ModuleType("custom_components.phantom_chess")
    _pc_stub.__path__ = [str(_PC_DIR)]
    sys.modules["custom_components.phantom_chess"] = _pc_stub
    # matrix.py is fully pure (only stdlib).
    _stage_pure_module("custom_components.phantom_chess.matrix", _PC_DIR / "matrix.py")


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

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

    # dashboard_provision.py: pure-function rendering paths (text + dict
    # passes) only, but it imports from `homeassistant.*` and from
    # `.const`. Stub the HA modules with the exact symbols
    # dashboard_provision references at module load time, then stage
    # `.const` (pure-stdlib) and `dashboard_provision` itself. Anything
    # that touches the storage / panel-registration side effects
    # (`async_provision_dashboard`, etc.) won't work in this stub
    # environment — only the offline render pipeline is exercisable.
    for _ha_path in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.components.bluetooth",
        "homeassistant.components.frontend",
        "homeassistant.components.lovelace",
        "homeassistant.components.lovelace.dashboard",
        "homeassistant.components.lovelace.const",
        "homeassistant.config_entries",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.entity_registry",
        "homeassistant.helpers.issue_registry",
        "homeassistant.helpers.storage",
        "homeassistant.helpers.update_coordinator",
    ):
        sys.modules.setdefault(_ha_path, types.ModuleType(_ha_path))
    _ll_const = sys.modules["homeassistant.components.lovelace.const"]
    _ll_const.CONF_ICON = "icon"
    _ll_const.CONF_REQUIRE_ADMIN = "require_admin"
    _ll_const.CONF_SHOW_IN_SIDEBAR = "show_in_sidebar"
    _ll_const.CONF_TITLE = "title"
    _ll_const.CONF_URL_PATH = "url_path"
    _ll_const.LOVELACE_DATA = "lovelace_data"
    _ll_const.MODE_STORAGE = "storage"
    sys.modules["homeassistant.config_entries"].ConfigEntry = type("ConfigEntry", (), {})
    sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (), {})
    sys.modules["homeassistant.helpers.entity_registry"].async_get = lambda *a, **k: None
    sys.modules["homeassistant.helpers.storage"].Store = type("Store", (), {})
    sys.modules["homeassistant.components.lovelace.dashboard"].LovelaceStorage = type(
        "LovelaceStorage", (), {}
    )
    sys.modules["homeassistant.components.frontend"].async_panel_exists = lambda *a, **k: False
    sys.modules["homeassistant.components.frontend"].async_register_built_in_panel = (
        lambda *a, **k: None
    )
    sys.modules["homeassistant.components.frontend"].async_remove_panel = lambda *a, **k: None
    sys.modules["homeassistant.helpers"].entity_registry = sys.modules[
        "homeassistant.helpers.entity_registry"
    ]
    _stage_pure_module("custom_components.phantom_chess.const", _PC_DIR / "const.py")
    _stage_pure_module(
        "custom_components.phantom_chess.dashboard_provision",
        _PC_DIR / "dashboard_provision.py",
    )
    # lichess_analysis imports aiohttp + chess at module top. Both are
    # installable in the minimal env (chess is the integration's own
    # runtime dep; aiohttp is small and pure-python). No HA imports
    # inside it that we'd need to stub.
    _stage_pure_module(
        "custom_components.phantom_chess.lichess_analysis",
        _PC_DIR / "lichess_analysis.py",
    )

    # coordinator.py: heavy module that pulls in HA's bluetooth +
    # aiohttp_client + update_coordinator helpers. Stub everything it
    # touches at module-load time. Only the pure-function helpers at the
    # module top (`_phantom_to_uci`, `_rotate_uci_180`) and the static-
    # methods inside `PhantomChessCoordinator` (`_coarse_accuracy`,
    # `_describe_mistake`) are unit-testable from this stub; everything
    # else needs the full HA test loop in the ha-tests job.
    sys.modules["homeassistant.components.bluetooth"].async_ble_device_from_address = (
        lambda *a, **k: None
    )
    sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = (
        lambda *a, **k: None
    )
    sys.modules["homeassistant.helpers.issue_registry"].async_delete_issue = (
        lambda *a, **k: None
    )
    # DataUpdateCoordinator is subscripted as `DataUpdateCoordinator[dict[str, Any]]`
    # at class-definition time in coordinator.py — the stub needs to support
    # generic subscription. Easiest is to give it a __class_getitem__ that
    # returns the class itself, matching how typing.Generic works.
    class _StubDataUpdateCoordinator:
        def __class_getitem__(cls, _params):
            return cls
    sys.modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = (
        _StubDataUpdateCoordinator
    )
    sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed = type(
        "UpdateFailed", (Exception,), {}
    )
    _stage_pure_module(
        "custom_components.phantom_chess.coordinator",
        _PC_DIR / "coordinator.py",
    )


# pytest-homeassistant-custom-component scopes integration discovery to
# the built-in HA components by default — custom integrations in
# `custom_components/` are NOT loaded unless the test opts in via this
# fixture. Marking it autouse so every HA test (which always wants
# phantom_chess registered) gets it without per-test boilerplate.
#
# Wrapped in try/except so the file still imports in the minimal
# environment where pytest-homeassistant-custom-component isn't
# installed (matrix-tests job).
try:
    from pytest_homeassistant_custom_component.common import (  # noqa: F401
        MockConfigEntry,
    )

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Auto-enable custom_components/phantom_chess discovery for every HA test."""
        yield
except ImportError:
    pass


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

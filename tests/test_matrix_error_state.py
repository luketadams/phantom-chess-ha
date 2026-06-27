"""Coordinator handling of a matrix ERROR payload that carries no grid.

This is the firmware wedge the user's board gets stuck in:
`firmware_mode == "Snapping Pieces"` + a repeating
`ERROR: Chessboard and sensor matrix do not match.` notification that may
arrive with no parseable trailing matrix. The coordinator must surface the
error status (so the dashboard/user can see it) WITHOUT trying to run the
grid-dependent computations (which would crash on a None grid).

`_apply_matrix_state` only touches `self._state` and
`self.async_set_updated_data`, so — like the two-player and voice tests —
we bind the unbound method to a lightweight stub. Runs in the minimal CI
env (no HA, no board). v0.4-beta3 (finding C2).
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock

from custom_components.phantom_chess.coordinator import PhantomChessCoordinator


def _stub() -> types.SimpleNamespace:
    stub = types.SimpleNamespace()
    stub._state = {}
    stub.async_set_updated_data = MagicMock()
    return stub


def _error_payload() -> dict:
    return {
        "raw": "ERROR: Chessboard and sensor matrix do not match.",
        "piece_grid": None,
        "sensor_bitmap": None,
        "status": "Error",
        "status_message": "Chessboard and sensor matrix do not match",
    }


def test_error_without_grid_surfaces_status() -> None:
    stub = _stub()
    PhantomChessCoordinator._apply_matrix_state(stub, _error_payload())

    assert stub._state["matrix_status"] == "Error"
    assert stub._state["matrix_status_message"] == "Chessboard and sensor matrix do not match"
    assert stub._state["matrix_raw"].startswith("ERROR:")
    assert stub._state.get("matrix_last_updated")
    # Grid-dependent fields must NOT have been computed/set.
    assert "piece_grid" not in stub._state
    assert "live_fen" not in stub._state
    assert "piece_count" not in stub._state
    stub.async_set_updated_data.assert_called_once()


def test_error_without_grid_dedups_repeats() -> None:
    """The firmware spams this error ~every 2s; identical repeats must not
    re-fire the coordinator update (no entity-update storm)."""
    stub = _stub()
    PhantomChessCoordinator._apply_matrix_state(stub, _error_payload())
    PhantomChessCoordinator._apply_matrix_state(stub, _error_payload())
    # Second identical payload is deduped -> only one update.
    stub.async_set_updated_data.assert_called_once()

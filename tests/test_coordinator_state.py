"""Tests for the synchronous state-mutator methods on PhantomChessCoordinator.

The coordinator is mostly async BLE/Lichess infrastructure, but it also
contains a layer of pure-data state mutators that get called from
notification handlers and poll loops:

- ``_apply_firmware_mode_state`` — parses firmware_mode text payload
  (mode label OR move event) and updates ``self._state``.
- ``_apply_battery_state`` — stores percent + charging flag.
- ``_apply_matrix_state`` — parses the matrix notification payload
  (CLEAN/ERROR + 10×10 grid + 10×10 sensor bitmap), recomputes derived
  fields (live_fen, piece_count, position_consistent, mismatches), and
  drives the persistent_notification side effect.
- ``_blank_state`` — initial state-dict shape used at coordinator boot.

These methods are synchronous and only need a stubbed coordinator
instance with ``self._state``, ``self.hass``, and a no-op
``async_set_updated_data``.

Run:
    pytest tests/test_coordinator_state.py
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.phantom_chess.coordinator import PhantomChessCoordinator


# ─── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def coord() -> PhantomChessCoordinator:
    """A coordinator instance with only the attributes the state mutators
    need. Bypasses ``__init__`` (which calls DataUpdateCoordinator.__init__
    with HA scheduling primitives) via ``__new__``.
    """
    c = PhantomChessCoordinator.__new__(PhantomChessCoordinator)
    # The state mutators read/write self._state freely.
    c._state = {}
    # _apply_matrix_state's notification path checks this against the
    # current mismatch signature; None means "no prior notification" so
    # the first ERROR payload triggers a create.
    c._last_mismatch_signature = None
    # async_set_updated_data is the DataUpdateCoordinator API — stub it
    # so the mutators can call it without crashing.
    c.async_set_updated_data = MagicMock()
    # _update_mismatch_notification fans out to hass services. Stub hass
    # with the only attributes the path touches.
    c.hass = MagicMock()
    # _debug_dump_enabled checks self._entry.options — coordinator code
    # tolerates None via the getattr/None-check pattern.
    c._entry = None
    return c


# ─── _apply_firmware_mode_state ─────────────────────────────────────────


def test_apply_firmware_mode_state_mode_label_writes_state(
    coord: PhantomChessCoordinator,
) -> None:
    """A mode label like 'Running' is stored under firmware_mode."""
    coord._apply_firmware_mode_state("Running")
    assert coord._state["firmware_mode"] == "Running"
    assert "firmware_mode_last_updated" in coord._state
    coord.async_set_updated_data.assert_called_once()


def test_apply_firmware_mode_state_dedup_skips_unchanged(
    coord: PhantomChessCoordinator,
) -> None:
    """Same mode value twice is a no-op (no async_set_updated_data call
    the second time)."""
    coord._apply_firmware_mode_state("Running")
    coord.async_set_updated_data.reset_mock()
    coord._apply_firmware_mode_state("Running")
    coord.async_set_updated_data.assert_not_called()


def test_apply_firmware_mode_state_move_event_routed_separately(
    coord: PhantomChessCoordinator,
) -> None:
    """A 'K e1-e2' style move event goes into firmware_last_move, NOT
    firmware_mode."""
    coord._state["firmware_mode"] = "Running"
    coord._apply_firmware_mode_state("K e1-e2")
    # firmware_mode untouched
    assert coord._state["firmware_mode"] == "Running"
    # firmware_last_move updated
    assert coord._state["firmware_last_move"] == "K e1-e2"
    assert "firmware_last_move_updated" in coord._state


def test_apply_firmware_mode_state_move_with_capture_separator(
    coord: PhantomChessCoordinator,
) -> None:
    """The 'x' separator is also valid for capture move events."""
    coord._apply_firmware_mode_state("p d5xe4")
    assert coord._state["firmware_last_move"] == "p d5xe4"


def test_apply_firmware_mode_state_invalid_text_falls_through_to_mode(
    coord: PhantomChessCoordinator,
) -> None:
    """A string that's neither a recognised mode label NOR a move event
    is still stored as firmware_mode — the integration doesn't reject
    unknown labels because new firmware versions may add modes."""
    coord._apply_firmware_mode_state("SomeNewMode")
    assert coord._state["firmware_mode"] == "SomeNewMode"


def test_apply_firmware_mode_state_dedup_skips_same_move(
    coord: PhantomChessCoordinator,
) -> None:
    """Same move event twice is dedup'd."""
    coord._apply_firmware_mode_state("K e1-e2")
    coord.async_set_updated_data.reset_mock()
    coord._apply_firmware_mode_state("K e1-e2")
    coord.async_set_updated_data.assert_not_called()


# ─── _apply_battery_state ───────────────────────────────────────────────


def test_apply_battery_state_writes_fields(
    coord: PhantomChessCoordinator,
) -> None:
    coord._apply_battery_state(percent=75, charging=False)
    assert coord._state["battery_percent"] == 75
    assert coord._state["battery_charging"] is False
    coord.async_set_updated_data.assert_called_once()


def test_apply_battery_state_charging_flag(
    coord: PhantomChessCoordinator,
) -> None:
    coord._apply_battery_state(percent=100, charging=True)
    assert coord._state["battery_charging"] is True


# ─── _apply_matrix_state ────────────────────────────────────────────────


# CLEAN-Match notification payload (firmware 0.3.0). 100-char piece grid
# (10×10 row-major, starting position) + 100-char sensor bitmap (all
# ones where pieces are, zeros elsewhere). Borders + gutter rows are '.'.
_CLEAN_PAYLOAD = {
    "raw": b"CLEAN: Match.,starting,starting",
    "piece_grid": (
        "............rnbqkbnr..pppppppp..........................."  # 60
        "PPPPPPPP..RNBQKBNR..............."                             # +33 = 93
    ).ljust(100, "."),
    "sensor_bitmap": (
        "............11111111..11111111..........................."
        "11111111..11111111..............."
    ).ljust(100, "0"),
    "status": "Clean",
    "status_message": "Match.",
}


def test_apply_matrix_state_clean_payload_populates_state(
    coord: PhantomChessCoordinator,
) -> None:
    """A CLEAN payload updates piece_grid, sensor_bitmap, status,
    live_fen, piece_count, position_consistent."""
    coord._apply_matrix_state(_CLEAN_PAYLOAD)
    assert coord._state["matrix_status"] == "Clean"
    assert coord._state["matrix_status_message"] == "Match."
    assert coord._state["piece_grid"] == _CLEAN_PAYLOAD["piece_grid"]
    assert coord._state["sensor_bitmap"] == _CLEAN_PAYLOAD["sensor_bitmap"]
    # piece_count = number of non-'.' chars in piece_grid
    expected_count = sum(1 for c in _CLEAN_PAYLOAD["piece_grid"] if c != ".")
    assert coord._state["piece_count"] == expected_count
    assert "live_fen" in coord._state
    assert "matrix_last_updated" in coord._state
    coord.async_set_updated_data.assert_called_once()


def test_apply_matrix_state_dedup_skips_identical_payload(
    coord: PhantomChessCoordinator,
) -> None:
    """Same payload arriving twice is dedup'd at the apply layer."""
    coord._apply_matrix_state(_CLEAN_PAYLOAD)
    coord.async_set_updated_data.reset_mock()
    coord._apply_matrix_state(_CLEAN_PAYLOAD)
    coord.async_set_updated_data.assert_not_called()


def test_apply_matrix_state_recalculates_piece_count(
    coord: PhantomChessCoordinator,
) -> None:
    """piece_count is recomputed from the new piece_grid each time."""
    # Empty board: all dots → piece_count 0
    empty_payload = {
        **_CLEAN_PAYLOAD,
        "piece_grid": "." * 100,
        "sensor_bitmap": "0" * 100,
    }
    coord._apply_matrix_state(empty_payload)
    assert coord._state["piece_count"] == 0


# ─── _blank_state ──────────────────────────────────────────────────────


def test_blank_state_returns_dict_with_starting_fen(
    coord: PhantomChessCoordinator,
) -> None:
    """``_blank_state`` returns a dict pre-populated with the starting FEN
    so entities reading ``live_fen`` before the first BLE message don't
    crash."""
    state = coord._blank_state()
    assert isinstance(state, dict)
    # The starting position FEN should be present in some form.
    assert state.get("live_fen") is not None
    # Common state fields the entity platforms read on first paint.
    # Don't pin every key — just sanity-check a few load-bearing ones.
    for key in ("piece_grid", "sensor_bitmap"):
        assert key in state, f"_blank_state should pre-populate {key!r}"


def test_blank_state_returns_independent_dicts(
    coord: PhantomChessCoordinator,
) -> None:
    """Each call returns a fresh dict (so mutations don't leak between
    coordinator resets)."""
    a = coord._blank_state()
    b = coord._blank_state()
    a["custom_key"] = "marker"
    assert "custom_key" not in b

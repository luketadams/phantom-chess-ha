"""Dashboard state-coverage simulation.

The Phantom dashboard is a state machine: which top-level card renders is
decided by the ``conditions`` of a stack of ``type: conditional`` cards. A
missed state → a blank screen; overlapping conditions → two stacked views.
Both are regressions the beta3 state-matrix audit and the C3 (2026-07-06)
picker_available collapse care about.

This test parses the *bundled* ``dashboard_template.yaml`` (raw, with the
YOUR_BOARD_MAC placeholder) and evaluates every top-level conditional card's
conditions against simulated entity states, for the full firmware_mode ×
setup_mode matrix. It asserts:

  * no scenario renders TWO content views at once (no double-render), and
  * every "connected, settled, no active game" scenario renders EXACTLY one
    view (no blank screen).

``binary_sensor.<mac>_picker_available`` and ``binary_sensor.chess_board_idle``
are integration-computed, so the simulation derives them from the same rule
the coordinator/binary_sensor use (``PICKER_FIRMWARE_MODES`` + idle + connected
+ no active game/review) — tying the YAML collapse to the sensor semantics.

Runs in the minimal CI env — pure YAML + Python, no Home Assistant.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from custom_components.phantom_chess.const import PICKER_FIRMWARE_MODES

_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "phantom_chess" / "dashboard_template.yaml"
)

# Raw template entity ids (pre-render placeholders).
_CONNECTED = "binary_sensor.phantom_YOUR_BOARD_MAC_connected"
_PICKER = "binary_sensor.phantom_YOUR_BOARD_MAC_picker_available"
_BOARD_IDLE = "binary_sensor.phantom_chess_board_idle"
_FW_MODE = "sensor.phantom_YOUR_BOARD_MAC_firmware_mode"
_SETUP_MODE = "input_select.phantom_chess_setup_mode"
_LICHESS_ACTIVE = "binary_sensor.phantom_YOUR_BOARD_MAC_lichess_active"
_REVIEW_READY = "binary_sensor.phantom_YOUR_BOARD_MAC_lichess_review_ready"
_LEARNING = "binary_sensor.phantom_YOUR_BOARD_MAC_learning_view_active"
_GAME_ID = "sensor.phantom_YOUR_BOARD_MAC_lichess_game_id"

# Every firmware_mode label the dashboard cares about.
_FIRMWARE_MODES = [
    "HOME", "Waiting Side", "unknown", "unavailable", "BLE Playing",
    "Board Playing", "Snapping Pieces", "Snap to Center", "Calibrating",
    "Setting Up", "Ending Game", "Initializing", "Running",
]
_SETUP_MODES = [
    "Choose a mode", "Play with Lichess", "Play with Stockfish",
    "Sculpture Library", "2-Player Game", "Watch AI vs AI",
]


@pytest.fixture(scope="module")
def top_level_conditionals() -> list[dict[str, Any]]:
    """The mutually-exclusive content views: every ``type: conditional`` in
    the root vertical-stack (the always-visible voice-announcements tile is
    not a conditional and is excluded)."""
    doc = yaml.safe_load(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    root_cards = doc["views"][0]["cards"][0]["cards"]
    return [c for c in root_cards if c.get("type") == "conditional"]


def _derive_state(base: dict[str, str]) -> dict[str, str]:
    """Fill in the integration-computed picker_available from the base facts,
    mirroring PhantomPickerAvailableSensor.is_on exactly."""
    state = dict(base)
    connected = state.get(_CONNECTED) == "on"
    idle = state.get(_BOARD_IDLE) == "on"
    no_game = (
        state.get(_LICHESS_ACTIVE) != "on"
        and state.get(_REVIEW_READY) != "on"
        and state.get(_LEARNING) != "on"  # proxy for local/two-player active
    )
    fw = state.get(_FW_MODE)
    picker = connected and idle and no_game and (fw in PICKER_FIRMWARE_MODES)
    state[_PICKER] = "on" if picker else "off"
    return state


def _cond_matches(cond: dict[str, Any], state: dict[str, str]) -> bool:
    # Shorthand ``{entity: X, state: Y}`` (no explicit condition key).
    if "condition" not in cond:
        return str(state.get(cond["entity"])) == str(cond["state"])
    kind = cond["condition"]
    if kind == "state":
        return str(state.get(cond["entity"])) == str(cond["state"])
    if kind == "or":
        return any(_cond_matches(c, state) for c in cond["conditions"])
    if kind == "and":
        return all(_cond_matches(c, state) for c in cond["conditions"])
    if kind == "not":
        # HA `not`: true iff none of the sub-conditions are true.
        return not any(_cond_matches(c, state) for c in cond["conditions"])
    if kind == "template":
        # Not evaluable here; no top-level view gate uses one.
        raise AssertionError("unexpected top-level template condition")
    raise AssertionError(f"unknown condition kind: {kind!r}")


def _view_renders(conditional: dict[str, Any], state: dict[str, str]) -> bool:
    return all(_cond_matches(c, state) for c in conditional["conditions"])


def _matching(conditionals, state):
    return [c for c in conditionals if _view_renders(c, state)]


def test_placeholder_ids_present_in_template(top_level_conditionals):
    """Guard: if a rename changes the raw entity ids this simulation keys on,
    fail loudly here rather than silently evaluating every view to False."""
    text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    for eid in (_PICKER, _BOARD_IDLE, _FW_MODE, _SETUP_MODE, _CONNECTED):
        assert eid in text, f"template no longer references {eid!r}"
    # picker_available must actually gate views (C3): at least the six
    # picker/setup views reference it.
    picker_gated = sum(
        1
        for c in top_level_conditionals
        if any(
            cc.get("entity") == _PICKER
            for cc in c["conditions"]
            if isinstance(cc, dict)
        )
    )
    assert picker_gated >= 6, f"only {picker_gated} views gate on picker_available"


@pytest.mark.parametrize("fw", _FIRMWARE_MODES)
@pytest.mark.parametrize("setup", _SETUP_MODES)
def test_no_double_render_when_idle_and_no_game(
    top_level_conditionals, fw, setup
):
    """Connected + settled (board_idle on) + no active game/review: at most
    one content view may render for every firmware × setup-mode combo.

    This is the core C3 guarantee — the picker/setup views (picker_available,
    a 6-state standby set) and the stand-by interstitials (the 4 transient
    labels) sit on DISJOINT firmware sets, so they never stack.
    """
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "on",
        _FW_MODE: fw,
        _SETUP_MODE: setup,
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "off",
        _GAME_ID: "none",
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) <= 1, (
        f"double-render for fw={fw!r} setup={setup!r}: "
        f"{len(matches)} views matched"
    )


@pytest.mark.parametrize("fw", _FIRMWARE_MODES)
@pytest.mark.parametrize("setup", _SETUP_MODES)
def test_no_blank_when_idle_and_no_game(top_level_conditionals, fw, setup):
    """Connected + settled + no game: EXACTLY one content view renders — no
    blank screen for any firmware × setup-mode combo (the regression the
    beta3 audit and C3 both guard).

    The one exception is Sculpture-Library while the firmware sits in a
    transient state that the sculpture-playback path owns — but with no game
    active (game_id != sculpture) even that resolves to the interstitial, so
    every combo here is covered.
    """
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "on",
        _FW_MODE: fw,
        _SETUP_MODE: setup,
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "off",
        _GAME_ID: "none",
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, (
        f"expected exactly one view for fw={fw!r} setup={setup!r}, "
        f"got {len(matches)}"
    )


@pytest.mark.parametrize("fw", _FIRMWARE_MODES)
def test_active_learning_game_never_double_renders(top_level_conditionals, fw):
    """While the live learning view is active, no picker/setup/interstitial
    view may also render (picker_available excludes any active game)."""
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "on",  # even if the game stalls 60s
        _FW_MODE: fw,
        _SETUP_MODE: "Play with Stockfish",
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "on",
        _GAME_ID: "none",
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, (
        f"learning view double-renders at fw={fw!r}: {len(matches)} views"
    )


@pytest.mark.parametrize("fw", _FIRMWARE_MODES)
def test_sculpture_playback_never_double_renders(top_level_conditionals, fw):
    """Live bug 2026-07-08: sculpture playback drives the analysis pipeline,
    turning learning_view_active ON — the learning view stacked a second
    board under the dedicated sculpture-playback view. The learning view
    must yield whenever lichess_game_id == 'sculpture'."""
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "off",
        _FW_MODE: fw,
        _SETUP_MODE: "Sculpture Library",
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "on",  # analysis pipeline active during playback
        _GAME_ID: "sculpture",
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, (
        f"sculpture playback double-renders at fw={fw!r}: {len(matches)} views"
    )


def test_disconnected_shows_only_not_connected_overlay(top_level_conditionals):
    """When the board is offline, only the 'not connected' card renders."""
    state = _derive_state({
        _CONNECTED: "off",
        _BOARD_IDLE: "on",
        _FW_MODE: "unavailable",
        _SETUP_MODE: "Choose a mode",
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "off",
        _GAME_ID: "none",
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, f"expected only the offline overlay, got {len(matches)}"

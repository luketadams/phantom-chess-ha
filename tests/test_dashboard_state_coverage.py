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

Study-mode (Luke, 2026-07-08): a global display-density toggle
(``switch.<mac>_study_view``) selects between two presentations of "a game is
running" — the SIMPLE full-width board (study OFF) and the RICH learning
layout (study ON). Every scenario below therefore runs with study ON *and*
OFF, and the sculpture / live-game scenarios assert which of the two variants
renders. These assertions FAIL against the pre-study template (the old
dedicated sculpture view and learning view carry no study gate) — the
falsifiability check the FINDINGS_STABILITY_QUARTET discipline requires.

Runs in the minimal CI env — pure YAML + Python, no Home Assistant.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

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
_STUDY = "switch.phantom_YOUR_BOARD_MAC_study_view"

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
# The active-game setup modes whose live view is study-gated (SIMPLE vs RICH).
_LIVE_GAME_SETUP_MODES = [
    "Play with Lichess", "Play with Stockfish", "2-Player Game",
    "Watch AI vs AI",
]
_STUDY_STATES = ["off", "on"]


@pytest.fixture(scope="module")
def top_level_conditionals() -> list[dict[str, Any]]:
    """The mutually-exclusive content views: every ``type: conditional`` in
    the root vertical-stack (the always-visible voice-announcements tile is
    not a conditional and is excluded)."""
    doc = yaml.safe_load(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    root_cards = doc["views"][0]["cards"][0]["cards"]
    return [c for c in root_cards if c.get("type") == "conditional"]


def _walk_cards(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict with a ``type`` key, depth-first."""
    if isinstance(node, dict):
        if "type" in node:
            yield node
        for v in node.values():
            yield from _walk_cards(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_cards(item)


def _study_gate(conditional: dict[str, Any]) -> str | None:
    """The study_view state this conditional requires ('on'/'off'), or None.

    SIMPLE game view gates study OFF; RICH learning view gates study ON. A
    conditional with no study gate (every pre-study view, and all the
    picker/setup/interstitial views) returns None."""
    for c in conditional["conditions"]:
        if isinstance(c, dict) and c.get("entity") == _STUDY:
            return c.get("state")
    return None


def _is_rich(conditional: dict[str, Any]) -> bool:
    """The RICH learning view is the only content view built on a
    ``custom:layout-card`` (eval-bar | board | moves-table grid). The SIMPLE
    view is a plain vertical-stack with a full-width ``picture-entity`` and no
    layout-card — so absence of a layout-card discriminates SIMPLE from RICH."""
    return any(
        c.get("type") == "custom:layout-card"
        for c in _walk_cards(conditional.get("card", {}))
    )


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
    for eid in (_PICKER, _BOARD_IDLE, _FW_MODE, _SETUP_MODE, _CONNECTED, _STUDY):
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
    # study_view must gate exactly the two live-game presentation variants
    # (SIMPLE off + RICH on).
    study_gated = [c for c in top_level_conditionals if _study_gate(c) is not None]
    assert len(study_gated) == 2, (
        f"expected exactly 2 study-gated views (SIMPLE + RICH), got "
        f"{len(study_gated)}"
    )
    assert sorted(_study_gate(c) for c in study_gated) == ["off", "on"]


@pytest.mark.parametrize("study", _STUDY_STATES)
@pytest.mark.parametrize("fw", _FIRMWARE_MODES)
@pytest.mark.parametrize("setup", _SETUP_MODES)
def test_no_double_render_when_idle_and_no_game(
    top_level_conditionals, fw, setup, study
):
    """Connected + settled (board_idle on) + no active game/review: at most
    one content view may render for every firmware × setup-mode × study combo.

    This is the core C3 guarantee — the picker/setup views (picker_available,
    a 6-state standby set) and the stand-by interstitials (the 4 transient
    labels) sit on DISJOINT firmware sets, so they never stack. Study mode is
    a no-op here (no game live), so it must not conjure a SIMPLE/RICH view.
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
        _STUDY: study,
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) <= 1, (
        f"double-render for fw={fw!r} setup={setup!r} study={study!r}: "
        f"{len(matches)} views matched"
    )


@pytest.mark.parametrize("study", _STUDY_STATES)
@pytest.mark.parametrize("fw", _FIRMWARE_MODES)
@pytest.mark.parametrize("setup", _SETUP_MODES)
def test_no_blank_when_idle_and_no_game(top_level_conditionals, fw, setup, study):
    """Connected + settled + no game: EXACTLY one content view renders — no
    blank screen for any firmware × setup-mode × study combo (the regression
    the beta3 audit and C3 both guard).

    The one exception is Sculpture-Library while the firmware sits in a
    transient state that the sculpture-playback path owns — but with no game
    active (game_id != sculpture) even that resolves to the interstitial, so
    every combo here is covered. Study mode adds no view when no game is live.
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
        _STUDY: study,
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, (
        f"expected exactly one view for fw={fw!r} setup={setup!r} "
        f"study={study!r}, got {len(matches)}"
    )


@pytest.mark.parametrize("study", _STUDY_STATES)
@pytest.mark.parametrize("fw", _FIRMWARE_MODES)
def test_active_learning_game_never_double_renders(
    top_level_conditionals, fw, study
):
    """While the live learning view is active, no picker/setup/interstitial
    view may also render (picker_available excludes any active game) — and the
    two study variants are mutually exclusive, so exactly one renders for
    either study state."""
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "on",  # even if the game stalls 60s
        _FW_MODE: fw,
        _SETUP_MODE: "Play with Stockfish",
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "on",
        _GAME_ID: "none",
        _STUDY: study,
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, (
        f"learning view double-renders at fw={fw!r} study={study!r}: "
        f"{len(matches)} views"
    )


@pytest.mark.parametrize("study", _STUDY_STATES)
@pytest.mark.parametrize("fw", _FIRMWARE_MODES)
def test_sculpture_playback_never_double_renders(
    top_level_conditionals, fw, study
):
    """Live bug 2026-07-08: sculpture playback drives the analysis pipeline,
    turning learning_view_active ON — the learning view stacked a second
    board under the dedicated sculpture-playback view. Exactly one view must
    render during playback for either study state (SIMPLE off / RICH on)."""
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "off",
        _FW_MODE: fw,
        _SETUP_MODE: "Sculpture Library",
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "on",  # analysis pipeline active during playback
        _GAME_ID: "sculpture",
        _STUDY: study,
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, (
        f"sculpture playback double-renders at fw={fw!r} study={study!r}: "
        f"{len(matches)} views"
    )


# ─── Study-mode: SIMPLE (off) vs RICH (on) selection ────────────────────
# These assert not just "one view" but *which* view. They fail against the
# pre-study template, where the old dedicated sculpture view and learning
# view carry no study gate (so _study_gate returns None on the sole match).


@pytest.mark.parametrize("fw", _FIRMWARE_MODES)
def test_sculpture_playback_study_off_renders_simple_view(
    top_level_conditionals, fw
):
    """Sculpture playback + Study mode OFF → exactly the SIMPLE full-width
    board view (study gate 'off', no layout-card)."""
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "off",
        _FW_MODE: fw,
        _SETUP_MODE: "Sculpture Library",
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "on",
        _GAME_ID: "sculpture",
        _STUDY: "off",
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, f"expected 1 view at fw={fw!r}, got {len(matches)}"
    view = matches[0]
    assert _study_gate(view) == "off", "matched view is not the study-OFF SIMPLE view"
    assert not _is_rich(view), "SIMPLE view must not use a layout-card"


@pytest.mark.parametrize("fw", _FIRMWARE_MODES)
def test_sculpture_playback_study_on_renders_rich_view(
    top_level_conditionals, fw
):
    """Sculpture playback + Study mode ON → exactly the RICH learning view
    (study gate 'on', layout-card present). This is the whole point of the
    toggle: in study mode, sculpture playback shows the rich layout."""
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "off",
        _FW_MODE: fw,
        _SETUP_MODE: "Sculpture Library",
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "on",
        _GAME_ID: "sculpture",
        _STUDY: "on",
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, f"expected 1 view at fw={fw!r}, got {len(matches)}"
    view = matches[0]
    assert _study_gate(view) == "on", "matched view is not the study-ON RICH view"
    assert _is_rich(view), "RICH view must use the layout-card"


@pytest.mark.parametrize("setup", _LIVE_GAME_SETUP_MODES)
def test_live_game_study_off_renders_simple_view(top_level_conditionals, setup):
    """A live Lichess / Stockfish / two-player / AI-vs-AI game (learning on,
    not sculpture) + Study mode OFF → exactly the SIMPLE view."""
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "off",
        _FW_MODE: "Board Playing",
        _SETUP_MODE: setup,
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "on",
        _GAME_ID: "none",
        _STUDY: "off",
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, f"expected 1 view for setup={setup!r}, got {len(matches)}"
    view = matches[0]
    assert _study_gate(view) == "off", "matched view is not the study-OFF SIMPLE view"
    assert not _is_rich(view), "SIMPLE view must not use a layout-card"


@pytest.mark.parametrize("setup", _LIVE_GAME_SETUP_MODES)
def test_live_game_study_on_renders_rich_view(top_level_conditionals, setup):
    """A live Lichess / Stockfish / two-player / AI-vs-AI game (learning on,
    not sculpture) + Study mode ON → exactly the RICH learning view."""
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "off",
        _FW_MODE: "Board Playing",
        _SETUP_MODE: setup,
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "on",
        _GAME_ID: "none",
        _STUDY: "on",
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, f"expected 1 view for setup={setup!r}, got {len(matches)}"
    view = matches[0]
    assert _study_gate(view) == "on", "matched view is not the study-ON RICH view"
    assert _is_rich(view), "RICH view must use the layout-card"


@pytest.mark.parametrize("study", _STUDY_STATES)
def test_sculpture_review_yields_to_review_view(top_level_conditionals, study):
    """When the post-game review is ready (review_ready on) the SIMPLE and
    RICH game views must both yield — game_id stays 'sculpture' into review,
    so without a review_ready gate the RICH view would double-render with the
    post-game review card for either study state."""
    state = _derive_state({
        _CONNECTED: "on",
        _BOARD_IDLE: "off",
        _FW_MODE: "Ending Game",
        _SETUP_MODE: "Sculpture Library",
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "on",
        _LEARNING: "off",
        _GAME_ID: "sculpture",
        _STUDY: study,
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, (
        f"expected only the review view for study={study!r}, got {len(matches)}"
    )
    assert _study_gate(matches[0]) is None, "review view must not be study-gated"


@pytest.mark.parametrize("study", _STUDY_STATES)
def test_disconnected_shows_only_not_connected_overlay(
    top_level_conditionals, study
):
    """When the board is offline, only the 'not connected' card renders —
    regardless of study mode (SIMPLE/RICH both require connected on)."""
    state = _derive_state({
        _CONNECTED: "off",
        _BOARD_IDLE: "on",
        _FW_MODE: "unavailable",
        _SETUP_MODE: "Choose a mode",
        _LICHESS_ACTIVE: "off",
        _REVIEW_READY: "off",
        _LEARNING: "off",
        _GAME_ID: "none",
        _STUDY: study,
    })
    matches = _matching(top_level_conditionals, state)
    assert len(matches) == 1, f"expected only the offline overlay, got {len(matches)}"

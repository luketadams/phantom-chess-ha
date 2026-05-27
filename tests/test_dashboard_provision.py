"""Tests for the dashboard auto-provision renderer.

The renderer is a pure-function pipeline that takes the bundled
``dashboard_template.yaml``, applies text substitutions (helper-id and
service-domain rewrites + entity-id resolution), parses to dict, and
runs ``_convert_action_tiles_to_buttons`` to swap action-shaped
``type: tile`` cards into ``type: button`` cards.

These tests run in the minimal CI environment (no HA installed) via
the conftest.py stub that pre-stages dashboard_provision with stubbed
HA imports. They cover the alpha8 conversion + earlier text passes
without touching the storage/panel side effects.

Run:
    pytest tests/test_dashboard_provision.py
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from custom_components.phantom_chess.dashboard_provision import (
    _HELPER_TO_NATIVE,
    _SCRIPT_TO_SERVICE,
    _TEMPLATE_TO_UNIQUE_SUFFIX_ALIASES,
    _convert_action_tiles_to_buttons,
    _is_action_service_call,
    _mac_to_slug,
    _render_template,
    _resolve_or_fallback,
    _rewrite_script_turn_on_in_text,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_PATH = (
    _REPO_ROOT / "custom_components" / "phantom_chess" / "dashboard_template.yaml"
)
_TEST_MAC = "C8:C9:A3:F2:7C:0A"


# ─── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def rendered_config() -> dict[str, Any]:
    """Full renderer pipeline output for a representative MAC.

    Empty entity_map forces every entity reference to fall back to the
    MAC-slug guess path, which is sufficient for shape assertions.
    """
    yaml_text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered_text = _render_template(yaml_text, _TEST_MAC, entity_map={})
    rendered_text = _rewrite_script_turn_on_in_text(rendered_text)
    config = yaml.safe_load(rendered_text)
    _convert_action_tiles_to_buttons(config)
    return config


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


def _find_strings(node: Any, needle: str) -> list[str]:
    """Return every string anywhere in ``node`` that contains ``needle``."""
    found: list[str] = []
    if isinstance(node, str):
        if needle in node:
            found.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            found.extend(_find_strings(v, needle))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_strings(item, needle))
    return found


# ─── card-count assertions ──────────────────────────────────────────────


def test_total_tile_to_button_split(rendered_config: dict[str, Any]) -> None:
    """alpha8 conversion produces 21 buttons + 17 surviving tiles.

    These exact counts are the alpha8 ground-truth. A regression here
    means either the template changed (re-baseline) or the detection
    logic in ``_convert_action_tiles_to_buttons`` slipped (investigate).
    """
    cards = list(_walk_cards(rendered_config))
    tiles = [c for c in cards if c.get("type") == "tile"]
    buttons = [c for c in cards if c.get("type") == "button"]
    assert len(tiles) == 17, f"expected 17 surviving tiles, got {len(tiles)}"
    assert len(buttons) == 21, f"expected 21 buttons, got {len(buttons)}"


# ─── "what should be gone" assertions ───────────────────────────────────


def test_no_residual_script_phantom_references(
    rendered_config: dict[str, Any],
) -> None:
    """v0.3 setup-pack scripts are fully retired in v0.4 alpha7+."""
    refs = _find_strings(rendered_config, "script.phantom_")
    assert refs == [], f"residual script.phantom_* references: {refs[:5]}"


def test_no_residual_icon_tap_action(rendered_config: dict[str, Any]) -> None:
    """alpha8 dropped the icon_tap_action mirror — type: button doesn't have one."""
    leftovers = [
        c.get("name") for c in _walk_cards(rendered_config) if "icon_tap_action" in c
    ]
    assert leftovers == [], f"residual icon_tap_action on cards: {leftovers}"


def test_no_residual_script_turn_on(rendered_config: dict[str, Any]) -> None:
    """script.turn_on tap_actions are rewritten to phantom_chess.X by text pass."""
    refs = _find_strings(rendered_config, "script.turn_on")
    assert refs == [], f"residual script.turn_on references: {refs[:5]}"


def test_no_residual_input_helpers(rendered_config: dict[str, Any]) -> None:
    """v0.3 input_select / input_number / input_boolean helpers are gone."""
    leftovers: list[str] = []
    for prefix in ("input_select.phantom_chess", "input_number.phantom_chess",
                   "input_boolean.phantom_chess"):
        leftovers.extend(_find_strings(rendered_config, prefix))
    assert leftovers == [], f"residual input_* helper references: {leftovers[:5]}"


def test_no_residual_your_board_mac(rendered_config: dict[str, Any]) -> None:
    """Every YOUR_BOARD_MAC placeholder is substituted at render time."""
    refs = _find_strings(rendered_config, "YOUR_BOARD_MAC")
    assert refs == [], f"residual YOUR_BOARD_MAC placeholders: {refs[:5]}"


# ─── button-shape assertions ────────────────────────────────────────────


def test_buttons_have_tap_action(rendered_config: dict[str, Any]) -> None:
    """Every converted button has a tap_action — silent no-op buttons would
    be a regression from the alpha7→alpha8 conversion's injection logic.
    """
    buttons = [c for c in _walk_cards(rendered_config) if c.get("type") == "button"]
    missing = [b.get("name") for b in buttons if "tap_action" not in b]
    assert missing == [], f"buttons missing tap_action: {missing}"


def test_buttons_drop_tile_only_fields(rendered_config: dict[str, Any]) -> None:
    """type: button cards must not carry tile-only fields after conversion."""
    buttons = [c for c in _walk_cards(rendered_config) if c.get("type") == "button"]
    tile_only = ("entity", "hide_state", "vertical", "icon_tap_action")
    leftovers: list[tuple[str, str]] = []
    for b in buttons:
        for field in tile_only:
            if field in b:
                leftovers.append((b.get("name", "?"), field))
    assert leftovers == [], f"buttons retain tile-only fields: {leftovers}"


def test_buttons_have_explicit_show_state_false(
    rendered_config: dict[str, Any],
) -> None:
    """alpha8 sets show_state: false explicitly on every converted button so
    the rendered config self-documents intent.
    """
    buttons = [c for c in _walk_cards(rendered_config) if c.get("type") == "button"]
    missing = [b.get("name") for b in buttons if b.get("show_state") is not False]
    assert missing == [], f"buttons missing show_state: false: {missing}"


# ─── surviving-tile assertions ──────────────────────────────────────────


def test_surviving_tiles_are_info_features_or_toggle(
    rendered_config: dict[str, Any],
) -> None:
    """Every tile left as type: tile must be info, features-enabled, or toggle.

    A regression here would mean an action-shaped tile slipped past the
    detection logic and stayed as a tile (re-exposing the icon-popup
    bug).
    """
    suspect: list[str] = []
    for c in _walk_cards(rendered_config):
        if c.get("type") != "tile":
            continue
        has_features = "features" in c
        tap = c.get("tap_action")
        is_toggle = isinstance(tap, dict) and tap.get("action") == "toggle"
        is_info = tap is None
        if not (has_features or is_toggle or is_info):
            suspect.append(str(c.get("name", "?")))
    assert suspect == [], f"action-shaped tiles still as type:tile: {suspect}"


# ─── content assertions: known buttons exist ────────────────────────────


def _button_named(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    for c in _walk_cards(config):
        if c.get("type") == "button" and c.get("name") == name:
            return c
    return None


@pytest.mark.parametrize(
    "name,service",
    [
        ("Back to modes", "phantom_chess.back_to_modes"),
        ("Start Lichess game", "phantom_chess.start_lichess_configured"),
        ("Start game vs Stockfish", "phantom_chess.start_local_game"),
        ("Play selected sculpture", "phantom_chess.play_selected_sculpture"),
        ("Takeback", "phantom_chess.takeback"),
        ("Resign", "phantom_chess.resign"),
        ("Hint", "phantom_chess.request_hint"),
        ("New game", "select.select_option"),
    ],
)
def test_known_buttons_invoke_expected_service(
    rendered_config: dict[str, Any], name: str, service: str
) -> None:
    """Smoke-check: each named button maps to its expected phantom_chess service.

    Several buttons appear multiple times in the template (e.g. multiple
    Back-to-modes for different submode contexts). This test finds the
    first one and verifies the tap_action service. Any rename of a
    service in ``_SCRIPT_TO_SERVICE`` without updating the template would
    fail one of these assertions.
    """
    button = _button_named(rendered_config, name)
    assert button is not None, f"button named {name!r} not found in rendered config"
    tap = button["tap_action"]
    assert isinstance(tap, dict)
    invoked = tap.get("perform_action") or tap.get("service")
    assert invoked == service, f"{name!r} invokes {invoked!r}, expected {service!r}"


def test_resign_button_preserves_confirmation(
    rendered_config: dict[str, Any],
) -> None:
    """The Resign button's confirmation prompt must survive conversion."""
    buttons = [
        c for c in _walk_cards(rendered_config)
        if c.get("type") == "button" and c.get("name") == "Resign"
    ]
    assert buttons, "no Resign button found"
    for resign in buttons:
        tap = resign["tap_action"]
        assert "confirmation" in tap, "Resign button lost confirmation block"
        assert "Resign" in tap["confirmation"].get("text", ""), (
            f"Resign confirmation text changed: {tap['confirmation']}"
        )


def test_mode_picker_buttons_target_setup_mode_select(
    rendered_config: dict[str, Any],
) -> None:
    """The four mode-picker buttons should all call select.select_option on
    the setup_mode select with their respective option.
    """
    expected = {
        "Play with Lichess": "Play with Lichess",
        "Play with Stockfish": "Play with Stockfish",
        "Sculpture Library": "Sculpture Library",
        "2-Player Game": "2-Player Game",
    }
    for name, option in expected.items():
        button = _button_named(rendered_config, name)
        assert button is not None, f"mode-picker button {name!r} not found"
        tap = button["tap_action"]
        assert tap.get("perform_action") == "select.select_option", (
            f"{name!r} doesn't call select.select_option"
        )
        data = tap.get("data", {})
        assert data.get("option") == option, (
            f"{name!r} sets option={data.get('option')!r}, expected {option!r}"
        )
        # entity_id should point at the resolved setup_mode select
        assert "setup_mode" in data.get("entity_id", ""), (
            f"{name!r} entity_id {data.get('entity_id')!r} doesn't reference setup_mode"
        )


# ─── helper-rewrite assertions ──────────────────────────────────────────


def test_helper_service_domains_rewritten(rendered_config: dict[str, Any]) -> None:
    """v0.3 input_select.select_option / input_number.set_value / etc. all
    become their native-domain counterparts.
    """
    leftovers: list[str] = []
    for old in (
        "input_select.select_option",
        "input_number.set_value",
        "input_boolean.toggle",
    ):
        leftovers.extend(_find_strings(rendered_config, old))
    assert leftovers == [], f"helper service domains not rewritten: {leftovers[:5]}"


# ─── _is_action_service_call unit tests ─────────────────────────────────


@pytest.mark.parametrize(
    "tap_action,expected",
    [
        # phantom_chess.* (modern perform_action shape)
        ({"action": "perform-action", "perform_action": "phantom_chess.takeback"}, True),
        # phantom_chess.* (legacy call-service shape)
        ({"action": "call-service", "service": "phantom_chess.resign"}, True),
        # select.select_option for mode-picker buttons
        ({"action": "perform-action", "perform_action": "select.select_option"}, True),
        # Toggle action — not a service call
        ({"action": "toggle"}, False),
        # Navigate — not a service call
        ({"action": "navigate", "navigation_path": "/foo"}, False),
        # Some other service — not in the allow-list
        ({"action": "call-service", "service": "light.turn_on"}, False),
        # Non-dict input
        (None, False),
        ("not a dict", False),
    ],
)
def test_is_action_service_call(tap_action: Any, expected: bool) -> None:
    assert _is_action_service_call(tap_action) is expected


# ─── _mac_to_slug ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ble_address,expected_slug",
    [
        ("AA:BB:CC:DD:EE:FF", "aa_bb_cc_dd_ee_ff"),
        ("aa:bb:cc:dd:ee:ff", "aa_bb_cc_dd_ee_ff"),
        ("C8:C9:A3:F2:7C:0A", "c8_c9_a3_f2_7c_0a"),  # Luke's actual board
        # Mixed case is canonicalised to lowercase
        ("c8:C9:a3:F2:7C:0a", "c8_c9_a3_f2_7c_0a"),
    ],
)
def test_mac_to_slug_canonicalises(ble_address: str, expected_slug: str) -> None:
    assert _mac_to_slug(ble_address) == expected_slug


# ─── _resolve_or_fallback ────────────────────────────────────────────────


def test_resolve_or_fallback_uses_registry_when_present() -> None:
    """When the registry has an entity_id under (domain, suffix), use it
    verbatim — regardless of what the MAC-slug guess would produce.
    """
    mac_slug = "c8_c9_a3_f2_7c_0a"
    # Simulate v0.3 entities registered with MAC-slug + v0.4-alpha entities
    # registered with device-name slug (Phantom 6552 → phantom_6552_*),
    # which is the exact divergence Luke's board has.
    entity_map = {
        ("binary_sensor", "connected"): "binary_sensor.phantom_c8_c9_a3_f2_7c_0a_connected",
        ("select", "setup_mode"): "select.phantom_6552_setup_mode",
    }
    # v0.3 entity — registry returns MAC-slug form, matches the guess
    assert _resolve_or_fallback("binary_sensor", "connected", entity_map, mac_slug) == (
        "binary_sensor.phantom_c8_c9_a3_f2_7c_0a_connected"
    )
    # v0.4 alpha entity — registry returns device-name slug, NOT the
    # MAC-slug guess. This is the load-bearing case for the alpha6+
    # fix (see ha-entity-id-slug-divergence memory).
    assert _resolve_or_fallback("select", "setup_mode", entity_map, mac_slug) == (
        "select.phantom_6552_setup_mode"
    )


def test_resolve_or_fallback_uses_mac_slug_when_registry_misses() -> None:
    """When the entity hasn't been registered yet (first setup_entry,
    platform forward not complete), fall back to the MAC-slug guess.
    """
    mac_slug = "c8_c9_a3_f2_7c_0a"
    entity_map: dict[tuple[str, str], str] = {}  # registry empty
    assert _resolve_or_fallback("sensor", "battery", entity_map, mac_slug) == (
        "sensor.phantom_c8_c9_a3_f2_7c_0a_battery"
    )


def test_resolve_or_fallback_applies_image_alias() -> None:
    """The image platform's unique_id suffix differs from the entity_id
    suffix (`_board_image` in registry, `_board` in template). The
    resolver bridges this via ``_TEMPLATE_TO_UNIQUE_SUFFIX_ALIASES``.
    """
    mac_slug = "c8_c9_a3_f2_7c_0a"
    entity_map = {
        # The registry has it under "board_image"
        ("image", "board_image"): "image.phantom_c8_c9_a3_f2_7c_0a_board",
    }
    # Template uses "board", which is aliased to "board_image" before lookup.
    assert _resolve_or_fallback("image", "board", entity_map, mac_slug) == (
        "image.phantom_c8_c9_a3_f2_7c_0a_board"
    )
    # Sanity check: the alias map declares this
    assert _TEMPLATE_TO_UNIQUE_SUFFIX_ALIASES.get("board") == "board_image"


# ─── _SCRIPT_TO_SERVICE / _HELPER_TO_NATIVE map consistency ─────────────


def test_script_to_service_covers_every_template_script_entity() -> None:
    """Every `script.phantom_X` referenced by the bundled template must
    have an entry in ``_SCRIPT_TO_SERVICE`` — otherwise the renderer
    leaves the script reference unchanged and the button is a no-op.
    """
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    import re

    referenced = set(re.findall(r"script\.(phantom_[a-z_]+)", template))
    missing_from_map = referenced - set(_SCRIPT_TO_SERVICE.keys())
    assert missing_from_map == set(), (
        f"template references scripts with no mapping: {missing_from_map}"
    )


def test_helper_to_native_covers_every_template_input_helper() -> None:
    """Every `input_select.phantom_chess_X` / `input_number.phantom_chess_X` /
    `input_boolean.phantom_chess_X` reference in the template must have a
    mapping to its v0.4 native replacement.
    """
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    import re

    referenced = set(
        re.findall(r"(input_(?:select|number|boolean)\.phantom_chess_[a-z_]+)", template)
    )
    # Also pick up the v0.3 template-helper binary_sensor that alpha5
    # rewrote to a native binary_sensor.
    referenced |= set(re.findall(r"(binary_sensor\.phantom_chess_[a-z_]+)", template))
    missing_from_map = referenced - set(_HELPER_TO_NATIVE.keys())
    assert missing_from_map == set(), (
        f"template references helpers with no mapping: {missing_from_map}"
    )

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
    FRONTEND_DEPS,
    _HELPER_TO_NATIVE,
    _SCRIPT_TO_SERVICE,
    _TEMPLATE_TO_UNIQUE_SUFFIX_ALIASES,
    _convert_action_tiles_to_buttons,
    _is_action_service_call,
    _mac_to_slug,
    _missing_frontend_deps,
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


def test_tile_and_action_control_split(rendered_config: dict[str, Any]) -> None:
    """Structural ground-truth for the C7-dash action-button redesign.

    C7-dash replaced the oversized full-width vertical action tiles and
    chunky 2-col tile grids with a compact hierarchy built entirely from
    Mushroom cards:
      - PRIMARY / SECONDARY actions → ``custom:mushroom-template-card``
        (icon + short label, uniform ~56px).
      - TERTIARY nav (Back to modes / status strip) →
        ``custom:mushroom-chips-card``.
    Consequently NO action-shaped ``type: tile`` remains, so the
    ``_convert_action_tiles_to_buttons`` pass produces ZERO ``type: button``
    cards. The mode-picker mascot picture buttons (``type: picture``) are
    untouched and asserted separately.

    A regression here means either the template's control structure changed
    (re-baseline) or an action tile slipped back in (investigate — it would
    re-expose the icon-popup bug the mushroom cards avoid).
    """
    cards = list(_walk_cards(rendered_config))
    tiles = [c for c in cards if c.get("type") == "tile"]
    buttons = [c for c in cards if c.get("type") == "button"]
    mushroom = [c for c in cards if c.get("type") == "custom:mushroom-template-card"]
    chips = [c for c in cards if c.get("type") == "custom:mushroom-chips-card"]

    # No tile converts to a button any more — all actions are mushroom cards.
    assert len(buttons) == 0, f"expected 0 converted buttons, got {len(buttons)}"
    # Surviving tiles are all info / features (select-options) / toggle:
    #   1 Voice-announcements toggle, 1 Bluetooth-connection info,
    #   4 select-options config tiles (2× My color, 2× AI level),
    #   1 Choose-a-game select tile, 7 Firmware-state info tiles
    #   (Snapping/Snap/Calibrating/Setting Up/Ending/Initializing/Running),
    #   2 bare-board info tiles (Last move, Pieces) = 16.
    assert len(tiles) == 16, f"expected 16 surviving tiles, got {len(tiles)}"
    # Every action button + interstitial banner is a mushroom-template-card;
    # every tertiary nav / status strip is a mushroom-chips-card.
    assert len(mushroom) == 24, f"expected 24 mushroom-template-cards, got {len(mushroom)}"
    assert len(chips) == 13, f"expected 13 mushroom-chips-cards, got {len(chips)}"


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


def _action_controls_labeled(config: dict[str, Any], label: str) -> list[dict[str, Any]]:
    """Return every C7-dash action control whose visible label is ``label``.

    An action control is a ``custom:mushroom-template-card`` (label in
    ``primary``) or a ``custom:mushroom-chips-card`` template chip (label in
    ``content``). Several labels recur across submode contexts (e.g. multiple
    "Back to modes" / "Resign"), so this returns all matches.
    """
    matches: list[dict[str, Any]] = []
    for c in _walk_cards(config):
        if c.get("type") == "custom:mushroom-template-card" and c.get("primary") == label:
            matches.append(c)
        elif c.get("type") == "custom:mushroom-chips-card":
            for chip in c.get("chips", []):
                if chip.get("content") == label:
                    matches.append(chip)
    return matches


@pytest.mark.parametrize(
    "label,service",
    [
        ("Back to modes", "phantom_chess.back_to_modes"),          # chip
        ("Re-sync detection", "phantom_chess.resync_detection"),   # chip
        ("Start Lichess game", "phantom_chess.start_lichess_configured"),
        ("Start game vs Stockfish", "phantom_chess.start_local_game"),
        ("Play selected game", "phantom_chess.play_selected_sculpture"),
        ("Start recording", "phantom_chess.start_two_player_game"),
        ("Start AI vs AI", "phantom_chess.start_ai_vs_ai_game"),
        ("Resync board", "phantom_chess.resync_two_player"),
        ("Takeback", "phantom_chess.takeback"),
        ("Resign", "phantom_chess.resign"),
        ("Hint", "phantom_chess.request_hint"),
        ("Reset board", "phantom_chess.reset_position"),
        ("New game", "select.select_option"),
    ],
)
def test_known_action_controls_invoke_expected_service(
    rendered_config: dict[str, Any], label: str, service: str
) -> None:
    """Smoke-check: each C7-dash action control maps to its expected service.

    Any rename of a phantom_chess service, or a helper-rewrite regression,
    without updating the template would fail one of these assertions.
    """
    controls = _action_controls_labeled(rendered_config, label)
    assert controls, f"action control labeled {label!r} not found in rendered config"
    for ctrl in controls:
        tap = ctrl["tap_action"]
        assert isinstance(tap, dict)
        invoked = tap.get("perform_action") or tap.get("service")
        assert invoked == service, f"{label!r} invokes {invoked!r}, expected {service!r}"


def test_resign_control_preserves_confirmation(
    rendered_config: dict[str, Any],
) -> None:
    """The Resign action must keep its confirmation prompt (C7-dash requires
    every existing tap_action/confirmation preserved verbatim)."""
    resigns = _action_controls_labeled(rendered_config, "Resign")
    assert resigns, "no Resign action control found"
    for resign in resigns:
        tap = resign["tap_action"]
        assert "confirmation" in tap, "Resign action lost confirmation block"
        assert "Resign" in tap["confirmation"].get("text", ""), (
            f"Resign confirmation text changed: {tap['confirmation']}"
        )


def test_mode_picker_buttons_target_setup_mode_select(
    rendered_config: dict[str, Any],
) -> None:
    """beta1: the mode picker is now custom ghost-mascot picture buttons.
    Each picture card's tap_action must call select.select_option on the
    setup_mode select with its respective option (the AI-vs-AI button was
    added to the picker in beta1).
    """
    expected = {
        "lichess.png": "Play with Lichess",
        "stockfish.png": "Play with Stockfish",
        "historic.png": "Sculpture Library",
        "two_player.png": "2-Player Game",
        "ai_vs_ai.png": "Watch AI vs AI",
    }
    pics = {
        c["image"].rsplit("/", 1)[-1].split("?")[0]: c
        for c in _walk_cards(rendered_config)
        if c.get("type") == "picture"
        and isinstance(c.get("image"), str)
        and "/phantom_chess/buttons/" in c["image"]
    }
    for img, option in expected.items():
        card = pics.get(img)
        assert card is not None, f"mode-picker picture button {img!r} not found"
        tap = card["tap_action"]
        assert tap.get("perform_action") == "select.select_option", (
            f"{img!r} doesn't call select.select_option"
        )
        data = tap.get("data", {})
        assert data.get("option") == option, (
            f"{img!r} sets option={data.get('option')!r}, expected {option!r}"
        )
        # entity_id should point at the resolved setup_mode select
        assert "setup_mode" in data.get("entity_id", ""), (
            f"{img!r} entity_id {data.get('entity_id')!r} doesn't reference setup_mode"
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


# ─── _missing_frontend_deps ─────────────────────────────────────────────


def _hass_with_resources(urls: list[str]):
    """Build a hass-like object whose Lovelace resources async_items()
    returns a list of {url: ..., type: ..., id: ...} dicts.
    """
    from unittest.mock import MagicMock
    from custom_components.phantom_chess.dashboard_provision import LOVELACE_DATA

    lovelace = MagicMock()
    lovelace.resources.async_items.return_value = [
        {"url": u, "type": "module", "id": f"r{i}"} for i, u in enumerate(urls)
    ]
    hass = MagicMock()
    hass.data = {LOVELACE_DATA: lovelace}
    return hass


def test_missing_frontend_deps_all_present() -> None:
    """When every dep is registered, the missing-list is empty."""
    hass = _hass_with_resources([
        "/hacsfiles/mushroom/mushroom.js",
        "/hacsfiles/layout-card/layout-card.js",
        "/hacsfiles/lovelace-card-mod/card-mod.js",
    ])
    assert _missing_frontend_deps(hass) == []


def test_missing_frontend_deps_all_missing() -> None:
    """When the resources list is empty, every dep is reported missing."""
    hass = _hass_with_resources([])
    missing = _missing_frontend_deps(hass)
    # Every entry in FRONTEND_DEPS shows up by display name
    expected = {display for _suffix, display, _patterns in FRONTEND_DEPS}
    assert set(missing) == expected


def test_missing_frontend_deps_partial() -> None:
    """One present, two missing — only the missing ones are reported."""
    hass = _hass_with_resources([
        "/hacsfiles/mushroom/mushroom.js",
    ])
    missing = _missing_frontend_deps(hass)
    # Mushroom is present; layout-card and card-mod should be flagged.
    assert "Mushroom" not in missing
    assert "layout-card" in missing
    assert "card-mod" in missing


def test_missing_frontend_deps_underscore_variant_accepted() -> None:
    """Older HACS versions sometimes used underscores in URLs; we accept
    both 'card-mod' and 'card_mod' as canonical."""
    hass = _hass_with_resources([
        "/hacsfiles/mushroom/mushroom.js",
        "/hacsfiles/layout_card/layout_card.js",
        "/hacsfiles/card_mod/card_mod.js",
    ])
    assert _missing_frontend_deps(hass) == []


def test_missing_frontend_deps_no_lovelace_data_returns_empty() -> None:
    """When LOVELACE_DATA isn't in hass.data (e.g. early boot), we
    conservatively return empty so we don't surface a false-positive
    issue. The user can't have a broken dashboard yet either way."""
    from unittest.mock import MagicMock
    hass = MagicMock()
    hass.data = {}
    assert _missing_frontend_deps(hass) == []


def test_missing_frontend_deps_resources_iteration_error_returns_empty() -> None:
    """If the resources collection isn't iterable yet (rare boot race),
    fall through to empty rather than crash."""
    from unittest.mock import MagicMock
    from custom_components.phantom_chess.dashboard_provision import LOVELACE_DATA

    lovelace = MagicMock()
    lovelace.resources.async_items.side_effect = AttributeError("not ready")
    hass = MagicMock()
    hass.data = {LOVELACE_DATA: lovelace}
    assert _missing_frontend_deps(hass) == []


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


def _find_eval_bar_card(config):
    """Return the single markdown card that drives the eval bar (reads eval_cp)."""
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "markdown" and "eval_cp" in repr(node.get("card_mod", "")):
                yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from walk(item)

    cards = list(walk(config))
    assert len(cards) == 1, f"expected exactly one eval-bar card, found {len(cards)}"
    return cards[0]


def test_eval_bar_is_card_mod_not_inline_html(rendered_config) -> None:
    """The eval bar must be drawn with card-mod CSS, not inline-styled HTML.

    HA's markdown card sanitizes its body with js-xss, whose default
    whitelist allows <div>/<span> as tags but with *zero* attributes, so
    every inline ``style="..."`` is stripped and an inline-HTML bar renders
    blank. The eval bar is therefore drawn by a card-mod ``style:`` block
    (gradient fill + ::before/::after labels) injected into the shadow DOM,
    which bypasses the sanitizer. This locks the shape in so a future edit
    can't regress to inline HTML. See memory ha-markdown-card-strips-inline-css.
    """
    card = _find_eval_bar_card(rendered_config)
    style = card["card_mod"]["style"]

    # The markdown body carries no styled HTML; the bar lives entirely in CSS.
    assert "<div" not in repr(card.get("content", ""))
    # card-mod CSS draws the bar: gradient fill + pseudo-element labels.
    assert "linear-gradient" in style
    assert "ha-card::before" in style
    assert "ha-card::after" in style
    # The body markdown is hidden so only the styled ha-card shows.
    assert "ha-markdown" in style and "display: none" in style
    # Eval entities resolve to the board's real entity_ids (MAC-slug fallback here).
    assert "sensor.phantom_c8_c9_a3_f2_7c_0a_eval_cp" in style
    assert "sensor.phantom_c8_c9_a3_f2_7c_0a_eval_mate" in style
    # No placeholder leaked into the CSS.
    assert "YOUR_BOARD_MAC" not in style


def test_no_inline_style_colors_use_font_tag(rendered_config) -> None:
    """No card may carry inline ``style=`` HTML; colored markup uses <font>.

    HA's markdown card sanitizes its body with js-xss, whose default
    whitelist allows <div>/<span> as tags but with zero attributes, so an
    inline ``style="color:..."`` is silently stripped and the color is lost.
    The moves table, last-move status, and game-review cards therefore carry
    their per-item colors via ``<font color="...">`` (which js-xss keeps:
    ``font`` whitelists ``color``/``size``/``face``). This guards the whole
    rendered config against a regression back to inline style. See memory
    ha-markdown-card-strips-inline-css.
    """
    # Collect every markdown card's content from the rendered config.
    contents = [
        str(c.get("content", ""))
        for c in _walk_cards(rendered_config)
        if c.get("type") == "markdown"
    ]
    blob = "\n".join(contents)

    # No card carries inline style= (js-xss would strip it, losing the color).
    assert "<span style=" not in blob, "inline <span style= leaked into a card"
    assert "<div style=" not in blob, "inline <div style= leaked into a card"
    # Colored markup is carried by <font color=...> instead, which survives.
    assert "<font color=" in blob, "expected <font color=...> for colored markup"

    # Each of the three colored cards is present and uses <font>, not span/div.
    # (identify by a stable substring unique to each card's content)
    def _card(needle: str) -> str:
        hits = [c for c in contents if needle in c]
        assert len(hits) == 1, f"expected 1 card containing {needle!r}, found {len(hits)}"
        return hits[0]

    # "num_pairs" is unique to the moves-table card (move_history is also
    # referenced by the AI-vs-AI status card, so don't key on that).
    moves = _card("num_pairs")
    assert "<font color=" in moves and "<span" not in moves and "<div" not in moves

    last_move = _card("last_move_classification")
    assert "<font color=" in last_move and "<span" not in last_move

    review = _card("biggest mistakes")
    assert "<font color=" in review and "<span" not in review and "<div" not in review


def test_copy_button_assets_provisions_pngs(tmp_path):
    """The 5 bundled mascot button PNGs copy into <config>/www/phantom_chess/buttons/."""
    import types
    from custom_components.phantom_chess.dashboard_provision import _copy_button_assets

    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *a: str(tmp_path.joinpath(*a)))
    )
    _copy_button_assets(hass)
    dest = tmp_path / "www" / "phantom_chess" / "buttons"
    names = {p.name for p in dest.glob("*.png")}
    assert {
        "lichess.png", "stockfish.png", "historic.png",
        "two_player.png", "ai_vs_ai.png",
    } <= names


# ─── B6: dashboards_collection path ─────────────────────────────────────


import pytest as _pytest
from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock

from custom_components.phantom_chess.dashboard_provision import (
    CONF_URL_PATH,
    DASHBOARD_URL_PATH,
    LOVELACE_DATA,
    _try_register_via_collection,
)


def _make_collection(*, existing_items=None):
    """Build a mock DashboardsCollection with async_items / async_create_item /
    async_update_item."""
    collection = _MagicMock()
    items = list(existing_items or [])
    collection.async_items = _MagicMock(return_value=items)
    collection.async_create_item = _AsyncMock()
    collection.async_update_item = _AsyncMock()
    return collection


@_pytest.mark.asyncio
async def test_try_register_via_collection_creates_when_not_present():
    """When the collection has no matching url_path row, async_create_item is
    called with the full row dict.

    Old code: _try_register_via_collection didn't exist → ImportError / AttributeError
    → test fails.  New code: collection.async_create_item is called and the
    function returns True.
    """
    collection = _make_collection()
    lovelace = _MagicMock()
    lovelace.dashboards_collection = collection

    hass = _MagicMock()
    hass.data = {LOVELACE_DATA: lovelace}

    row = {"id": "phantom_chess", CONF_URL_PATH: DASHBOARD_URL_PATH, "mode": "storage"}

    result = await _try_register_via_collection(hass, row)
    assert result is True
    collection.async_create_item.assert_awaited_once_with(row)
    collection.async_update_item.assert_not_awaited()


@_pytest.mark.asyncio
async def test_try_register_via_collection_updates_when_present():
    """When a matching row already exists, async_update_item is called instead
    of async_create_item, using the existing row's id as the key."""
    existing = {"id": "phantom_chess", CONF_URL_PATH: DASHBOARD_URL_PATH, "title": "old"}
    collection = _make_collection(existing_items=[existing])
    lovelace = _MagicMock()
    lovelace.dashboards_collection = collection

    hass = _MagicMock()
    hass.data = {LOVELACE_DATA: lovelace}

    row = {"id": "phantom_chess", CONF_URL_PATH: DASHBOARD_URL_PATH, "title": "new"}

    result = await _try_register_via_collection(hass, row)
    assert result is True
    collection.async_update_item.assert_awaited_once_with("phantom_chess", row)
    collection.async_create_item.assert_not_awaited()


@_pytest.mark.asyncio
async def test_try_register_via_collection_returns_false_when_no_collection():
    """When lovelace_data has no dashboards_collection attribute, the helper
    returns False so the caller falls back to the Store write."""
    class _NoCollection:
        pass

    hass = _MagicMock()
    hass.data = {LOVELACE_DATA: _NoCollection()}

    row = {"id": "phantom_chess", CONF_URL_PATH: DASHBOARD_URL_PATH}
    result = await _try_register_via_collection(hass, row)
    assert result is False


@_pytest.mark.asyncio
async def test_try_register_via_collection_returns_false_when_no_lovelace_data():
    """When LOVELACE_DATA is absent from hass.data, the helper returns False."""
    hass = _MagicMock()
    hass.data = {}
    row = {"id": "phantom_chess", CONF_URL_PATH: DASHBOARD_URL_PATH}
    result = await _try_register_via_collection(hass, row)
    assert result is False

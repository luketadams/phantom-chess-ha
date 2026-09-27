"""Tests for the dashboard renderer and Lovelace registration.

The renderer reads the bundled ``dashboard_app.yaml`` (four views of the
packaged ``phantom-chess-card``) and resolves each templated entity reference
against the entity registry.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock, patch

import pytest
import pytest as _pytest

from custom_components.phantom_chess.dashboard_provision import (
    CARD_URL,
    CARD_URL_VERSIONED,
    CONF_URL_PATH,
    DASHBOARD_URL_PATH,
    LOVELACE_DATA,
    _TEMPLATE_ENTITY_REF,
    _TEMPLATE_PATH,
    _TEMPLATE_TO_UNIQUE_SUFFIX_ALIASES,
    _mac_to_slug,
    _render_template,
    _resolve_or_fallback,
    _try_register_via_collection,
)

_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "phantom_chess"
_TEST_MAC = "C8:C9:A3:F2:7C:0A"


# ─── the bundled template ──────────────────────────────────────────────


def test_render_resolves_every_reference() -> None:
    text = _TEMPLATE_PATH.read_text()
    entity_map = {("sensor", "live_position"): "sensor.living_room_board_live_position"}
    rendered = _render_template(text, _TEST_MAC, entity_map)
    assert "YOUR_BOARD_MAC" not in rendered
    assert "sensor.living_room_board_live_position" in rendered
    # Unregistered references fall back to the MAC slug.
    assert "switch.phantom_c8_c9_a3_f2_7c_0a_paused" in rendered


def test_card_url_tracks_manifest_version() -> None:
    import json
    version = json.loads((_PKG / "manifest.json").read_text())["version"]
    assert CARD_URL_VERSIONED == f"{CARD_URL}?v={version}"


def test_manifest_version_is_not_hardcoded_in_code() -> None:
    import json
    version = json.loads((_PKG / "manifest.json").read_text())["version"]
    for path in _PKG.glob("*.py"):
        assert f"v={version}" not in path.read_text(encoding="utf-8"), path.name


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



# ─── dashboards_collection path ─────────────────────────────────────


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


async def test_native_dashboard_uses_bundled_card_and_resolved_entities():
    from unittest.mock import patch
    from custom_components.phantom_chess.dashboard_provision import build_dashboard_config
    with patch("custom_components.phantom_chess.dashboard_provision._resolve_entity_ids", return_value={}):
        config = await build_dashboard_config(_MagicMock(), _TEST_MAC)
    assert [v["path"] for v in config["views"]] == ["main", "learn", "review", "board"]
    for view in config["views"]:
        card = view["cards"][0]
        assert card["type"] == "custom:phantom-chess-card"
        assert "YOUR_BOARD_MAC" not in str(card)
        assert card["entity"].endswith("live_position")

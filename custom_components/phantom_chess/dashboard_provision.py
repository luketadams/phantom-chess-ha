"""Provision the bundled Phantom Chess card as a Home Assistant dashboard.

The dashboard (``dashboard_app.yaml``) is four views of the packaged
``phantom-chess-card`` JavaScript card. Rendering resolves each templated
entity reference against the entity registry, so the dashboard follows the
real entity IDs whatever slug Home Assistant chose. Lovelace storage and
sidebar registration are idempotent across entry reloads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Final

import yaml

from homeassistant.components import frontend
from homeassistant.components.lovelace import dashboard as ll_dashboard
from homeassistant.components.lovelace.const import (
    CONF_ICON,
    CONF_REQUIRE_ADMIN,
    CONF_SHOW_IN_SIDEBAR,
    CONF_TITLE,
    CONF_URL_PATH,
    LOVELACE_DATA,
    MODE_STORAGE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import CONF_BLE_ADDRESS, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Lovelace storage internals — must match
# homeassistant.components.lovelace.dashboard. They've been stable since the
# multi-dashboard feature landed in core 0.107.0 (March 2020). If a future
# core release renames them this module will break loudly with ImportError
# the next time the integration loads, which is the failure mode we want.
DASHBOARDS_STORAGE_KEY: Final = "lovelace_dashboards"
DASHBOARDS_STORAGE_VERSION: Final = 1
CONFIG_STORAGE_VERSION: Final = 1
# DashboardsCollection uses CONF_URL_PATH as the suggested ID, so the
# per-dashboard config file lands at .storage/lovelace.<url_path>.
DASHBOARD_URL_PATH: Final = "phantom-chess"
DASHBOARD_ID: Final = "phantom_chess"
DASHBOARD_TITLE: Final = "Phantom Chess"
DASHBOARD_ICON: Final = "mdi:chess-knight"
CARD_URL: Final = "/phantom_chess_static/phantom-chess-card.js"

_TEMPLATE_PATH: Final = Path(__file__).parent / "dashboard_app.yaml"

# The template references entities as `<domain>.phantom_YOUR_BOARD_MAC_<suffix>`.
# HA picks entity_id slugs from the device name at first registration, so the
# real IDs differ between installs (e.g. `phantom_6552_*`, `living_room_*`, or
# the MAC slug). The renderer looks each reference up in the entity registry
# by (domain, unique_id suffix).
#
# A few entities use a unique_id suffix that differs from the template suffix.
_TEMPLATE_TO_UNIQUE_SUFFIX_ALIASES: Final[dict[str, str]] = {
    "board": "board_image",  # image platform; template uses "board", registry has "board_image"
    "paused": "pause",  # pause switch: entity_id ends "_paused", unique_id ends "_pause"
}

_TEMPLATE_ENTITY_REF = re.compile(
    r"\b(?P<domain>binary_sensor|select|switch|number|sensor|image|button)"
    r"\.phantom_YOUR_BOARD_MAC_(?P<suffix>[a-z][a-z0-9_]*)"
)


# --------------------------------------------------------------------------- #
# Template rendering                                                          #
# --------------------------------------------------------------------------- #


def _mac_to_slug(ble_address: str) -> str:
    """Convert ``AA:BB:CC:DD:EE:FF`` (any case) to ``aa_bb_cc_dd_ee_ff``.

    Fallback only — used to construct a guessed entity_id when the
    entity registry doesn't have a matching entity yet. The registry lookup
    in :func:`_resolve_entity_ids` is the authoritative source.
    """
    return ble_address.replace(":", "_").lower()


def _resolve_entity_ids(
    hass: HomeAssistant, ble_address: str
) -> dict[tuple[str, str], str]:
    """Build a ``(domain, unique_id_suffix) → entity_id`` map for this board."""
    registry = er.async_get(hass)
    prefix = f"{ble_address.upper()}_"
    result: dict[tuple[str, str], str] = {}
    for entity in registry.entities.values():
        if entity.platform != DOMAIN or not entity.unique_id.startswith(prefix):
            continue
        domain = entity.entity_id.split(".", 1)[0]
        result[(domain, entity.unique_id[len(prefix):])] = entity.entity_id
    return result


def _resolve_or_fallback(
    domain: str,
    template_suffix: str,
    entity_map: dict[tuple[str, str], str],
    mac_slug: str,
) -> str:
    """Resolve a templated reference, or guess from the MAC if unregistered.

    The first setup_entry call may run before the platform forward registers
    every entity, so an unknown reference falls back to the MAC-slug form.
    """
    unique_suffix = _TEMPLATE_TO_UNIQUE_SUFFIX_ALIASES.get(template_suffix, template_suffix)
    if (entity_id := entity_map.get((domain, unique_suffix))) is not None:
        return entity_id
    return f"{domain}.phantom_{mac_slug}_{template_suffix}"


def _render_template(
    yaml_text: str,
    ble_address: str,
    entity_map: dict[tuple[str, str], str],
) -> str:
    """Replace every templated entity reference with a real entity_id."""
    mac_slug = _mac_to_slug(ble_address)
    return _TEMPLATE_ENTITY_REF.sub(
        lambda m: _resolve_or_fallback(m.group("domain"), m.group("suffix"), entity_map, mac_slug),
        yaml_text,
    )


async def build_dashboard_config(hass: HomeAssistant, ble_address: str) -> dict[str, Any]:
    """Render the bundled dashboard for ``ble_address`` and parse it.

    The template is read off the event loop to avoid HA's blocking-call warning.
    """
    entity_map = _resolve_entity_ids(hass, ble_address)
    yaml_text: str = await asyncio.to_thread(_TEMPLATE_PATH.read_text, encoding="utf-8")
    config = yaml.safe_load(_render_template(yaml_text, ble_address, entity_map))
    if not isinstance(config, dict):
        raise ValueError("Rendered dashboard template did not parse as a YAML mapping")
    return config


# The card URL carries the integration version as a cache-buster: browsers
# cache the card aggressively, and a new version after an update forces them
# to load the new JavaScript. Read once at import (HA imports integrations in
# an executor, so this file read never blocks the event loop).
_VERSION: Final = json.loads(
    (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
)["version"]
CARD_URL_VERSIONED: Final = f"{CARD_URL}?v={_VERSION}"


# --------------------------------------------------------------------------- #
# Provision / Unprovision                                                     #
# --------------------------------------------------------------------------- #


async def _try_register_via_collection(
    hass: HomeAssistant, row: dict[str, Any]
) -> bool:
    """Register or update the dashboard row via the in-memory DashboardsCollection.

    Returns True if the collection was available and the call succeeded;
    False if the collection isn't exposed or the call failed (caller falls
    back to the direct Store write so persistence still happens).
    """
    lovelace_data = hass.data.get(LOVELACE_DATA)
    collection = getattr(lovelace_data, "dashboards_collection", None)
    if collection is None:
        return False
    try:
        existing = next(
            (item for item in collection.async_items()
             if item.get(CONF_URL_PATH) == DASHBOARD_URL_PATH),
            None,
        )
        if existing is not None:
            await collection.async_update_item(existing["id"], row)
        else:
            await collection.async_create_item(row)
        return True
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "dashboards_collection not usable — falling back to Store write"
        )
        return False


async def _async_load_dashboards_store(hass: HomeAssistant) -> tuple[Store, list[dict]]:
    """Load the persistent lovelace_dashboards Store contents.

    Returns the Store handle plus the current items list. The items list is
    a list of dashboard-row dicts matching what
    ``DashboardsCollection.async_create_item`` would persist.
    """
    store: Store = Store(hass, DASHBOARDS_STORAGE_VERSION, DASHBOARDS_STORAGE_KEY)
    data = await store.async_load() or {}
    # DictStorageCollection stores rows under "items" as a list of dicts.
    items = list(data.get("items", []))
    return store, items


async def async_provision_dashboard(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Create / refresh the Phantom Chess dashboard for this config entry.

    Idempotent — safe to call on every setup_entry. If the dashboard panel
    already exists in the frontend (e.g. after a HA restart loaded the
    persisted row), this function just refreshes the storage config so the
    panel always reflects the current MAC.
    """

    ble_address = entry.data.get(CONF_BLE_ADDRESS)
    if not ble_address:
        _LOGGER.warning(
            "Cannot provision dashboard: config entry %s has no BLE address",
            entry.entry_id,
        )
        return

    try:
        config = await build_dashboard_config(hass, ble_address)
    except Exception:  # noqa: BLE001 — surface template errors loudly
        _LOGGER.exception("Failed to render Phantom Chess dashboard")
        return

    # Frontend is ready when Lovelace provisions the dashboard.
    frontend.add_extra_js_url(hass, CARD_URL_VERSIONED)

    # 1. Persist the per-dashboard config (lovelace.<id> store).
    storage_meta = {"id": DASHBOARD_ID, CONF_URL_PATH: DASHBOARD_URL_PATH}
    lovelace_storage = ll_dashboard.LovelaceStorage(hass, storage_meta)
    try:
        await lovelace_storage.async_save(config)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Failed to save Phantom Chess dashboard storage")
        return

    # 2. Persist the dashboards collection row so the dashboard survives a
    #    restart. The row shape matches what DashboardsCollection persists
    #    after its _process_create_data validation runs (which also strips
    #    CONF_ALLOW_SINGLE_WORD — so we don't include it). See
    #    homeassistant.components.lovelace.dashboard.
    row = {
        "id": DASHBOARD_ID,
        CONF_URL_PATH: DASHBOARD_URL_PATH,
        CONF_TITLE: DASHBOARD_TITLE,
        CONF_ICON: DASHBOARD_ICON,
        CONF_SHOW_IN_SIDEBAR: True,
        CONF_REQUIRE_ADMIN: False,
        "mode": MODE_STORAGE,
    }
    # Prefer the in-memory DashboardsCollection when available so core's
    # in-memory state stays consistent with the Store.  Fallback to direct
    # Store write for environments where the collection isn't exposed
    # (e.g. YAML-mode Lovelace, older HA builds).
    if not await _try_register_via_collection(hass, row):
        store, items = await _async_load_dashboards_store(hass)
        existing_idx: int | None = next(
            (i for i, item in enumerate(items) if item.get(CONF_URL_PATH) == DASHBOARD_URL_PATH),
            None,
        )
        if existing_idx is None:
            items.append(row)
        else:
            # Update in place so title/icon changes (or recovery from a broken
            # half-row) get applied without duplicating.
            items[existing_idx] = {**items[existing_idx], **row}
        await store.async_save({"items": items})

    # 3. Register the panel + LovelaceStorage in-memory so the user sees the
    #    sidebar entry immediately, without a restart.
    if not frontend.async_panel_exists(hass, DASHBOARD_URL_PATH):
        try:
            frontend.async_register_built_in_panel(
                hass,
                "lovelace",
                frontend_url_path=DASHBOARD_URL_PATH,
                sidebar_title=DASHBOARD_TITLE,
                sidebar_icon=DASHBOARD_ICON,
                show_in_sidebar=True,
                require_admin=False,
                config={"mode": MODE_STORAGE},
            )
        except ValueError:
            # Panel already exists under a different registration — leave it.
            _LOGGER.debug(
                "Lovelace panel %s already registered", DASHBOARD_URL_PATH
            )

    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is not None:
        lovelace_data.dashboards[DASHBOARD_URL_PATH] = lovelace_storage

    _LOGGER.info(
        "Provisioned Phantom Chess dashboard at /%s for board %s",
        DASHBOARD_URL_PATH,
        ble_address,
    )


async def async_unprovision_dashboard(hass: HomeAssistant) -> None:
    """Remove the Phantom Chess dashboard.

    Called on integration removal (``async_remove_entry``). Cleans up:
      - the in-memory panel registration
      - the LOVELACE_DATA dashboards dict entry
      - the persistent .storage/lovelace.<id> file
      - the row in .storage/lovelace_dashboards
    """
    # 1. Drop the in-memory panel.
    if frontend.async_panel_exists(hass, DASHBOARD_URL_PATH):
        try:
            frontend.async_remove_panel(hass, DASHBOARD_URL_PATH)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to remove panel %s", DASHBOARD_URL_PATH)

    # 2. Drop from LOVELACE_DATA so websocket lookups stop returning it.
    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is not None:
        lovelace_storage = lovelace_data.dashboards.pop(DASHBOARD_URL_PATH, None)
        if lovelace_storage is not None:
            try:
                await lovelace_storage.async_delete()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Failed to delete LovelaceStorage data")

    # 3. Remove the row from the persistent collection.
    store, items = await _async_load_dashboards_store(hass)
    new_items = [item for item in items if item.get(CONF_URL_PATH) != DASHBOARD_URL_PATH]
    if new_items != items:
        await store.async_save({"items": new_items})

    _LOGGER.info("Unprovisioned Phantom Chess dashboard at /%s", DASHBOARD_URL_PATH)

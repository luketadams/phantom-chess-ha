"""Auto-provision a Lovelace dashboard for a Phantom Chess board.

The integration's v0.3.x release shipped a 1083-line "rich" dashboard in
``examples/dashboard-rich.yaml`` that the user had to copy by hand and run a
find/replace on the YOUR_BOARD_MAC placeholder. v0.4 promotes this dashboard
to a first-class integration deliverable: on config-entry setup we render the
template against the user's board MAC and install it as a real
``select.phantom-chess`` dashboard in the sidebar.

Strategy
--------
1. ``dashboard_template.yaml`` (bundled in the integration package) is the
   v0.3 rich template, unchanged. Keeping the template in YAML rather than
   building cards in Python keeps the dashboard editable by humans and easy
   to diff.
2. At provision time we apply a small set of text substitutions:
     - MAC slug (``YOUR_BOARD_MAC`` → ``aa_bb_cc_dd_ee_ff``).
     - Helper entities (``input_select.phantom_chess_*`` →
       ``select.phantom_<mac>_*``, ``input_number.*`` → ``number.*``,
       ``input_boolean.phantom_chess_training_wheels`` →
       ``switch.phantom_<mac>_training_wheels``).
     - Helper service domains (``input_select.select_option`` →
       ``select.select_option``, etc.).
     - Script tile references and tap_actions
       (``script.phantom_back_to_modes`` → ``phantom_chess.back_to_modes``
       service call, and the tile's ``entity:`` is repointed at
       ``binary_sensor.phantom_<mac>_connected`` with ``hide_state: true`` so
       it renders as an icon-only button).
3. The rendered YAML is parsed and written to ``.storage/lovelace.<id>``
   via ``LovelaceStorage.async_save``.
4. The dashboard registration row is appended to ``.storage/lovelace_dashboards``
   via the same ``Store`` helper Home Assistant's
   ``DashboardsCollection`` uses. We write storage directly because the
   collection object is only kept as a closure inside ``lovelace.async_setup``
   and has no public accessor — see core/homeassistant/components/lovelace/__init__.py.
5. A Lovelace panel is registered in-memory via
   ``frontend.async_register_built_in_panel`` so the dashboard appears in the
   sidebar without a restart.
6. The ``LovelaceStorage`` instance is added to
   ``hass.data[LOVELACE_DATA].dashboards[url_path]`` so the websocket
   ``lovelace/config`` lookup finds it.

The provision is idempotent: ``async_panel_exists`` is checked first so a
restart, second config entry, or reload doesn't duplicate the row.

Unprovision (called from ``async_remove_entry``) reverses all four side
effects. ``async_unload_entry`` does NOT unprovision — the dashboard is
meant to survive reload, and users who delete a single config entry but
keep the integration installed get to keep their dashboard until they
remove the integration entirely.
"""

from __future__ import annotations

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
DASHBOARD_TITLE: Final = "Chess"
DASHBOARD_ICON: Final = "mdi:chess-knight"

# v0.3 setup-pack script names → v0.4 native service names.
_SCRIPT_TO_SERVICE: Final[dict[str, str]] = {
    "phantom_back_to_modes": "back_to_modes",
    "phantom_play_selected_sculpture": "play_selected_sculpture",
    "phantom_start_lichess_configured": "start_lichess_configured",
    "phantom_request_hint": "request_hint",
    "phantom_resign": "resign",
    "phantom_takeback": "takeback",
}

# Per-instance helper entity_ids that the dashboard template references and
# their native v0.4 equivalents. The leading "phantom_chess" namespace was a
# global helper convention from v0.3; v0.4 has per-board entities under
# "phantom_<mac>_*".
_HELPER_ENTITY_REWRITES: Final[dict[str, str]] = {
    "input_select.phantom_chess_setup_mode": "select.phantom_{mac}_setup_mode",
    "input_select.phantom_chess_sculpture_game": "select.phantom_{mac}_sculpture_game",
    "input_number.phantom_chess_lichess_clock_minutes": "number.phantom_{mac}_lichess_clock_minutes",
    "input_number.phantom_chess_lichess_clock_increment": "number.phantom_{mac}_lichess_clock_increment",
    "input_boolean.phantom_chess_training_wheels": "switch.phantom_{mac}_training_wheels",
}

# Helper service-domain rewrites: helper domains and native-entity domains
# expose the same actions under different names.
_SERVICE_DOMAIN_REWRITES: Final[dict[str, str]] = {
    "input_select.select_option": "select.select_option",
    "input_number.set_value": "number.set_value",
    "input_boolean.toggle": "switch.toggle",
    "input_boolean.turn_on": "switch.turn_on",
    "input_boolean.turn_off": "switch.turn_off",
}

_TEMPLATE_PATH: Final = Path(__file__).parent / "dashboard_template.yaml"


# --------------------------------------------------------------------------- #
# Template rendering                                                          #
# --------------------------------------------------------------------------- #


def _mac_to_slug(ble_address: str) -> str:
    """Convert ``AA:BB:CC:DD:EE:FF`` (any case) to ``aa_bb_cc_dd_ee_ff``.

    The integration's entity-id slug pattern. Matches what the platforms
    build via ``device_info`` + slugify.
    """
    return ble_address.replace(":", "_").lower()


def _render_template(yaml_text: str, mac_slug: str) -> str:
    """Apply the v0.3→v0.4 text substitutions to the rich dashboard template.

    Performed at the text layer (not via yaml.dump) so the bundled template
    stays human-diffable and the substitutions don't reorder keys or rewrap
    long multiline strings.
    """
    text = yaml_text

    # 1. MAC slug — replaces the literal placeholder used throughout.
    text = text.replace("YOUR_BOARD_MAC", mac_slug)

    # 2. Helper entity ids → native entity ids.
    for old, new_tpl in _HELPER_ENTITY_REWRITES.items():
        text = text.replace(old, new_tpl.format(mac=mac_slug))

    # 3. Service-domain rewrites (input_select.select_option → select.select_option).
    for old, new in _SERVICE_DOMAIN_REWRITES.items():
        text = text.replace(old, new)

    # 4. Script tile references. Each "entity: script.phantom_X" is in a tile
    #    that uses the script as a clickable button. v0.4 has no script — we
    #    point the tile's `entity:` at the always-present "connected" sensor
    #    purely for display, hide its state, and rewrite the tap_action to
    #    invoke the native service.
    connected_entity = f"binary_sensor.phantom_{mac_slug}_connected"
    for script_name in _SCRIPT_TO_SERVICE:
        text = text.replace(
            f"entity: script.{script_name}",
            f"entity: {connected_entity}",
        )

    # 5. tap_action service calls that target script.phantom_X. The rich
    #    dashboard mixes four shapes:
    #      a) action: call-service / service: script.turn_on / target: entity_id: ...
    #      b) action: perform-action / perform_action: script.turn_on / target: entity_id: ...
    #      c) action: perform-action / perform_action: script.turn_on / data: entity_id: ...
    #      d) action: call-service / service: script.turn_on / data: entity_id: ...
    #    All four rewrite to `action: perform-action / perform_action: <domain>.<service>`.
    #    The indentation capture group preserves the surrounding YAML structure.
    pattern_target = re.compile(
        r"action: (?:call-service|perform-action)\n"
        r"(?P<indent>\s+)(?:service|perform_action): script\.turn_on\n"
        r"\s+target:\n\s+entity_id: script\.(?P<name>phantom_[a-z_]+)",
    )
    pattern_data = re.compile(
        r"action: (?:call-service|perform-action)\n"
        r"(?P<indent>\s+)(?:service|perform_action): script\.turn_on\n"
        r"\s+data:\n\s+entity_id: script\.(?P<name>phantom_[a-z_]+)",
    )

    def _rewrite(match: re.Match[str]) -> str:
        script_name = match.group("name")
        service = _SCRIPT_TO_SERVICE.get(script_name)
        if service is None:
            # Unknown script — leave the original action intact so it surfaces
            # as a broken reference rather than silently changing behavior.
            return match.group(0)
        indent = match.group("indent")
        return f"action: perform-action\n{indent}perform_action: {DOMAIN}.{service}"

    text = pattern_target.sub(_rewrite, text)
    text = pattern_data.sub(_rewrite, text)

    return text


def build_dashboard_config(ble_address: str) -> dict[str, Any]:
    """Render the bundled template against ``ble_address`` and parse to dict.

    Returns the Lovelace config dict that ``LovelaceStorage.async_save``
    expects (``title``, ``views``, etc.).
    """
    mac_slug = _mac_to_slug(ble_address)
    yaml_text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = _render_template(yaml_text, mac_slug)
    config = yaml.safe_load(rendered)
    if not isinstance(config, dict):
        raise ValueError(
            "Rendered dashboard template did not parse as a YAML mapping"
        )
    return config


# --------------------------------------------------------------------------- #
# Provision / Unprovision                                                     #
# --------------------------------------------------------------------------- #


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
        config = build_dashboard_config(ble_address)
    except Exception:  # noqa: BLE001 — surface template errors loudly
        _LOGGER.exception("Failed to render Phantom Chess dashboard template")
        return

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

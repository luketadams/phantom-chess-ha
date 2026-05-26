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

import asyncio
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

# v0.3 helper entities → unique_id suffixes of their v0.4 native replacements.
# The renderer rewrites every `input_select.phantom_chess_X` (and similar) to
# the corresponding native entity, looked up at provision time from the
# entity registry by (target_domain, unique_id_suffix).
_HELPER_TO_NATIVE: Final[dict[str, tuple[str, str]]] = {
    "input_select.phantom_chess_setup_mode": ("select", "setup_mode"),
    "input_select.phantom_chess_sculpture_game": ("select", "sculpture_game"),
    "input_number.phantom_chess_lichess_clock_minutes": ("number", "lichess_clock_minutes"),
    "input_number.phantom_chess_lichess_clock_increment": ("number", "lichess_clock_increment"),
    "input_boolean.phantom_chess_training_wheels": ("switch", "training_wheels"),
    # v0.3 template helper replaced by alpha3's native board_idle binary_sensor.
    "binary_sensor.phantom_chess_board_idle": ("binary_sensor", "board_idle"),
}

# Entity-id resolution: the template references entities as
# `<domain>.phantom_YOUR_BOARD_MAC_<suffix>`, but HA can pick different
# entity_id slugs depending on when each entity was first registered (v0.3
# entities were created with the MAC slug, v0.4 alpha entities use the
# device-name slug like "Phantom 6552"). Predicting the slug from the MAC
# is therefore unreliable — the renderer must look up real entity_ids from
# the entity registry at provision time.
#
# Most (domain, template_suffix) pairs map directly to a unique_id suffix.
# A few entities use a different suffix in their unique_id than in their
# entity_id slug (e.g. the image entity's unique_id is "<MAC>_board_image"
# but its entity_id ends in "_board"). Those go in this alias map; the
# rest resolve identically.
_TEMPLATE_TO_UNIQUE_SUFFIX_ALIASES: Final[dict[str, str]] = {
    "board": "board_image",  # image platform; template uses "board", registry has "board_image"
}

# Regex matching a templated entity reference. Captures the domain and the
# template suffix; produces an entity_id when resolved against the registry.
_TEMPLATE_ENTITY_REF = re.compile(
    r"\b(?P<domain>binary_sensor|select|switch|number|sensor|image|button)"
    r"\.phantom_YOUR_BOARD_MAC_(?P<suffix>[a-z][a-z0-9_]*)"
)

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

    Fallback only — used to construct a guessed entity_id when the
    entity registry doesn't have a matching entity yet. The registry lookup
    in :func:`_resolve_entity_ids` is the authoritative source.
    """
    return ble_address.replace(":", "_").lower()


def _resolve_entity_ids(
    hass: HomeAssistant, ble_address: str
) -> dict[tuple[str, str], str]:
    """Build a ``(domain, unique_id_suffix) → entity_id`` map for this board.

    Walks the entity registry, picks out the phantom_chess entities whose
    unique_id starts with the upper-case MAC, and indexes them by their
    domain + suffix. The renderer uses this map to substitute the right
    entity_id for every template reference, regardless of whether the
    entity was registered with the legacy MAC slug
    (``phantom_c8_c9_a3_f2_7c_0a_*``) or the modern device-name slug
    (``phantom_6552_*``).
    """
    registry = er.async_get(hass)
    address_upper = ble_address.upper()
    prefix = f"{address_upper}_"
    result: dict[tuple[str, str], str] = {}
    for entity in registry.entities.values():
        if entity.platform != DOMAIN:
            continue
        if not entity.unique_id.startswith(prefix):
            continue
        suffix = entity.unique_id[len(prefix):]
        domain = entity.entity_id.split(".", 1)[0]
        result[(domain, suffix)] = entity.entity_id
    return result


def _resolve_or_fallback(
    domain: str,
    template_suffix: str,
    entity_map: dict[tuple[str, str], str],
    mac_slug: str,
) -> str:
    """Resolve a templated entity reference to a real entity_id.

    Looks up ``(domain, suffix)`` in the registry-derived map, applying the
    template-to-unique alias for the special-cased image entity. Falls back
    to a MAC-slug-based guess if the entity isn't yet registered (the first
    setup_entry call may run before the platform forward completes).
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
    """Apply the v0.3→v0.4 text substitutions to the rich dashboard template.

    Performed at the text layer (not via yaml.dump) so the bundled template
    stays human-diffable and the substitutions don't reorder keys or rewrap
    long multiline strings. Order matters:

    1. Helper rewrites first — turn ``input_select.phantom_chess_X`` into
       a ``<native_domain>.phantom_YOUR_BOARD_MAC_X`` placeholder, which is
       then resolved in step 3.
    2. Service-domain rewrites — ``input_select.select_option`` →
       ``select.select_option`` etc.
    3. Entity-id resolution — every ``<domain>.phantom_YOUR_BOARD_MAC_<X>``
       reference is looked up in the registry and replaced with the real
       entity_id.
    4. Script tile/tap_action rewrites — point the tile's ``entity:`` at
       the connected sensor and rewrite tap_actions to invoke native
       services. Runs after entity resolution so the connected-sensor
       substitution uses the resolved entity_id.
    """
    mac_slug = _mac_to_slug(ble_address)
    text = yaml_text

    # 1. v0.3 helper entities → templated native entity ids (resolved in step 3).
    for helper_id, (native_domain, suffix) in _HELPER_TO_NATIVE.items():
        text = text.replace(
            helper_id,
            f"{native_domain}.phantom_YOUR_BOARD_MAC_{suffix}",
        )

    # 2. Service-domain rewrites (input_select.select_option → select.select_option).
    for old, new in _SERVICE_DOMAIN_REWRITES.items():
        text = text.replace(old, new)

    # 3. Entity-id resolution — turn every templated reference into a real
    #    entity_id by consulting the entity registry.
    def _resolve(m: re.Match[str]) -> str:
        return _resolve_or_fallback(
            m.group("domain"),
            m.group("suffix"),
            entity_map,
            mac_slug,
        )

    text = _TEMPLATE_ENTITY_REF.sub(_resolve, text)

    # Script-tile and script.turn_on tap_action rewrites are intentionally
    # deferred to :func:`_fixup_script_tiles` (post-parse, dict-level) so we
    # can both rewrite the entity AND inject the matching ``tap_action`` /
    # ``icon_tap_action`` for tiles that lacked an explicit tap_action.

    return text


def _walk_cards(node: Any):
    """Yield every dict that has a ``type`` field, depth-first.

    Used to find Lovelace cards anywhere in the config tree, including
    nested inside ``vertical-stack``, ``grid``, ``conditional`` cards,
    etc. Operates in-place; consumers mutate yielded dicts directly.
    """
    if isinstance(node, dict):
        if "type" in node:
            yield node
        for value in node.values():
            yield from _walk_cards(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_cards(item)


def _fixup_script_tiles(
    config: dict[str, Any],
    connected_entity: str,
) -> None:
    """Convert v0.3 script-based tiles into native-service buttons.

    The v0.3 dashboard used ``script.phantom_X`` entities as clickable
    buttons — clicking the script entity launched the action. After
    helper migration there are no scripts, so we replace each such tile
    with a visual icon-button:

    1. ``entity:`` repointed at the always-present ``connected`` sensor
       (so the icon still renders); ``hide_state: true`` so the connected
       state doesn't leak into the button label.
    2. ``tap_action:`` set to invoke ``phantom_chess.<service>`` directly.
    3. ``icon_tap_action:`` mirrors ``tap_action`` so clicks on the icon
       portion don't fall through to HA's default "show more info" popup
       (which opens the connected-sensor history dialog — see alpha7).

    Also handles the case where a tile already had an explicit
    ``tap_action`` pointing at ``phantom_chess.<service>`` (rewritten by
    the text-level pass earlier): the existing tap_action stays, and
    ``icon_tap_action`` is added to match.
    """
    for card in _walk_cards(config):
        if card.get("type") != "tile":
            continue

        entity_ref = card.get("entity", "")
        is_script_tile = isinstance(entity_ref, str) and entity_ref.startswith(
            "script.phantom_"
        )

        if is_script_tile:
            # Case A: v0.3 script-as-entity tile. Replace entity, inject
            # both tap_action and icon_tap_action.
            script_name = entity_ref.split(".", 1)[1]
            service = _SCRIPT_TO_SERVICE.get(script_name)
            if service is None:
                # Unknown script — leave it as-is so the broken reference
                # is visible to the user rather than silently swapped.
                continue
            action = {
                "action": "perform-action",
                "perform_action": f"{DOMAIN}.{service}",
            }
            card["entity"] = connected_entity
            card.setdefault("hide_state", True)
            # Preserve any existing confirmation prompt on tap_action.
            existing_tap = card.get("tap_action")
            if isinstance(existing_tap, dict) and "confirmation" in existing_tap:
                action_with_conf = {**action, "confirmation": existing_tap["confirmation"]}
                card["tap_action"] = action_with_conf
                card["icon_tap_action"] = action_with_conf
            else:
                card["tap_action"] = action
                card["icon_tap_action"] = action
            continue

        # Case B: tile that already had tap_action rewritten by the
        # text-level pass to phantom_chess.X. Mirror to icon_tap_action
        # so the icon doesn't open the entity popup.
        tap = card.get("tap_action")
        if not isinstance(tap, dict):
            continue
        # Recognise both "perform_action: phantom_chess.X" and the
        # legacy "service: phantom_chess.X" shapes.
        target_call = tap.get("perform_action") or tap.get("service")
        if not isinstance(target_call, str):
            continue
        if not target_call.startswith(f"{DOMAIN}."):
            continue
        # Mirror — but ONLY for tiles likely to be buttons (hide_state set,
        # or the entity is the connected sensor we repurposed). Leave
        # informational tiles like `binary_sensor.X_connected` "Bluetooth
        # connection" alone — they should still show entity history on
        # icon click.
        if card.get("hide_state") is not True and card.get("entity") != connected_entity:
            continue
        card.setdefault("icon_tap_action", tap)


async def build_dashboard_config(
    hass: HomeAssistant, ble_address: str
) -> dict[str, Any]:
    """Render the bundled template and parse to dict.

    Async because the template file is read off the event loop
    (``asyncio.to_thread``) to avoid the HA "blocking call" warning.

    Two-phase rendering:

    1. **Text-level pass** in :func:`_render_template`: helper-id and
       service-domain rewrites, plus entity-id resolution via the registry.
       Leaves ``entity: script.phantom_X`` references and
       ``service: script.turn_on`` tap_actions intact.

    2. **Dict-level pass** in :func:`_fixup_script_tiles` (post-parse):
       rewrites every script-based tile to use the native service and
       mirrors ``tap_action`` into ``icon_tap_action`` so icon clicks
       don't open HA's default entity-history popup.
    """
    entity_map = _resolve_entity_ids(hass, ble_address)
    yaml_text: str = await asyncio.to_thread(
        _TEMPLATE_PATH.read_text, encoding="utf-8"
    )

    # text-level pass needs `script.turn_on` tap_action rewrites collapsed
    # into perform_action before dict walk, so do them here as a quick
    # regex pass on the rendered text. They're shape-only (no need for
    # dict-level context).
    rendered = _render_template(yaml_text, ble_address, entity_map)
    rendered = _rewrite_script_turn_on_in_text(rendered)

    config = yaml.safe_load(rendered)
    if not isinstance(config, dict):
        raise ValueError(
            "Rendered dashboard template did not parse as a YAML mapping"
        )

    connected_entity = _resolve_or_fallback(
        "binary_sensor", "connected", entity_map, _mac_to_slug(ble_address)
    )
    _fixup_script_tiles(config, connected_entity)

    return config


# script.turn_on tap_action shapes (call-service/perform-action × target/data).
_SCRIPT_TURNON_PATTERNS: Final = (
    re.compile(
        r"action: (?:call-service|perform-action)\n"
        r"(?P<indent>\s+)(?:service|perform_action): script\.turn_on\n"
        r"\s+target:\n\s+entity_id: script\.(?P<name>phantom_[a-z_]+)"
    ),
    re.compile(
        r"action: (?:call-service|perform-action)\n"
        r"(?P<indent>\s+)(?:service|perform_action): script\.turn_on\n"
        r"\s+data:\n\s+entity_id: script\.(?P<name>phantom_[a-z_]+)"
    ),
)


def _rewrite_script_turn_on_in_text(text: str) -> str:
    """Collapse every ``script.turn_on`` tap_action targeting a v0.3 phantom
    script into ``perform-action`` with the native service name.

    Same effect as the previous text-level step 5, kept text-level so the
    parsed dict already has ``perform_action: phantom_chess.X`` to be
    mirrored into ``icon_tap_action`` by :func:`_fixup_script_tiles`.
    """
    def _rewrite(match: re.Match[str]) -> str:
        service = _SCRIPT_TO_SERVICE.get(match.group("name"))
        if service is None:
            return match.group(0)
        return (
            f"action: perform-action\n"
            f"{match.group('indent')}perform_action: {DOMAIN}.{service}"
        )

    for pattern in _SCRIPT_TURNON_PATTERNS:
        text = pattern.sub(_rewrite, text)
    return text


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
        config = await build_dashboard_config(hass, ble_address)
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

"""Phantom Chess Board integration."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
)

from .config_flow import _normalize_ble_address
from .const import CONF_BLE_ADDRESS, CONF_DEVICE_NAME, DOMAIN
from .coordinator import PhantomChessCoordinator
from .dashboard_provision import (
    async_provision_dashboard,
    async_unprovision_dashboard,
)

# Typed ConfigEntry alias — Bronze quality scale rule `runtime-data`.
# Storing the coordinator on entry.runtime_data (instead of
# hass.data[DOMAIN][entry.entry_id]) lets HA garbage-collect it on
# unload and gives type-checkers visibility into the stored value.
#
# `type X = ...` (PEP 695, Python 3.12+) is the modern preferred form;
# falls back to a plain TypeAlias on older runtimes via the import
# branch below. HACS floor is HA 2024.11 which ships Python 3.12+.
from typing import TypeAlias  # noqa: E402

PhantomChessConfigEntry: TypeAlias = ConfigEntry[PhantomChessCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Options-flow key. v0.4-alpha4 default: True (auto-provision the rich
# Chess dashboard at /phantom-chess on first setup). Power users who want
# to author their own dashboard can flip this off in Settings →
# Devices & Services → Phantom Chess → Configure.
OPT_AUTO_PROVISION_DASHBOARD = "auto_provision_dashboard"
DEFAULT_AUTO_PROVISION_DASHBOARD = True

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "select", "number", "switch", "button", "image"]

# ── Service schemas ───────────────────────────────────────────────────────────

SERVICE_START_GAME       = "start_game"
SERVICE_START_LOCAL_GAME = "start_local_game"
SERVICE_STOP_LOCAL_GAME  = "stop_local_game"
# AI-vs-AI play: Stockfish plays both sides on the physical board.
# Useful for autonomous protocol testing (e.g., castle handling) and
# as a "watch the AI play itself" demo. Added 2026-05-25.
SERVICE_START_AI_VS_AI_GAME = "start_ai_vs_ai_game"
SERVICE_START_TWO_PLAYER_GAME = "start_two_player_game"
SERVICE_RESYNC_TWO_PLAYER = "resync_two_player"
SERVICE_SEND_MOVE        = "send_move"
SERVICE_RESIGN           = "resign"
SERVICE_TAKEBACK         = "takeback"
SERVICE_DEBUG_BLE_WRITE  = "debug_ble_write"
# fw0.3.2 GAME_START length-rejection diagnostic (added 2026-06-14). Logs the
# negotiated MTU + UUID_GAME write limits and runs a non-destructive size probe;
# experimental=true also A/B-tests the candidate GAME_START write variants live.
SERVICE_DIAGNOSE_GAME_START = "diagnose_game_start"
# Confirmed-protocol services (added 2026-05-09 after HCI capture)
SERVICE_PHANTOM_START_GAME    = "phantom_start_game"
SERVICE_PHANTOM_APPLY_AI_MOVE = "phantom_apply_ai_move"
# Generic arbitrary-movement primitive (added 2026-05-12)
SERVICE_MOVE_PIECE            = "move_piece"
# Sculpture mode — write SELECT_MODE 1 to enter sculpture (added 2026-05-13)
SERVICE_START_SCULPTURE       = "start_sculpture"
# Position reset — drives physical board + self._board back to starting (added 2026-05-14)
SERVICE_RESET_POSITION        = "reset_position"
# Play firmware-native check/checkmate sound (opcode 9, per Efraín doc 2026-05-14)
SERVICE_PLAY_SOUND            = "play_sound"
# Lichess hint refresh — refetches cloud-eval, populates best_move_san sensor
SERVICE_REQUEST_HINT          = "request_hint"
# Dismiss the post-game review view (used by the Back to modes button)
SERVICE_DISMISS_REVIEW        = "dismiss_review"
# Reconcile local game state against Lichess (Task #14, 2026-05-16) — user-facing
# escape hatch when lichess_active gets stuck ON after a stream-task death.
SERVICE_RECONCILE_LICHESS_STATE = "reconcile_lichess_state"
# Resume physical board from current internal state (Task #7, 2026-05-16) — used
# after AI moves failed to drive the magnet and the user continued on phone.
SERVICE_RESUME_FROM_PHONE       = "resume_from_phone"
# Dashboard-initiated move (Task #27, 2026-05-17) — distinct from send_move
# because it drives the magnet for the user. send_move assumes the user has
# already moved the piece physically; execute_move drives the firmware to
# do it. Used by the bundled interactive web board at /phantom_chess_static/board.html.
SERVICE_EXECUTE_MOVE            = "execute_move"
# v0.4-alpha3 services — replace the script wrappers in v0.3's
# examples/scripts.yaml so the dashboard can call services directly
# without users having to install those scripts.
SERVICE_BACK_TO_MODES           = "back_to_modes"
SERVICE_RESYNC_DETECTION        = "resync_detection"
SERVICE_START_LICHESS_CONFIGURED = "start_lichess_configured"
SERVICE_PLAY_SELECTED_SCULPTURE  = "play_selected_sculpture"

START_GAME_SCHEMA = vol.Schema(
    {
        vol.Optional("ai_level"): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
        vol.Optional("color"): vol.In(["white", "black", "random"]),
        # Lichess clock parameters — combined limit + increment must be ≥ 4s.
        vol.Optional("clock_limit_seconds"): vol.All(vol.Coerce(int), vol.Range(min=60, max=10800)),
        vol.Optional("clock_increment_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=180)),
        # B2: required for multi-board installs; optional (ignored) for single-board.
        vol.Optional("entry_id"): cv.string,
    }
)

SEND_MOVE_SCHEMA = vol.Schema(
    {
        vol.Required("move"): cv.string,  # UCI notation, e.g. "e2e4"
        vol.Optional("entry_id"): cv.string,
    }
)

DEBUG_BLE_WRITE_SCHEMA = vol.Schema(
    {
        vol.Required("uuid"): cv.string,
        vol.Required("data"): cv.string,
        vol.Optional("entry_id"): cv.string,
    }
)

DIAGNOSE_GAME_START_SCHEMA = vol.Schema(
    {
        # experimental=true additionally A/B-tests the candidate GAME_START
        # write variants on the live board (a successful variant starts a game).
        vol.Optional("experimental", default=False): cv.boolean,
        vol.Optional("entry_id"): cv.string,
    }
)

PHANTOM_START_GAME_SCHEMA = vol.Schema(
    {
        vol.Optional("fen", default="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"): cv.string,
        vol.Optional("side", default="W"): vol.In(["W", "B"]),
        vol.Optional("timeout", default=60): vol.All(vol.Coerce(float), vol.Range(min=5, max=300)),
        vol.Optional("entry_id"): cv.string,
    }
)

PHANTOM_APPLY_AI_MOVE_SCHEMA = vol.Schema(
    {
        vol.Required("move"): cv.string,  # UCI notation, e.g. 'e7e5'
        vol.Optional("entry_id"): cv.string,
    }
)

EXECUTE_MOVE_SCHEMA = vol.Schema(
    {
        vol.Required("move"): cv.string,  # UCI notation, e.g. 'e2e4' or 'e7e8q' (promotion)
        vol.Optional("promotion"): vol.In(["q", "Q", "r", "R", "b", "B", "n", "N"]),
        vol.Optional("entry_id"): cv.string,
    }
)

START_AI_VS_AI_GAME_SCHEMA = vol.Schema(
    {
        vol.Optional("white_ai_level"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=8)
        ),
        vol.Optional("black_ai_level"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=8)
        ),
        vol.Optional("move_delay_seconds", default=1.5): vol.All(
            vol.Coerce(float), vol.Range(min=0.0, max=60.0)
        ),
        vol.Optional("entry_id"): cv.string,
    }
)


TAKEBACK_SCHEMA = vol.Schema(
    {
        vol.Optional("count", default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=20)
        ),
        vol.Optional("entry_id"): cv.string,
    }
)


# Generic arbitrary-movement: from any square to any other square, no chess legality
# check. Bypasses python-chess's is_capture (which has spurious-True bug for moves
# whose destination is the en-passant target square). Use for piece-relocation
# scripts, board-setup helpers, anything that needs to drive the magnet between
# two squares regardless of game rules.
MOVE_PIECE_SCHEMA = vol.Schema(
    {
        vol.Required("from_square"): vol.All(cv.string, vol.Length(min=2, max=2)),  # 'e2'
        vol.Required("to_square"):   vol.All(cv.string, vol.Length(min=2, max=2)),  # 'e4'
        vol.Optional("capture", default=False): cv.boolean,
        vol.Optional("piece", default="E"): cv.string,
        vol.Optional("entry_id"): cv.string,
    }
)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate older config entries to the current schema.

    v1 → v2 (2026-05-24):
        Pre-v0.3.0 installs that were set up via the manual-MAC path stored
        the unique_id and CONF_BLE_ADDRESS in whatever case the user typed
        (e.g. ``c8:C9:A3:F2:7C:0A``). HA's Bluetooth integration always
        delivers uppercase addresses, so _abort_if_unique_id_configured()
        kept failing on a case mismatch and the board was perpetually
        re-discovered. v2 canonicalizes both unique_id and CONF_BLE_ADDRESS
        to uppercase colon-separated. Also rewrites the entry title and
        CONF_DEVICE_NAME if they embedded the old mixed-case form.

    v2 → v3 (2026-05-24):
        The v2 migration normalized the CONFIG ENTRY but did NOT migrate
        the ENTITY REGISTRY or DEVICE REGISTRY, which carry their own
        identifier spaces. Entities are registered with unique_id =
        ``{ble_address}_{suffix}`` and devices with identifier
        ``(DOMAIN, ble_address)``. After v2 ran, the integration's next
        setup created NEW entities (with uppercase prefix) attached to a
        NEW device (with uppercase identifier), because HA couldn't match
        the (now-uppercase) unique_ids it was looking up against the
        (still-lowercase) entries that existed in the registry. Result:
        the user saw two devices for the same physical board and the live
        data went to the new entity_ids (with ``_2`` suffixes), breaking
        existing dashboards and automations.

        v3 consolidates: rewrites lowercase entity unique_ids to uppercase
        (preserving entity_id, area, customizations) and prefers the
        non-canonical (original) entry when both forms exist for the same
        suffix, deletes the duplicate canonical-form entries created in
        the broken state, updates the original device's identifier to
        uppercase, and removes the orphaned uppercase device.

    v3 → v4 (2026-05-24):
        The initial cut of v3 shipped with a priority bug in the
        "both forms exist" branch — it kept the auto-created uppercase
        entry (whose entity_id had been auto-suffixed by HA's
        collision-handling with ``_2``) and deleted the user's original
        clean entity_id. Users who applied that initial v3 ended up with
        a registry full of ``_2``-suffix entity_ids that don't match
        anything their dashboards reference. v4 renames any
        ``_2``-suffix entity_ids belonging to this config_entry back to
        their base form when the base is free. The v3 logic itself was
        also fixed in the same release so a fresh upgrader in the
        duplicate state would pick the cleaner entity_id; v3→v4 only
        cleans up installs that already passed through the broken v3.

    Migration is idempotent: entries already in canonical form pass through
    unchanged (still version-bumped so HA stops re-running the migration).
    """
    _LOGGER.debug(
        "Migrating Phantom Chess entry %s from v%s.%s",
        entry.entry_id, entry.version, entry.minor_version,
    )

    if entry.version == 1:
        stored_addr = entry.data.get(CONF_BLE_ADDRESS)
        normalized = _normalize_ble_address(stored_addr)
        if normalized is None:
            # Stored address isn't a valid MAC — can't safely migrate. Bump
            # the version anyway so we don't retry every reload; the user
            # will need to re-create the entry to use BLE discovery.
            _LOGGER.warning(
                "Phantom Chess entry %s has unparseable BLE address %r — "
                "version-bumping without normalization. Bluetooth "
                "auto-discovery will keep re-firing for this board until "
                "the integration is deleted and re-added.",
                entry.entry_id, stored_addr,
            )
            hass.config_entries.async_update_entry(entry, version=2)
            # Fall through to the v2→v3 step.
        else:
            new_data = {**entry.data, CONF_BLE_ADDRESS: normalized}

            # Refresh the device name + title if they embedded the old form.
            # Match either the stored address verbatim or any case variant.
            old_device_name = entry.data.get(CONF_DEVICE_NAME) or ""
            if stored_addr and stored_addr in old_device_name:
                new_data[CONF_DEVICE_NAME] = old_device_name.replace(stored_addr, normalized)
            elif old_device_name == f"Phantom {stored_addr}":
                new_data[CONF_DEVICE_NAME] = f"Phantom {normalized}"

            new_title = entry.title
            if stored_addr and stored_addr in entry.title:
                new_title = entry.title.replace(stored_addr, normalized)

            hass.config_entries.async_update_entry(
                entry,
                data=new_data,
                unique_id=normalized,
                title=new_title,
                version=2,
            )
            _LOGGER.info(
                "Phantom Chess entry %s migrated v1→v2: unique_id %r → %r",
                entry.entry_id, stored_addr, normalized,
            )
            # Fall through to the v2→v3 step.

    if entry.version == 2:
        canonical = entry.data.get(CONF_BLE_ADDRESS)
        if not canonical:
            _LOGGER.warning(
                "Phantom Chess entry %s has no CONF_BLE_ADDRESS — skipping "
                "v2→v3 registry consolidation; version-bumping.",
                entry.entry_id,
            )
            hass.config_entries.async_update_entry(entry, version=3)
            # Fall through to v3→v4.
        else:
            canonical_lower = canonical.lower()
            _consolidate_registries_to_canonical(hass, entry, canonical, canonical_lower)
            hass.config_entries.async_update_entry(entry, version=3)
            _LOGGER.info(
                "Phantom Chess entry %s migrated v2→v3: entity + device registry "
                "consolidated to canonical address %r",
                entry.entry_id, canonical,
            )
            # Fall through to v3→v4.

    if entry.version == 3:
        renamed = _rename_collision_suffix_entity_ids(hass, entry)
        hass.config_entries.async_update_entry(entry, version=4)
        _LOGGER.info(
            "Phantom Chess entry %s migrated v3→v4: renamed %d "
            "`_2`-suffix entity_id(s) back to base form",
            entry.entry_id, renamed,
        )
        return True

    # Unknown future version — refuse to load (HA logs this as setup failure).
    _LOGGER.error(
        "Phantom Chess entry %s is at unsupported schema version %s — "
        "downgrade is not supported. Delete and re-create the integration.",
        entry.entry_id, entry.version,
    )
    return False


def _consolidate_registries_to_canonical(
    hass: HomeAssistant,
    entry: ConfigEntry,
    canonical: str,
    canonical_lower: str,
) -> None:
    """v2→v3 helper: merge any duplicate-cased entity + device registry
    entries that belong to ``entry`` down to the canonical address form.

    Entity unique_id is ``{address}_{suffix}``; device identifier is
    ``(DOMAIN, address)``. We:

    1. Walk every entity belonging to this config_entry whose unique_id's
       address prefix case-insensitively matches ``canonical``. Group by
       suffix. For each suffix:
         - If a non-canonical entity exists and no canonical counterpart:
           rewrite its unique_id to the canonical form (preserves
           entity_id, name overrides, area, etc.).
         - If both canonical and non-canonical exist: keep the canonical
           entity; delete the non-canonical one(s).
       Any entity whose unique_id doesn't start with this entry's address
       (e.g. legacy fields, free-form unique_ids) is left untouched.

    2. Walk every device belonging to this config_entry whose identifier's
       address case-insensitively matches ``canonical``.
         - If a canonical-identifier device exists and a non-canonical one
           also exists: reassign any entities still attached to the
           non-canonical device over to the canonical device, then remove
           the non-canonical device.
         - If only non-canonical devices exist: rewrite the first one's
           identifier to canonical, remove any remaining duplicates.

    All operations are best-effort: any individual entity/device error is
    logged at debug and the loop continues. Migration always succeeds at
    the framework level so version is bumped reliably; the goal is to
    consolidate as much as possible without leaving the registries in a
    partially-broken state.
    """
    er_inst = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    # ── 1. Entity registry consolidation ────────────────────────────────
    # Snapshot the entries list because we mutate during iteration.
    our_entities = [
        e for e in list(er_inst.entities.values())
        if e.config_entry_id == entry.entry_id
    ]

    by_suffix: dict[str, dict[str, list]] = {}
    for e in our_entities:
        uid = e.unique_id or ""
        if "_" not in uid:
            continue
        addr_part, _, suffix = uid.partition("_")
        if addr_part.lower() != canonical_lower:
            continue
        bucket = by_suffix.setdefault(suffix, {"canonical": [], "noncanonical": []})
        if addr_part == canonical:
            bucket["canonical"].append(e)
        else:
            bucket["noncanonical"].append(e)

    entities_rewritten = 0
    entities_deleted = 0
    for suffix, bucket in by_suffix.items():
        canonicals = bucket["canonical"]
        noncanonicals = bucket["noncanonical"]
        if not noncanonicals and not canonicals:
            continue
        if not canonicals:
            # Only non-canonical present (typical fresh-from-v1 upgrade).
            # Promote first non-canonical to canonical; delete the rest.
            keeper = noncanonicals[0]
            new_uid = f"{canonical}_{suffix}"
            try:
                er_inst.async_update_entity(keeper.entity_id, new_unique_id=new_uid)
                entities_rewritten += 1
            except Exception as err:
                _LOGGER.debug(
                    "v3: failed to rewrite %s unique_id %r → %r: %s",
                    keeper.entity_id, keeper.unique_id, new_uid, err,
                )
            for dup in noncanonicals[1:]:
                try:
                    er_inst.async_remove(dup.entity_id)
                    entities_deleted += 1
                except Exception as err:
                    _LOGGER.debug("v3: failed to remove duplicate entity %s: %s",
                                  dup.entity_id, err)
        elif not noncanonicals:
            # Only canonical present — already clean, nothing to do.
            continue
        else:
            # BOTH present. The canonical-form entry is almost always the
            # auto-created duplicate from the broken-v2 boot (its entity_id
            # carries an HA-auto-suffix like `_2`), while the non-canonical
            # form is the user's ORIGINAL entity with the clean entity_id
            # their dashboards and automations reference. Prefer the
            # non-canonical entry: rewrite its unique_id to canonical,
            # delete the canonical entry. This preserves entity_id, name
            # overrides, area, customizations.
            keeper = noncanonicals[0]
            new_uid = f"{canonical}_{suffix}"
            try:
                er_inst.async_update_entity(keeper.entity_id, new_unique_id=new_uid)
                entities_rewritten += 1
            except Exception as err:
                _LOGGER.debug(
                    "v3: failed to rewrite %s unique_id %r → %r: %s",
                    keeper.entity_id, keeper.unique_id, new_uid, err,
                )
            for dup in canonicals + noncanonicals[1:]:
                try:
                    er_inst.async_remove(dup.entity_id)
                    entities_deleted += 1
                except Exception as err:
                    _LOGGER.debug("v3: failed to remove duplicate entity %s: %s",
                                  dup.entity_id, err)

    # ── 2. Device registry consolidation ────────────────────────────────
    our_devices = [
        d for d in list(dev_reg.devices.values())
        if entry.entry_id in d.config_entries
    ]

    canonical_devs: list = []
    noncanonical_devs: list = []
    for d in our_devices:
        for ident in d.identifiers:
            if len(ident) < 2 or ident[0] != DOMAIN:
                continue
            addr = ident[1]
            if addr.lower() != canonical_lower:
                continue
            if addr == canonical:
                canonical_devs.append(d)
            else:
                noncanonical_devs.append(d)
            break  # one matching identifier is enough

    devices_rewritten = 0
    devices_removed = 0
    if noncanonical_devs and not canonical_devs:
        # Promote the first non-canonical device's identifier.
        keeper = noncanonical_devs[0]
        new_identifiers = set()
        for ident in keeper.identifiers:
            if len(ident) >= 2 and ident[0] == DOMAIN and ident[1].lower() == canonical_lower:
                new_identifiers.add((DOMAIN, canonical))
            else:
                new_identifiers.add(tuple(ident))
        try:
            dev_reg.async_update_device(keeper.id, new_identifiers=new_identifiers)
            devices_rewritten += 1
        except Exception as err:
            _LOGGER.debug("v3: failed to rewrite device %s identifier: %s",
                          keeper.id, err)
        # Remove any further duplicates (rare).
        for dup in noncanonical_devs[1:]:
            try:
                dev_reg.async_remove_device(dup.id)
                devices_removed += 1
            except Exception as err:
                _LOGGER.debug("v3: failed to remove duplicate device %s: %s",
                              dup.id, err)
    elif canonical_devs and noncanonical_devs:
        # Canonical exists; reassign entities then remove non-canonicals.
        keeper = canonical_devs[0]
        for dup in noncanonical_devs:
            # Re-fetch our_entities post-rewrite so we see updated device_ids.
            for e in list(er_inst.entities.values()):
                if e.config_entry_id == entry.entry_id and e.device_id == dup.id:
                    try:
                        er_inst.async_update_entity(e.entity_id, device_id=keeper.id)
                    except Exception as err:
                        _LOGGER.debug(
                            "v3: failed to reassign %s to device %s: %s",
                            e.entity_id, keeper.id, err,
                        )
            try:
                dev_reg.async_remove_device(dup.id)
                devices_removed += 1
            except Exception as err:
                _LOGGER.debug("v3: failed to remove duplicate device %s: %s",
                              dup.id, err)

    _LOGGER.info(
        "v3: entity registry — rewrote %d unique_ids, removed %d duplicates. "
        "Device registry — rewrote %d identifiers, removed %d duplicate devices.",
        entities_rewritten, entities_deleted, devices_rewritten, devices_removed,
    )


def _rename_collision_suffix_entity_ids(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> int:
    """v3→v4 helper: rename any ``_2``-suffix entity_ids belonging to
    ``entry`` back to their base form (without the suffix), provided the
    base entity_id is free.

    The ``_2`` suffix is what HA appends automatically when the
    suggested_object_id for a new entity collides with an existing
    entity_id. The broken v3 migration deleted the user's original
    (clean) entity_id and kept the auto-suffixed duplicate, leaving
    every affected entity with a `_2` in its name. With the originals
    gone the base entity_id is now free, so we can safely rename.

    Returns the count of successful renames.
    """
    er_inst = er.async_get(hass)
    renamed = 0
    # Snapshot entries because async_update_entity mutates the registry.
    our_entities = [
        e for e in list(er_inst.entities.values())
        if e.config_entry_id == entry.entry_id
    ]
    for e in our_entities:
        eid = e.entity_id
        if not eid.endswith("_2"):
            continue
        base = eid[:-2]
        # Only rename if the base entity_id is free.
        if er_inst.async_get(base) is not None:
            _LOGGER.debug(
                "v4: not renaming %s → %s (base already exists)", eid, base,
            )
            continue
        try:
            er_inst.async_update_entity(eid, new_entity_id=base)
            renamed += 1
        except Exception as err:
            _LOGGER.debug("v4: failed to rename %s → %s: %s", eid, base, err)
    return renamed


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-level services and static paths.

    Bronze quality scale rule ``action-setup`` requires service actions
    to be registered here (not in ``async_setup_entry``), so they are
    discoverable before any config entry is loaded and survive an entry
    reload without needing re-registration.

    Service handlers find their target coordinator at call-time by
    enumerating ``hass.config_entries.async_entries(DOMAIN)`` and reading
    ``entry.runtime_data`` — see ``_get_coordinator``.
    """
    _register_services(hass)
    await _register_static_paths(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: PhantomChessConfigEntry
) -> bool:
    """Set up Phantom Chess Board from a config entry."""
    coordinator = PhantomChessCoordinator(hass, dict(entry.data), entry=entry)
    await coordinator.async_setup()

    # B3 lifecycle: register the coordinator shutdown as an on_unload hook
    # BEFORE forwarding to platforms. async_setup() has already started the
    # BLE loop (reconnect task) — if async_forward_entry_setups (or any step
    # below) raises, HA runs the on_unload callbacks for the failed setup, so
    # a failed setup can't leak the reconnect task + analysis client. On a
    # normal unload HA runs these callbacks only AFTER async_unload_entry
    # reports the platforms unloaded cleanly, giving the correct teardown
    # ordering (platforms first, coordinator last).
    entry.async_on_unload(coordinator.async_shutdown)

    # Pre-populate coordinator.data with the blank state so entities don't
    # crash on first read before the BLE loop calls async_set_updated_data.
    # B7: seed a snapshot copy, not the live mutable dict, so coordinator.data
    # doesn't alias _state and mutate underneath entity reads before the first
    # real fan-out.
    coordinator.async_set_updated_data(dict(coordinator._state))

    # Bronze quality scale rule `runtime-data` — store per-entry state on
    # entry.runtime_data, not hass.data[DOMAIN][entry.entry_id]. HA
    # garbage-collects this automatically on unload.
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # v0.4-alpha4: provision (or remove) the /phantom-chess dashboard based
    # on the per-entry toggle. Runs after platform setup so the entities the
    # dashboard references exist by the time the user opens the page.
    # Both branches are idempotent — safe to call on every reload, and a
    # user flipping the toggle in options gets an immediate effect via the
    # reload listener below.
    if entry.options.get(OPT_AUTO_PROVISION_DASHBOARD, DEFAULT_AUTO_PROVISION_DASHBOARD):
        try:
            await async_provision_dashboard(hass, entry)
        except Exception:
            # Provisioning is non-critical — never fail integration setup
            # because the dashboard couldn't be written.
            _LOGGER.exception(
                "Failed to provision Phantom Chess dashboard for entry %s",
                entry.entry_id,
            )
    else:
        # B6: only remove the dashboard when no *other* loaded entry still
        # wants it.  Without this check, a second board with
        # auto_provision=False would delete the dashboard provisioned by
        # the first board — even though the first board still wants it.
        other_wants_dashboard = any(
            e.options.get(OPT_AUTO_PROVISION_DASHBOARD, DEFAULT_AUTO_PROVISION_DASHBOARD)
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id
        )
        if not other_wants_dashboard:
            try:
                await async_unprovision_dashboard(hass)
            except Exception:
                _LOGGER.exception(
                    "Failed to unprovision Phantom Chess dashboard for entry %s",
                    entry.entry_id,
                )

    # Reload when options change so the auto_provision_dashboard toggle takes
    # effect without a HA restart. async_on_unload auto-removes the listener
    # when the entry is unloaded, so we don't re-register on every reload.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-load the config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# Marker key for static-path registration. Stored under a SEPARATE
# hass.data slot — not under hass.data[DOMAIN] — because hass.data[DOMAIN]
# is the entry_id → coordinator map used by _get_coordinator's routing
# logic. Polluting that dict caused multi-board routing to count the
# marker string as a second "board" and reject every service call
# (Task #27 hot-fix, 2026-05-17).
STATIC_PATH_MARKER = f"{DOMAIN}_static_paths_registered"


async def _register_static_paths(hass: HomeAssistant) -> None:
    """Expose the integration's www/ directory at /phantom_chess_static/.

    Used to ship the interactive chessboard HTML page (board.html) without
    requiring the user to copy files into /config/www/. Idempotent — second
    setup_entry calls are no-ops via STATIC_PATH_MARKER.
    """
    if hass.data.get(STATIC_PATH_MARKER):
        return
    try:
        from homeassistant.components.http import StaticPathConfig
        import os
        www_dir = os.path.join(os.path.dirname(__file__), "www")
        if not os.path.isdir(www_dir):
            _LOGGER.debug(
                "Phantom Chess: www/ directory missing at %s — skipping static path registration",
                www_dir,
            )
            return
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    url_path="/phantom_chess_static",
                    path=www_dir,
                    cache_headers=False,
                )
            ]
        )
        hass.data[STATIC_PATH_MARKER] = True
        _LOGGER.info(
            "Phantom Chess: interactive board served at /phantom_chess_static/board.html"
        )
    except RuntimeError as err:
        # HA raises if path is already registered. Idempotent — mark and move on.
        _LOGGER.debug("Static path registration skipped: %s", err)
        hass.data[STATIC_PATH_MARKER] = True
    except Exception as err:
        _LOGGER.warning("Failed to register Phantom Chess static path: %s", err)


async def async_unload_entry(
    hass: HomeAssistant, entry: PhantomChessConfigEntry
) -> bool:
    """Unload a config entry.

    Services are registered in ``async_setup`` (not here). B3: unload the
    platforms FIRST and report their result to HA. The coordinator shutdown
    is registered via ``entry.async_on_unload`` in ``async_setup_entry``, so
    HA runs it only when this returns True — i.e. the coordinator is torn
    down only after the platforms actually unloaded, never leaving a loaded
    entry with a dead coordinator. HA garbage-collects ``entry.runtime_data``
    automatically after this function returns.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(
    hass: HomeAssistant, entry: PhantomChessConfigEntry
) -> None:
    """Tear down per-entry artifacts the user shouldn't keep after uninstall.

    Called by HA when the user actively deletes the integration (not on
    unload/reload). v0.4-alpha4: removes the auto-provisioned
    /phantom-chess dashboard so a clean reinstall starts fresh.

    The dashboard is shared across all configured boards (single
    /phantom-chess url_path), so we only remove it when the last entry is
    going away.
    """
    # async_remove_entry runs AFTER async_unload_entry, so the entry being
    # removed is no longer in async_entries(). If the user has multiple
    # boards and is deleting just one, we keep the dashboard for the
    # surviving entry/entries. v1 design: the dashboard is shared across
    # all boards.
    remaining_entries = hass.config_entries.async_entries(DOMAIN)
    if remaining_entries:
        return
    if entry.options.get(OPT_AUTO_PROVISION_DASHBOARD, DEFAULT_AUTO_PROVISION_DASHBOARD):
        try:
            await async_unprovision_dashboard(hass)
        except Exception:
            _LOGGER.exception(
                "Failed to unprovision Phantom Chess dashboard for entry %s",
                entry.entry_id,
            )


def _register_services(hass: HomeAssistant) -> None:
    """Register domain-level services.

    Idempotent — guarded against repeat invocation via has_service.
    Called from async_setup so the services are available before any
    config entry is loaded (Bronze quality scale rule `action-setup`).
    """

    if hass.services.has_service(DOMAIN, SERVICE_EXECUTE_MOVE):
        # Services already registered (defensive — in normal flow
        # async_setup runs exactly once per HA process).
        return

    def _get_coordinator(call: ServiceCall) -> PhantomChessCoordinator:
        """Resolve which coordinator a service call targets.

        Routing rules (Task #18, 2026-05-16 release-readiness):
          - Zero boards configured → error.
          - Exactly one board → use it; entry_id is optional and only
            validated if provided.
          - Two or more boards → entry_id REQUIRED. Without it we'd
            silently mutate the first-listed board which is dangerous
            (previously the integration's behavior, called out as a
            BLOCKER by the public-release audit).

        entry_id is the ConfigEntry's `entry_id` attribute, which the user
        can find in Settings → Devices & Services → Phantom Chess → ⋮ → ID,
        or via the entity registry.
        """
        # alpha10: enumerate loaded entries via the config-entries API and
        # read entry.runtime_data (the Bronze `runtime-data` pattern).
        # Pre-alpha10 this read from hass.data[DOMAIN][entry_id].
        # alpha11: replaced vol.Invalid (a voluptuous validation error
        # that HA surfaces as a generic "unknown" failure) with
        # ServiceValidationError — Silver quality scale rule
        # `action-exceptions`. ServiceValidationError is the documented
        # type for "user-input is wrong" or "referenced thing doesn't
        # exist". HA shows the message verbatim in the UI.
        coordinators: dict[str, PhantomChessCoordinator] = {
            e.entry_id: e.runtime_data
            for e in hass.config_entries.async_entries(DOMAIN)
            if getattr(e, "runtime_data", None) is not None
        }
        if not coordinators:
            # Gold quality scale rule `exception-translations`: use
            # translation_domain + translation_key so the message is
            # locale-friendly. Falls back to the existing English
            # string if no translation is registered.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_board_configured",
            )

        target_id = call.data.get("entry_id")
        if target_id:
            if target_id not in coordinators:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_entry_id",
                    translation_placeholders={
                        "entry_id": str(target_id),
                        "known": ", ".join(coordinators),
                    },
                )
            return coordinators[target_id]

        # No entry_id supplied — only safe if exactly one board exists.
        if len(coordinators) == 1:
            return next(iter(coordinators.values()))
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="ambiguous_target_entry",
            translation_placeholders={
                "count": str(len(coordinators)),
                "known": ", ".join(coordinators),
            },
        )

    async def handle_start_game(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        if ai_level := call.data.get("ai_level"):
            coordinator.ai_level = ai_level
        if color := call.data.get("color"):
            coordinator.player_color = color
        # Optional clock overrides forwarded to async_start_game.
        kwargs = {}
        if (clock_limit := call.data.get("clock_limit_seconds")) is not None:
            kwargs["clock_limit_seconds"] = clock_limit
        if (clock_inc := call.data.get("clock_increment_seconds")) is not None:
            kwargs["clock_increment_seconds"] = clock_inc
        await coordinator.async_start_game(**kwargs)

    async def handle_send_move(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_send_move(call.data["move"])

    async def handle_resign(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_resign()

    async def handle_takeback(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        count = int(call.data.get("count", 1))
        await coordinator.async_takeback(count=count)

    async def handle_start_local_game(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        if color := call.data.get("color"):
            coordinator.player_color = color
        await coordinator.async_start_local_game()

    async def handle_stop_local_game(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_stop_local_game()

    async def handle_start_two_player_game(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_start_two_player_game()

    async def handle_resync_two_player(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_resync_two_player()

    async def handle_start_ai_vs_ai_game(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        # alpha30: fall back to the integration-owned spectator-mode
        # sliders (`number.*_white_ai_level`, `_black_ai_level`,
        # `_ai_vs_ai_move_delay`) when the caller omits args. The
        # dashboard's 5th mode tile passes explicit args anyway, but
        # Developer-Tools yaml + automations benefit from
        # default-from-state behavior.
        await coordinator.async_start_ai_vs_ai_game(
            white_ai_level=call.data.get(
                "white_ai_level", coordinator.white_ai_level
            ),
            black_ai_level=call.data.get(
                "black_ai_level", coordinator.black_ai_level
            ),
            move_delay_seconds=call.data.get(
                "move_delay_seconds", coordinator.ai_vs_ai_move_delay
            ),
        )

    async def handle_back_to_modes(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_back_to_modes()

    async def handle_resync_detection(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_resync_detection()

    async def handle_start_lichess_configured(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_start_lichess_configured()

    async def handle_play_selected_sculpture(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_play_selected_sculpture()

    async def handle_debug_ble_write(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_debug_ble_write(call.data["uuid"], call.data["data"])

    async def handle_diagnose_game_start(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_diagnose_game_start(
            experimental=call.data.get("experimental", False)
        )

    async def handle_phantom_start_game(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_phantom_start_game(
            fen=call.data.get("fen"),
            side=call.data.get("side", "W"),
            wait_for_running_timeout_s=call.data.get("timeout", 60),
        )

    async def handle_phantom_apply_ai_move(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_phantom_apply_ai_move(call.data["move"])

    async def handle_move_piece(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_move_piece(
            from_square=call.data["from_square"].lower(),
            to_square=call.data["to_square"].lower(),
            capture=call.data.get("capture", False),
            piece=call.data.get("piece", "E"),
        )

    async def handle_start_sculpture(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_start_sculpture()

    async def handle_reset_position(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_reset_position()

    async def handle_play_sound(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_play_sound(call.data.get("sound", "check"))

    async def handle_request_hint(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_request_hint()

    async def handle_dismiss_review(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_dismiss_review()

    async def handle_reconcile_lichess_state(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_reconcile_lichess_state()

    async def handle_resume_from_phone(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_resume_from_phone()

    async def handle_execute_move(call: ServiceCall) -> None:
        coordinator = _get_coordinator(call)
        await coordinator.async_execute_dashboard_move(
            uci=call.data["move"],
            promotion=call.data.get("promotion"),
        )

    hass.services.async_register(
        DOMAIN, SERVICE_START_GAME, handle_start_game, schema=START_GAME_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_START_LOCAL_GAME, handle_start_local_game,
        schema=vol.Schema({
            vol.Optional("color"): vol.In(["white", "black", "random"]),
            vol.Optional("entry_id"): cv.string,
        }),
    )
    hass.services.async_register(DOMAIN, SERVICE_STOP_LOCAL_GAME, handle_stop_local_game)
    hass.services.async_register(
        DOMAIN, SERVICE_START_AI_VS_AI_GAME, handle_start_ai_vs_ai_game,
        schema=START_AI_VS_AI_GAME_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_START_TWO_PLAYER_GAME, handle_start_two_player_game
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESYNC_TWO_PLAYER, handle_resync_two_player
    )
    # v0.4-alpha3 service wrappers — replace user-side scripts.
    hass.services.async_register(DOMAIN, SERVICE_BACK_TO_MODES, handle_back_to_modes)
    hass.services.async_register(
        DOMAIN, SERVICE_RESYNC_DETECTION, handle_resync_detection
    )
    hass.services.async_register(
        DOMAIN, SERVICE_START_LICHESS_CONFIGURED, handle_start_lichess_configured,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PLAY_SELECTED_SCULPTURE, handle_play_selected_sculpture,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_MOVE, handle_send_move, schema=SEND_MOVE_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_RESIGN, handle_resign)
    hass.services.async_register(
        DOMAIN, SERVICE_TAKEBACK, handle_takeback, schema=TAKEBACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DEBUG_BLE_WRITE, handle_debug_ble_write,
        schema=DEBUG_BLE_WRITE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DIAGNOSE_GAME_START, handle_diagnose_game_start,
        schema=DIAGNOSE_GAME_START_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PHANTOM_START_GAME, handle_phantom_start_game,
        schema=PHANTOM_START_GAME_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PHANTOM_APPLY_AI_MOVE, handle_phantom_apply_ai_move,
        schema=PHANTOM_APPLY_AI_MOVE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_MOVE_PIECE, handle_move_piece,
        schema=MOVE_PIECE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_START_SCULPTURE, handle_start_sculpture,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESET_POSITION, handle_reset_position,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PLAY_SOUND, handle_play_sound,
        schema=vol.Schema({
            vol.Optional("sound", default="check"): vol.In(["check", "checkmate"]),
            vol.Optional("entry_id"): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REQUEST_HINT, handle_request_hint,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DISMISS_REVIEW, handle_dismiss_review,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RECONCILE_LICHESS_STATE, handle_reconcile_lichess_state,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESUME_FROM_PHONE, handle_resume_from_phone,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EXECUTE_MOVE, handle_execute_move,
        schema=EXECUTE_MOVE_SCHEMA,
    )

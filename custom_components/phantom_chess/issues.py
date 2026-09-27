"""Repair issues (not a repairs platform — no fix flows) surfaced in Settings → Repairs.

Each issue here corresponds to a condition a user can act on and that the
integration can observe directly, so it can also clear the issue once the
condition is gone:

- ``engine_unsupported_platform``: no verified Stockfish package exists for
  this host (libc/architecture). Local play and review are unavailable.
- ``engine_failed``: the verified engine could not be installed or started.
  Cleared when the engine next reports ready (e.g. after Board & settings →
  Check chess engine).
- ``ble_route_unsupported``: the board rejected a game-start write with ATT
  0x0D. On firmware 0.3.2+ this is the known failure of the host Bluetooth
  (BlueZ) route; an ESPHome Bluetooth proxy is the supported fix. Cleared by
  the next successful game-start write.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

ISSUE_ENGINE_UNSUPPORTED = "engine_unsupported_platform"
ISSUE_ENGINE_FAILED = "engine_failed"
ISSUE_BLE_ROUTE = "ble_route_unsupported"
# Raised by 0.4.x when HACS card plugins were missing. 0.5 bundles its own
# card, so any lingering copy is stale and removed at setup.
LEGACY_ISSUES = ("missing_frontend_deps",)

DOCS_URL = "https://github.com/luketadams/phantom-chess-ha#troubleshooting"


def _create(
    hass: HomeAssistant,
    issue_id: str,
    placeholders: dict[str, str],
    *,
    translation_key: str | None = None,
    severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=severity,
        translation_key=translation_key or issue_id,
        translation_placeholders=placeholders,
        learn_more_url=DOCS_URL,
    )


def sync_engine_issue(hass: HomeAssistant, state: dict) -> None:
    """Mirror the engine health state into Repairs.

    Only terminal states change issues: ``unavailable`` and ``error`` raise
    the matching issue, ``ready`` clears both. Transitional states
    (downloading, verifying, not_checked) leave issues as they are so a
    retry in progress does not flap the Repairs list.
    """
    status = state.get("status")
    detail = str(state.get("error") or "")
    if status == "unavailable":
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ENGINE_FAILED)
        _create(hass, ISSUE_ENGINE_UNSUPPORTED, {"detail": detail or "unknown platform"})
    elif status == "error":
        _create(hass, ISSUE_ENGINE_FAILED, {"detail": detail or "unknown error"})
    elif status == "ready":
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ENGINE_UNSUPPORTED)
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ENGINE_FAILED)


def _ble_issue_id(address: str) -> str:
    return f"{ISSUE_BLE_ROUTE}_{address.replace(':', '').lower()}"


def raise_ble_route_issue(hass: HomeAssistant, address: str, firmware: str | None) -> None:
    _create(
        hass,
        _ble_issue_id(address),
        {"address": address, "firmware": firmware or "unknown"},
        translation_key=ISSUE_BLE_ROUTE,
        severity=ir.IssueSeverity.ERROR,
    )


def clear_ble_route_issue(hass: HomeAssistant, address: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, _ble_issue_id(address))


def clear_legacy_issues(hass: HomeAssistant) -> None:
    for issue_id in LEGACY_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue_id)

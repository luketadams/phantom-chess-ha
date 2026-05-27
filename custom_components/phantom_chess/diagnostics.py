"""Diagnostics support for Phantom Chess Board.

Exposes a one-click "Download diagnostics" button in the integration's
device page. The downloaded JSON is suitable for attaching to GitHub
issues — sensitive fields (Lichess token, partial MAC, username) are
automatically redacted.

Added 2026-05-16 (release-readiness Task #22).
"""
from __future__ import annotations

import platform
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NAME,
    CONF_LICHESS_TOKEN,
    CONF_LICHESS_USER,
    DOMAIN,
)
from .coordinator import PhantomChessCoordinator

# Sensitive keys that are masked when serialized.
# CONF_LICHESS_TOKEN is fully redacted; CONF_BLE_ADDRESS and
# CONF_LICHESS_USER are partially redacted in _mask_*() helpers below
# so the diagnostic stays useful for debugging (you can correlate
# multiple reports by the masked prefix).
TO_REDACT = {CONF_LICHESS_TOKEN}


def _mask_ble_address(address: str | None) -> str | None:
    """Mask the middle bytes of a BLE MAC address.

    "C8:C9:A3:F2:7C:0A" → "C8:**:**:**:**:0A"

    Keeps the first and last bytes for cross-report correlation while
    hiding the device-uniquely-identifying middle bytes.
    """
    if not address:
        return address
    parts = address.split(":")
    if len(parts) != 6:
        return "[malformed]"
    return f"{parts[0]}:**:**:**:**:{parts[5]}"


def _mask_username(username: str | None) -> str | None:
    """Show first 2 chars + last char of a Lichess username, mask middle.

    "lukeadams" → "lu*****s"
    """
    if not username or len(username) < 4:
        return "[redacted]"
    return f"{username[:2]}{'*' * (len(username) - 3)}{username[-1]}"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Build a redacted diagnostics dump for the given config entry."""
    coordinator: PhantomChessCoordinator | None = getattr(
        entry, "runtime_data", None
    )

    # ── Entry-level data (mostly already redacted via TO_REDACT) ──────────
    entry_payload = async_redact_data(dict(entry.data), TO_REDACT)
    entry_payload[CONF_BLE_ADDRESS] = _mask_ble_address(
        entry.data.get(CONF_BLE_ADDRESS)
    )
    entry_payload[CONF_LICHESS_USER] = _mask_username(
        entry.data.get(CONF_LICHESS_USER)
    )

    # ── Coordinator runtime state ─────────────────────────────────────────
    if coordinator is None:
        runtime: dict[str, Any] = {"loaded": False}
    else:
        state = coordinator.data or {}
        # Pull only the keys useful for debugging. Skip large/noisy fields
        # (move_history_moves, piece_grid, sensor_bitmap) — they're available
        # in the matrix_log debug dump if needed and would bloat this file.
        keys_of_interest = (
            "firmware_version",
            "firmware_mode",
            "firmware_mode_last_updated",
            "game_status",
            "lichess_active",
            "local_game_active",
            "lichess_review_ready",
            "matrix_status",
            "matrix_status_message",
            "matrix_mismatches",
            "piece_count",
            "eval_cp",
            "eval_mate",
            "eval_source",
            "eval_depth",
            "opening_name",
            "opening_eco",
            "last_move",
            "last_move_classification",
            "last_move_cpl",
            "last_game_result",
            "last_game_accuracy_white",
            "last_game_accuracy_black",
            "battery_level",
        )
        runtime = {
            "loaded": True,
            "ble_connected": coordinator.is_ble_connected,
            "ai_level": getattr(coordinator, "ai_level", None),
            "player_color": getattr(coordinator, "player_color", None),
            "paused": getattr(coordinator, "paused", None),
            "session_initialized": getattr(coordinator, "_phantom_session_initialized", None),
            "lichess_game_id_set": coordinator._game_id is not None,
            "state": {k: state.get(k) for k in keys_of_interest},
        }

    # ── Architecture + environment (for Stockfish download triage) ────────
    import os
    env = {
        "platform_machine": platform.machine(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "python_version": platform.python_version(),
        "musl_linker_present": any(
            os.path.exists(p)
            for p in (
                "/lib/ld-musl-x86_64.so.1",
                "/lib/ld-musl-aarch64.so.1",
                "/lib/ld-musl-armhf.so.1",
            )
        ),
    }

    # ── Stockfish state ───────────────────────────────────────────────────
    stockfish: dict[str, Any] = {"client_initialized": False}
    if coordinator is not None and getattr(coordinator, "_analysis_client", None) is not None:
        sf = coordinator._analysis_client._stockfish
        if sf is not None:
            stockfish = {
                "client_initialized": True,
                "available": sf._available,
                "binary_path": str(sf.binary_path) if sf.binary_path else None,
                "binary_path_exists": (
                    sf.binary_path is not None and os.path.exists(sf.binary_path)
                ),
                "engine_running": sf._engine is not None,
                "unsupported_arch_warned": sf._unsupported_arch_warned,
                "bin_dir": str(sf.bin_dir),
            }

    return {
        "integration": {
            "domain": DOMAIN,
            "version": "0.2.0",  # mirror manifest.version
            "entry_id": entry.entry_id,
            "entry_title": entry.title,
            "entry_options": dict(entry.options or {}),
        },
        "entry_data": entry_payload,
        "runtime": runtime,
        "environment": env,
        "stockfish": stockfish,
    }

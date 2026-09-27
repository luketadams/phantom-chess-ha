"""Shared runtime pieces for the coordinator and its session modules.

Module-level helpers that used to live at the top of coordinator.py. Code
calls ``rt._sleep`` and ``rt.async_get_clientsession`` through this module so
tests patch one place.
"""
from __future__ import annotations

import asyncio
import re

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import MOVE_PREFIX

# Names the coordinator and its session modules import from here.
__all__ = [
    "AI_VS_AI_TWO_STEP_SETTLE_S",
    "BleakClient",
    "BleakError",
    "BleakGATTCharacteristic",
    "_build_matrix_from_fen_module",
    "_check_consistency",
    "_diff_grid_vs_sensor",
    "_format_mismatch_instructions",
    "_grid_to_fen",
    "_is_move_frame",
    "_parse_matrix_notification",
    "_phantom_to_uci",
    "_rotate_uci_180",
    "_sleep",
    "async_get_clientsession",
]

# Module-level sleep indirection so tests can monkeypatch coordinator sleeps
# without patching the stdlib asyncio module process-wide (C8b).
_sleep = asyncio.sleep

# AI-vs-AI: minimum inter-move settle (seconds) AFTER a two-step physical
# move (capture or castle). The board reports BLE_MOVE_DONE on CLEAN: Match,
# which only validates the 8x8 playing area — the captured piece (or the
# rook) may still be moving to its square. A short gap fires the next
# snapshot into the moving magnet and the board wedges. Proven on hardware:
# a ~3s gap clears captures that froze at 0.5s. See [[phantom-chess-aivai-capture-rootcause]].
AI_VS_AI_TWO_STEP_SETTLE_S: float = 3.0

# Lazy import bleak — HA installs it as part of the bluetooth stack
try:
    from bleak import BleakClient
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.exc import BleakError
except ImportError:
    BleakClient = None  # type: ignore[assignment,misc]
    BleakError = Exception  # type: ignore[assignment,misc]
    # Only used in (lazy) annotations; defined so the session modules'
    # ``from .runtime import`` works without bleak (minimal test env).
    BleakGATTCharacteristic = None  # type: ignore[assignment,misc]


def _phantom_to_uci(move_str: str) -> str:
    """Convert Phantom move notation to UCI.

    "M 1 e2-e4" → "e2e4"        (firmware 0.1.6 / 0.3.0 white)
    "M 2 e7-e5" → "e7e5"        (firmware 0.3.0 black)
    "M 1 d5xe4" → "d5e4"        (capture)
    "M 1 e1-g1" → "e1g1"        (castling — king target square is enough)
    "M e2-e4"   → "e2e4"        (older firmware variant without index)
    """
    # Robust parser: extract the first '<sq>[-x]<sq>' token from the string.
    # This avoids relying on an exact "M N " prefix and tolerates both indexed
    # ("M 1 e2-e4") and unindexed ("M e2-e4") variants.
    m = re.search(r"([a-h][1-8])[-x]([a-h][1-8])", move_str)
    if m:
        return m.group(1) + m.group(2)
    # Fallback to legacy behaviour
    stripped = move_str.removeprefix(MOVE_PREFIX)
    return re.sub(r"[-x]", "", stripped)


def _rotate_uci_180(uci: str) -> str:
    """Apply a 180° rotation (rank-mirror + from-to swap) to a UCI move.

    Firmware 0.3.0 reports black-piece sensor events with this exact transform
    applied. To recover the actual move from the firmware's report, apply the
    same transform again — the operation is an involution.

      'e7e5' (actual black move) → 'e2e4' (rank-mirror) → 'e4e2' (from-to-swap)
                                                         ^ firmware emits this
      'e4e2' (decode firmware → actual) → 'e5e7' → 'e7e5'

    Validated 2026-05-10:
      - Luke played e7→e5 physically; firmware emitted "M 1 e4-e2".
      - rotate_180("e4e2") == "e7e5" ✓
      - White moves are reported without the transform.

    Used by the discovery-callback path to pick the right interpretation by
    legality-checking both candidates against the current python-chess board.
    """
    if len(uci) < 4:
        return uci
    f_file, f_rank, t_file, t_rank = uci[0], uci[1], uci[2], uci[3]
    promotion = uci[4:] if len(uci) > 4 else ""
    try:
        f_rank_m = str(9 - int(f_rank))
        t_rank_m = str(9 - int(t_rank))
    except ValueError:
        return uci
    # Mirror ranks and swap from-to in one shot.
    return f"{t_file}{t_rank_m}{f_file}{f_rank_m}{promotion}"


def _is_move_frame(payload_str: str) -> bool:
    """True if an opcode-stripped game-channel payload is a physical-move
    notification the discovery callback would try to apply.

    Recognizes the firmware-0.3.0 forms ``"M <n> <from>-<to>"`` / ``"SQ ..."``
    and a bare ``<sq><sep><sq>`` square-pair. Extracted (audit M4) so the
    heartbeat/status ``last_seen`` dedup gate and the move-apply branch share a
    single definition of "is a move" and can never disagree — a repeated move
    frame must reach the apply path (where the ~400 ms double-fire window, M2,
    owns move de-duplication) rather than being silently swallowed as a
    duplicate heartbeat.
    """
    return (
        payload_str.startswith("M ")
        or payload_str.startswith("SQ ")
        or (
            len(payload_str) >= 4
            and payload_str[0] in "abcdefgh"
            and payload_str[1] in "12345678"
            and payload_str[2] in "abcdefgh-x"
            and payload_str[3] in "abcdefgh12345678"
        )
    )


# ── Matrix-state notification parsing (UUID_SEND_MATRIX, firmware 0.3.0) ──────
# The board emits notifications on 1b034927 in the form:
#   "CLEAN: Match.,<100-char piece grid>,<100-char binary bitmap>"
# - Piece grid: 10×10, '.' = empty, uppercase = white piece (P/N/B/R/Q/K),
#   lowercase = black. The wire layout is COLUMN-MAJOR (each consecutive
#   10-char block is one file column, not a rank row) — the firmware matrix
#   is 90°-rotated relative to a human board. Index 0/9 rows+cols are the
#   gutter/graveyard border; the inner 8×8 is the playing area. See the
#   authoritative encode/decode (`grid_index_to_square`, `grid_to_fen`,
#   `build_matrix_from_fen`) in matrix.py and XOUXOU_PROTOCOL.md.
# - Bitmap: 10×10 of '0'/'1' representing raw hall-effect sensor state.
# Source: live capture 2026-05-09 + setupBoard asm; orientation reconfirmed
# 2026-06-09 against matrix.py + XOUXOU_PROTOCOL.


# Matrix parsing, FEN conversion, and mismatch diff helpers extracted to
# `matrix.py` as the first step of the Task #21 coordinator split
# (2026-05-16). These functions are pure (stateless) — they never needed
# to be on the coordinator class. Imported with underscore aliases to
# preserve every existing call-site verbatim; no behavior change.
from .matrix import (  # noqa: E402 — intentional late import, kept beside the extraction-doc comment above
    build_matrix_from_fen as _build_matrix_from_fen_module,
    check_consistency as _check_consistency,
    diff_grid_vs_sensor as _diff_grid_vs_sensor,
    format_mismatch_instructions as _format_mismatch_instructions,
    grid_to_fen as _grid_to_fen,
    parse_matrix_notification as _parse_matrix_notification,
)

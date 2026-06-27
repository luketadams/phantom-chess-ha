"""Wedge-recovery service `async_resync_detection` (v0.4-beta3, finding C1).

Re-seeds the firmware's expected matrix to the integration's current board
position via RESET_DETECTION (opcode 14) — the recovery for the
"Snapping Pieces / matrix do not match" wedge. It must:
  - refuse when the board isn't connected (raise),
  - send the current board-only FEN (start position at idle) and NOT drive
    any pieces (no GAME_START / execute_position),
  - best-effort dismiss the mismatch notifications.

Bound to a lightweight stub like the other coordinator method tests; runs
in the minimal CI env (no HA, no board).
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import chess
import pytest

from custom_components.phantom_chess.coordinator import PhantomChessCoordinator


def _stub(connected: bool = True) -> types.SimpleNamespace:
    stub = types.SimpleNamespace()
    stub._ble_connected = connected
    stub._board = chess.Board()  # idle = standard start position
    stub._phantom_send_reset_detection = AsyncMock()
    stub._announce_via_tts = MagicMock(return_value=None)
    stub.hass = types.SimpleNamespace(
        services=types.SimpleNamespace(async_call=AsyncMock()),
        async_create_task=MagicMock(),
    )
    return stub


def test_resync_detection_seeds_current_fen_without_driving() -> None:
    stub = _stub(connected=True)
    asyncio.run(PhantomChessCoordinator.async_resync_detection(stub))

    # RESET_DETECTION sent once with the current board-only FEN (start pos).
    stub._phantom_send_reset_detection.assert_awaited_once()
    sent_fen = stub._phantom_send_reset_detection.call_args.args[0]
    assert sent_fen == chess.Board().board_fen()
    # Best-effort notification dismissals were attempted.
    assert stub.hass.services.async_call.await_count >= 1


def test_resync_detection_requires_connection() -> None:
    stub = _stub(connected=False)
    with pytest.raises(RuntimeError):
        asyncio.run(PhantomChessCoordinator.async_resync_detection(stub))
    stub._phantom_send_reset_detection.assert_not_called()

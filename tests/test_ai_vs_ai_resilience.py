"""Spectator playback stops on ambiguous movement; reconnect discovery remains available."""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import chess
import pytest

import custom_components.phantom_chess.coordinator as coord_mod
from custom_components.phantom_chess.coordinator import PhantomChessCoordinator


class _FakeLoop:
    """Monotonic clock for hass.loop.time(); advances 0.5s per call."""

    def __init__(self) -> None:
        self._t = 0.0

    def time(self) -> float:
        self._t += 0.5
        return self._t


class _FakeHass:
    def __init__(self) -> None:
        self.loop = _FakeLoop()


def _make_stub() -> types.SimpleNamespace:
    """A stub carrying just the attributes the two loop methods touch."""
    stub = types.SimpleNamespace()
    stub.hass = _FakeHass()
    stub._board = chess.Board()
    stub._ai_vs_ai_active = True
    stub._ai_vs_ai_white_level = 3
    stub._ai_vs_ai_black_level = 3
    stub._ai_vs_ai_move_delay = 0.0
    stub._our_color = chess.WHITE
    stub._local_game_active = True
    stub._state = {}
    stub._ble_connected = True
    stub.ai_level = 3
    stub.async_set_updated_data = lambda *a, **k: None
    # methods the loop calls that we don't exercise here
    stub._record_and_analyze_local_move = lambda *a, **k: None
    stub._build_post_game_review = AsyncMock()

    def _create_task(coro, **k):
        # The loop fires _build_post_game_review via async_create_task at the
        # end; close the coro so we don't leak an un-awaited warning.
        try:
            coro.close()
        except Exception:
            pass
        return None

    stub.hass.async_create_task = _create_task

    # Bind the real methods under test.
    stub._ai_vs_ai_loop = types.MethodType(
        PhantomChessCoordinator._ai_vs_ai_loop, stub
    )
    stub._ai_vs_ai_await_reconnect = types.MethodType(
        PhantomChessCoordinator._ai_vs_ai_await_reconnect, stub
    )
    return stub


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(coord_mod.rt, "_sleep", _instant)


def _legal_uci(board: chess.Board) -> str:
    return next(iter(board.legal_moves)).uci()


@pytest.mark.asyncio
async def test_transport_failure_stops_without_replaying_motion(monkeypatch):
    """An ambiguous command stops playback before any further physical write."""
    stub = _make_stub()
    stub._notify_wedge_circuit_breaker = MagicMock()
    stub._get_ai_move = AsyncMock(return_value="e2e4")
    stub.async_phantom_apply_ai_move = AsyncMock(side_effect=RuntimeError("BLE dropped"))
    stub._phantom_execute_position = AsyncMock()
    monkeypatch.setattr(coord_mod.rt, "_sleep", AsyncMock())
    await stub._ai_vs_ai_loop()
    stub.async_phantom_apply_ai_move.assert_awaited_once_with("e2e4")
    stub._phantom_execute_position.assert_not_awaited()
    stub._notify_wedge_circuit_breaker.assert_called_once()
    assert len(stub._board.move_stack) == 0


@pytest.mark.asyncio
async def test_no_reconnect_stops_loop_gracefully():
    """If the board never comes back, the loop halts rather than spinning."""
    stub = _make_stub()
    # Tight timeout via the fake clock: time() advances 0.5s/call, deadline
    # 30s → ~60 polls then returns False. Keep it bounded with wait_for.

    async def _compute(_board):
        return _legal_uci(stub._board)

    stub._get_ai_move = AsyncMock(side_effect=_compute)

    async def _apply(uci):
        stub._board.push(chess.Move.from_uci(uci))
        stub._ble_connected = False
        raise RuntimeError("BLE not connected")

    stub.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    stub._phantom_execute_position = AsyncMock(return_value=True)

    await asyncio.wait_for(stub._ai_vs_ai_loop(), timeout=5.0)

    # Never reconnected → await-reconnect timed out → loop broke without
    # ever re-driving.
    assert stub._phantom_execute_position.await_count == 0
    assert stub._ai_vs_ai_active is False


@pytest.mark.asyncio
async def test_await_reconnect_returns_true_when_link_restored():
    """_ai_vs_ai_await_reconnect returns True once _ble_connected flips on."""
    stub = _make_stub()
    stub._ble_connected = False

    calls = {"n": 0}

    async def _flip(_seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            stub._ble_connected = True
        return None

    # Override the no-op sleep with one that flips the link after a couple polls.
    orig = coord_mod.rt._sleep
    coord_mod.rt._sleep = _flip
    try:
        result = await stub._ai_vs_ai_await_reconnect(timeout=30.0)
    finally:
        coord_mod.rt._sleep = orig
    assert result is True


@pytest.mark.asyncio
async def test_await_reconnect_bails_when_game_stopped():
    """If the game is stopped while waiting, await-reconnect returns False."""
    stub = _make_stub()
    stub._ble_connected = False
    stub._ai_vs_ai_active = False  # already stopped
    result = await stub._ai_vs_ai_await_reconnect(timeout=30.0)
    assert result is False

"""AI-vs-AI loop resilience: a transient BLE drop should not kill the game.

Regression test for the ply-9 disconnect observed live 2026-05-31. The
``_ai_vs_ai_loop`` used to ``break`` permanently on the first
``async_phantom_apply_ai_move`` failure. It now waits for the background
maintain loop to reconnect (``_ai_vs_ai_await_reconnect``) and RE-DRIVES the
already-pushed current position via ``_phantom_execute_position`` (it must NOT
re-call ``apply_ai_move``, which would no-op because the move is already on
``self._board``).

These tests bind the *real* loop methods to a lightweight stub so they run
with no Home Assistant and no hardware. ``asyncio.sleep`` is patched to a
no-op so the move-delay / reconnect waits don't slow the suite.

Runs in the minimal CI env (no HA) — only needs ``chess``.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

import chess
import pytest

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

    monkeypatch.setattr(asyncio, "sleep", _instant)


def _legal_uci(board: chess.Board) -> str:
    return next(iter(board.legal_moves)).uci()


@pytest.mark.asyncio
async def test_transient_drop_is_re_driven_not_fatal(monkeypatch):
    """One BLE failure mid-game → reconnect + re-drive, game continues."""
    stub = _make_stub()

    async def _compute(_board):
        return _legal_uci(stub._board)

    stub._get_ai_move = AsyncMock(side_effect=_compute)

    applies = {"n": 0}

    async def _apply(uci):
        applies["n"] += 1
        # Fail on the 3rd ply, simulating a BLE drop: push the move (as the
        # real apply_ai_move does before the failed write) but raise and drop
        # the link.
        if applies["n"] == 3:
            stub._board.push(chess.Move.from_uci(uci))
            stub._ble_connected = False
            raise RuntimeError("apply_ai_move BLE write failed: BLE not connected")
        stub._board.push(chess.Move.from_uci(uci))
        if len(stub._board.move_stack) >= 6:
            stub._ai_vs_ai_active = False

    stub.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)

    redrives = {"n": 0}

    async def _execute(*, fen, side, timeout_s, side_opcode):
        redrives["n"] += 1
        assert side == "W"  # _our_color is WHITE in AI-vs-AI
        assert side_opcode == "1"
        assert fen == stub._board.fen()  # re-drive targets the CURRENT position
        return True

    stub._phantom_execute_position = AsyncMock(side_effect=_execute)

    # Emulate the background maintain loop reconnecting: whenever the real
    # _ai_vs_ai_await_reconnect polls (via asyncio.sleep), restore the link.
    # We override sleep inside the coordinator module so the helper's
    # `if self._ble_connected` poll flips True on the next iteration.
    import custom_components.phantom_chess.coordinator as coord_mod

    async def _reconnecting_sleep(_seconds):
        if not stub._ble_connected:
            stub._ble_connected = True
        return None

    monkeypatch.setattr(coord_mod.asyncio, "sleep", _reconnecting_sleep)

    await stub._ai_vs_ai_loop()

    # The dropped move was re-driven exactly once, and the game ran on to our
    # scripted end (6 plies) rather than dying at ply 3.
    assert redrives["n"] == 1, f"expected 1 re-drive, got {redrives['n']}"
    assert len(stub._board.move_stack) >= 6
    assert stub._ai_vs_ai_active is False


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
    real_sleep_patched = asyncio.sleep  # already no-op via fixture

    async def _flip(_seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            stub._ble_connected = True
        return None

    # Override the no-op sleep with one that flips the link after a couple polls.
    import custom_components.phantom_chess.coordinator as coord_mod
    orig = coord_mod.asyncio.sleep
    coord_mod.asyncio.sleep = _flip
    try:
        result = await stub._ai_vs_ai_await_reconnect(timeout=30.0)
    finally:
        coord_mod.asyncio.sleep = orig
    assert result is True


@pytest.mark.asyncio
async def test_await_reconnect_bails_when_game_stopped():
    """If the game is stopped while waiting, await-reconnect returns False."""
    stub = _make_stub()
    stub._ble_connected = False
    stub._ai_vs_ai_active = False  # already stopped
    result = await stub._ai_vs_ai_await_reconnect(timeout=30.0)
    assert result is False

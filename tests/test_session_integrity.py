"""Session integrity regressions derived from the engineering assessment."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import chess
import chess.engine
import pytest
from .ble_mock import make_coordinator, drain_tasks
from custom_components.phantom_chess.lichess_analysis import (
    EvalResult,
    StockfishFallback,
)


async def test_failed_takeback_preserves_authoritative_board():
    c = make_coordinator()
    c._board.push_uci("e2e4")
    before = c._board.fen()
    c._ble_write = AsyncMock(side_effect=RuntimeError("offline"))
    with pytest.raises(RuntimeError):
        await c.async_takeback()
    assert c._board.fen() == before
    assert len(c._board.move_stack) == 1


async def test_successful_takeback_truncates_history():
    c = make_coordinator()
    c._board.push_uci("e2e4")
    c._state["move_history_moves"] = [{"uci": "e2e4", "san": "e4"}]
    c._ble_write = AsyncMock()
    c._await_takeback_completion = AsyncMock()
    await c.async_takeback()
    assert len(c._board.move_stack) == 0
    assert len(c._state["move_history_moves"]) == 0


async def test_two_player_start_failure_cleans_session():
    c = make_coordinator()
    c._phantom_send_game_assistance = AsyncMock()
    c.async_phantom_start_game = AsyncMock(
        side_effect=TimeoutError("no acknowledgement")
    )
    c._announce_via_tts = AsyncMock()
    c._analyze_starting_position = AsyncMock()
    with pytest.raises(TimeoutError):
        await c.async_start_two_player_game()
    await drain_tasks()
    assert c._two_player_active is False
    assert c._state["game_status"] == "idle"
    c._announce_via_tts.assert_not_awaited()


async def test_old_analysis_cannot_overwrite_new_game_history():
    c = make_coordinator()
    entered = asyncio.Event()
    release = asyncio.Event()
    ev = EvalResult(cp=300, mate=None, depth=20, best_uci=None)

    async def evaluate(fen):
        entered.set()
        await release.wait()
        return ev

    c._analysis_client = SimpleNamespace(
        get_eval=evaluate, get_opening=AsyncMock(return_value=(None, None))
    )
    c._maybe_announce_classification = AsyncMock()
    before = chess.Board()
    move = chess.Move.from_uci("e2e4")
    after = before.copy()
    after.push(move)
    c._state["move_history_moves"] = [{"uci": "e2e4", "classification": "unknown"}]
    task = asyncio.create_task(c._analyze_move(0, before, after, move, True))
    await entered.wait()
    c._board = chess.Board()
    c._board.push_uci("d2d4")
    c._state["move_history_moves"] = [{"uci": "d2d4", "classification": "unknown"}]
    c._state["eval_cp"] = None
    release.set()
    await task
    assert c._state["move_history_moves"][0]["uci"] == "d2d4"
    assert c._state["move_history_moves"][0]["classification"] == "unknown"
    assert c._state["eval_cp"] is None


async def test_dead_engine_discarded_after_play_error(tmp_path):
    c = make_coordinator()
    sf = StockfishFallback(c.hass, tmp_path)
    dead = SimpleNamespace(
        configure=AsyncMock(),
        play=AsyncMock(side_effect=chess.engine.EngineTerminatedError("dead")),
    )
    sf._engine = dead
    assert await sf.play_move(chess.Board(), 5, 8) is None
    assert sf._engine is None


async def test_ai_turn_cannot_dispatch_when_paused(monkeypatch):
    from custom_components.phantom_chess import coordinator as module
    from unittest.mock import MagicMock

    monkeypatch.setattr(module, "_sleep", AsyncMock())
    c = make_coordinator()
    c.paused = True
    c._local_game_active = True
    c._board.push_uci("e2e4")
    c._get_ai_move = AsyncMock(return_value="e7e5")
    c.async_phantom_apply_ai_move = AsyncMock(return_value=True)
    c._record_and_analyze_local_move = MagicMock()
    await c._local_ai_turn()
    c.async_phantom_apply_ai_move.assert_not_awaited()


async def test_online_start_preserves_active_local_game():
    c = make_coordinator()
    c._local_game_active = True
    c._board.push_uci("e2e4")
    c._ble_write = AsyncMock(side_effect=RuntimeError("transport failed"))
    with pytest.raises(RuntimeError):
        await c.async_start_game()
    assert len(c._board.move_stack) == 1
    assert c._local_game_active is True


async def test_pause_during_calculation_prevents_dispatch(monkeypatch):
    from custom_components.phantom_chess import coordinator as module

    monkeypatch.setattr(module, "_sleep", AsyncMock())
    c = make_coordinator()
    c._local_game_active = True
    entered = asyncio.Event()
    release = asyncio.Event()

    async def calculate(board):
        entered.set()
        await release.wait()
        return "e2e4"

    c._get_ai_move = calculate
    c.async_phantom_apply_ai_move = AsyncMock()
    task = asyncio.create_task(c._local_ai_turn())
    await entered.wait()
    c.paused = True
    release.set()
    await task
    c.async_phantom_apply_ai_move.assert_not_awaited()


async def test_replaced_board_during_calculation_prevents_dispatch(monkeypatch):
    from custom_components.phantom_chess import coordinator as module

    monkeypatch.setattr(module, "_sleep", AsyncMock())
    c = make_coordinator()
    c._local_game_active = True

    async def calculate(board):
        c._board = chess.Board()
        return "e2e4"

    c._get_ai_move = calculate
    c.async_phantom_apply_ai_move = AsyncMock()
    await c._local_ai_turn()
    c.async_phantom_apply_ai_move.assert_not_awaited()


async def test_takeback_timeout_preserves_position_and_history():
    c = make_coordinator()
    c._board.push_uci("e2e4")
    before = c._board.fen()
    history = [{"uci": "e2e4"}]
    c._state["move_history_moves"] = history
    c._ble_write = AsyncMock()
    c._await_takeback_completion = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        await c.async_takeback()
    assert c._board.fen() == before
    assert c._state["move_history_moves"] == history
    assert c._state["physical_operation"] == "uncertain"
    assert c.paused
    assert c._move_done_future is None


async def test_second_physical_operation_cannot_steal_completion_channel():
    c = make_coordinator()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute(*args):
        entered.set()
        await release.wait()
        return True

    c._execute_position_unlocked = execute
    task = asyncio.create_task(c._phantom_execute_position(chess.STARTING_FEN))
    await entered.wait()
    with pytest.raises(RuntimeError, match="still moving"):
        await c._phantom_execute_position(chess.STARTING_FEN)
    with pytest.raises(RuntimeError, match="still moving"):
        await c.async_takeback()
    release.set()
    assert await task is True
    assert c._state["position_confirmed"] is True


async def test_reset_timeout_preserves_last_position():
    c = make_coordinator()
    c._board.push_uci("e2e4")
    before = c._board.fen()
    c._state["live_fen"] = c._board.board_fen()
    c._phantom_execute_position = AsyncMock(return_value=False)
    with pytest.raises(TimeoutError):
        await c.async_reset_position()
    assert c._board.fen() == before
    assert c._state["live_fen"] == c._board.board_fen()


async def test_pausing_then_resuming_invalidates_old_calculation(monkeypatch):
    from custom_components.phantom_chess import coordinator as module

    monkeypatch.setattr(module, "_sleep", AsyncMock())
    c = make_coordinator()
    c._local_game_active = True
    c._our_color = chess.WHITE
    c._ble_write = AsyncMock()

    async def calculate(board):
        await c.async_set_pause(True)
        await c.async_set_pause(False)
        return "e2e4"

    c._get_ai_move = calculate
    c.async_phantom_apply_ai_move = AsyncMock()
    await c._local_ai_turn()
    c.async_phantom_apply_ai_move.assert_not_awaited()


async def test_confirmed_undo_reschedules_computer_when_it_is_its_turn():
    c = make_coordinator()
    c._local_game_active = True
    c._our_color = chess.WHITE
    c._board.push_uci("e2e4")
    c._board.push_uci("e7e5")
    c._ble_write = AsyncMock()
    c._await_takeback_completion = AsyncMock()
    c._replace_local_game_task = AsyncMock()
    await c.async_takeback(1)
    assert c._board.turn == chess.BLACK
    c._replace_local_game_task.assert_awaited_once()
    assert not c._physical_operation_lock.locked()


async def test_dashboard_failed_motion_does_not_schedule_reply():
    c = make_coordinator(ble_connected=True)
    c._local_game_active = True
    c._our_color = chess.WHITE
    c.async_phantom_apply_ai_move = AsyncMock(return_value=False)
    c._replace_local_game_task = AsyncMock()
    with pytest.raises(RuntimeError, match="did not confirm"):
        await c.async_execute_dashboard_move("e2e4")
    c._replace_local_game_task.assert_not_awaited()


@pytest.mark.parametrize("method", ["async_start_ai_vs_ai_game", "async_start_sculpture", "async_play_selected_sculpture"])
async def test_spectator_modes_cannot_replace_active_game(method):
    c = make_coordinator(ble_connected=True)
    c._local_game_active = True
    before = c._board
    with pytest.raises(RuntimeError, match="already running"):
        await getattr(c, method)()
    assert c._local_game_active
    assert c._board is before

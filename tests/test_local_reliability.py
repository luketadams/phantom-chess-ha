"""Regression tests for voice starts and actual physical local-game paths."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.phantom_chess import const
from .ble_mock import make_coordinator


def quiet(coord):
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **kw: coro.close())
    coord.hass.services.async_call = AsyncMock()
    coord._phantom_send_game_assistance = AsyncMock()
    coord.async_phantom_start_game = AsyncMock()
    coord.player_color = "white"


@pytest.mark.parametrize("failure", [RuntimeError("BLE failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_start_is_not_active(failure):
    coord = make_coordinator(ble_connected=True)
    quiet(coord)
    coord.async_phantom_start_game.side_effect = failure
    with pytest.raises(type(failure)):
        await coord.async_start_local_game()
    assert not coord._local_game_active
    assert not coord._state["local_game_active"]
    assert coord._state["lichess_game_id"] is None


async def test_repeated_voice_request_preserves_moves():
    coord = make_coordinator(ble_connected=True)
    quiet(coord)
    await coord.async_start_local_game()
    coord._board.push_uci("e2e4")
    await coord.async_start_local_game()
    assert coord._board.peek().uci() == "e2e4"
    coord.async_phantom_start_game.assert_awaited_once()


async def test_simultaneous_starts_activate_only_once():
    coord = make_coordinator(ble_connected=True)
    quiet(coord)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def activation(**kw):
        entered.set()
        await release.wait()
    coord.async_phantom_start_game.side_effect = activation
    first = asyncio.create_task(coord.async_start_local_game())
    await entered.wait()
    second = asyncio.create_task(coord.async_start_local_game())
    release.set()
    await asyncio.gather(first, second)
    coord.async_phantom_start_game.assert_awaited_once()


async def test_voice_start_does_not_abandon_online_game():
    coord = make_coordinator(ble_connected=True)
    coord._game_id = "existing_game"
    with pytest.raises(RuntimeError, match="already running"):
        await coord.async_start_local_game()
    assert coord._game_id == "existing_game"


async def test_shutdown_awaits_ai_task_before_engine_shutdown():
    coord = make_coordinator()
    cancelled = asyncio.Event()
    async def ai():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    coord._local_game_active = True
    coord._local_game_task = asyncio.create_task(ai())
    await asyncio.sleep(0)
    coord._analysis_client = MagicMock()
    async def shutdown():
        assert cancelled.is_set()
    coord._analysis_client.shutdown = AsyncMock(side_effect=shutdown)
    await coord.async_shutdown()
    assert coord._local_game_task.cancelled()
    assert not coord._local_game_active


async def test_physical_human_move_records_history_before_ai_response():
    coord = make_coordinator(ble_connected=True)
    quiet(coord)
    coord._local_game_active = True
    coord._replace_local_game_task = AsyncMock()
    coord._apply_move_frame("M 1 e2-e4", const.UUID_GAME, coord._ble_client, 3)
    assert coord._analysis_board.peek().uci() == "e2e4"
    assert len(coord._state["move_history_moves"]) == 1
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    coord._replace_local_game_task.assert_awaited_once()


async def test_physical_human_mate_finishes_without_scheduling_ai():
    coord = make_coordinator(ble_connected=True)
    quiet(coord)
    for move in ("f2f3", "e7e5", "g2g4"):
        coord._board.push_uci(move)
    coord._analysis_board = coord._board.copy()
    coord._local_game_active = True
    coord._replace_local_game_task = AsyncMock()
    coord._apply_move_frame("M 1 d8-h4", const.UUID_GAME, coord._ble_client, 3)
    assert coord._board.is_checkmate()
    assert not coord._local_game_active
    assert coord._state["last_game_result"] == "0-1"
    assert coord._state["lichess_review_ready"]
    await asyncio.sleep(0)
    coord._replace_local_game_task.assert_not_awaited()


async def test_unconfirmed_ai_move_halts_local_game(monkeypatch):
    from custom_components.phantom_chess import coordinator as module
    monkeypatch.setattr(module.rt, "_sleep", AsyncMock())
    coord = make_coordinator(ble_connected=True)
    quiet(coord)
    coord._local_game_active = True
    coord._get_ai_move = AsyncMock(return_value="e2e4")
    coord.async_phantom_apply_ai_move = AsyncMock(return_value=False)
    coord._record_and_analyze_local_move = MagicMock()
    await coord._local_ai_turn()
    assert not coord._local_game_active
    assert coord.paused
    coord._record_and_analyze_local_move.assert_not_called()
    coord.hass.services.async_call.assert_awaited_once()
    coord._apply_move_frame("M 1 e2-e4", const.UUID_GAME, coord._ble_client, 3)
    assert not coord._board.move_stack


async def test_stop_waits_for_ai_cancellation_before_pause_command():
    coord = make_coordinator(ble_connected=True)
    stopped = asyncio.Event()
    async def ai():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    coord._local_game_task = asyncio.create_task(ai())
    await asyncio.sleep(0)
    async def write(*args):
        assert stopped.is_set()
    coord._ble_write = AsyncMock(side_effect=write)
    await coord.async_stop_local_game()
    assert coord._local_game_task.cancelled()
    coord._ble_write.assert_awaited_once()


async def test_shutdown_rejects_late_ai_replacement_callback():
    coord = make_coordinator()
    coord._stop_event.set()
    coord._local_ai_turn = AsyncMock()
    await coord._replace_local_game_task(name="late_callback")
    assert coord._local_game_task is None
    coord._local_ai_turn.assert_not_called()


@pytest.mark.parametrize("method", ["_analyze_starting_position", "async_request_hint"])
async def test_delayed_position_evaluation_does_not_overwrite_new_position(method):
    coord = make_coordinator(ble_connected=True)
    coord._state["eval_cp"] = 123
    client = MagicMock()
    async def delayed(*args, **kwargs):
        coord._board.push_uci("e2e4")
        return MagicMock(cp=999, mate=None, depth=18, source="local")
    client.get_eval = AsyncMock(side_effect=delayed)
    client.get_opening = AsyncMock(return_value=(None, None))
    coord._analysis_client = client
    await getattr(coord, method)()
    assert coord._state["eval_cp"] == 123
    client.get_opening.assert_not_awaited()

async def test_mating_reply_after_check_is_announced():
    """Regression:39...Nh3# was silent because status still said check."""
    import chess
    coord = make_coordinator(ble_connected=True)
    coord._local_game_active = True
    coord._state['game_status'] = const.STATUS_CHECK
    coord._board = chess.Board('3b4/2r1k1pp/P3P3/3b1P2/6P1/3p4/2p2n1P/5RK1 b - - 2 39')
    coord._phantom_execute_position = AsyncMock(return_value=True)
    coord._announce_via_tts = AsyncMock()
    tasks = []
    def schedule(coro, **kw):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task
    coord.hass.async_create_task = schedule
    assert await coord.async_phantom_apply_ai_move('f2h3')
    await asyncio.gather(*tasks)
    assert coord._board.is_checkmate()
    coord._announce_via_tts.assert_awaited_once_with('Black knight to h3. Checkmate. Black wins')

@pytest.mark.parametrize("status,sculpture,expected", [(const.STATUS_CHECK,False,True),(const.STATUS_CHECK,True,False),(const.STATUS_PAUSED,False,False),(const.STATUS_IDLE,False,False),(const.STATUS_CHECKMATE,False,False)])
async def test_speech_gate_in_check_preserves_other_mode_guards(status, sculpture, expected):
    coord = make_coordinator()
    coord._state['game_status'] = status
    coord._sculpture_active = sculpture
    assert coord._should_announce_active_game() is expected

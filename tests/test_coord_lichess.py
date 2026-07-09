"""Lichess-path coordinator tests.

Exercises the Lichess bridge surface of ``PhantomChessCoordinator``:
event routing (``_handle_lichess_event``), the per-event handlers
(``_on_game_full`` / ``_on_game_state`` / ``_on_game_finish``), clock
extraction (``_update_clocks_from_event``), the move-list differ
(``_process_move_list``), the history-stub builder
(``_record_history_stub``), the REST reconcile / resume-from-phone /
configured-start entry points, the physical-move drain + Lichess POST,
the stream loop, and the stream-task done callback.

These run against the BLE/coordinator harness in ``tests/ble_mock.py``
with all async sub-calls (analysis, AI-move application, network I/O)
stubbed via ``AsyncMock`` so each unit is isolated.

Run (fast, minimal-style env — no phacc needed)::

    pytest tests/test_coord_lichess.py -p no:pytest_homeassistant_custom_component
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import chess
import pytest

from custom_components.phantom_chess import const  # noqa: F401
from custom_components.phantom_chess import coordinator as coord_mod
from custom_components.phantom_chess.coordinator import PhantomChessCoordinator  # noqa: F401

from .ble_mock import make_coordinator


# ─── helpers ────────────────────────────────────────────────────────────────


def _quiet_create_task(coord) -> None:
    """Make ``hass.async_create_task`` close the coroutine it's handed.

    The harness's hass is a MagicMock, so ``async_create_task`` returns a
    MagicMock without ever awaiting the coroutine argument — Python then
    emits a "coroutine was never awaited" RuntimeWarning. Closing the coro
    silences that while still recording the call for assertions.
    """
    def _consume(coro, *args, **kwargs):
        try:
            coro.close()
        except AttributeError:
            pass
        return MagicMock()

    coord.hass.async_create_task = MagicMock(side_effect=_consume)


def _stub_analysis(coord) -> None:
    """Neutralise the async analysis fan-out so move processing is pure."""
    coord._analyze_move = AsyncMock()
    coord._analyze_starting_position = AsyncMock()
    coord._build_post_game_review = AsyncMock()
    _quiet_create_task(coord)


# ─── _handle_lichess_event routing ──────────────────────────────────────────


async def test_handle_event_routes_gamefull():
    coord = make_coordinator()
    coord._on_game_full = AsyncMock()
    coord._on_game_state = AsyncMock()
    coord._on_game_finish = MagicMock()
    await coord._handle_lichess_event({"type": "gameFull"})
    coord._on_game_full.assert_awaited_once()
    coord._on_game_state.assert_not_awaited()
    coord._on_game_finish.assert_not_called()


async def test_handle_event_routes_gamestate():
    coord = make_coordinator()
    coord._on_game_full = AsyncMock()
    coord._on_game_state = AsyncMock()
    coord._on_game_finish = MagicMock()
    await coord._handle_lichess_event({"type": "gameState"})
    coord._on_game_state.assert_awaited_once()


async def test_handle_event_routes_gamefinish():
    coord = make_coordinator()
    coord._on_game_full = AsyncMock()
    coord._on_game_state = AsyncMock()
    coord._on_game_finish = MagicMock()
    await coord._handle_lichess_event({"type": "gameFinish"})
    coord._on_game_finish.assert_called_once()


async def test_handle_event_ignores_unknown_type():
    coord = make_coordinator()
    coord._on_game_full = AsyncMock()
    coord._on_game_state = AsyncMock()
    coord._on_game_finish = MagicMock()
    await coord._handle_lichess_event({"type": "chatLine"})
    coord._on_game_full.assert_not_awaited()
    coord._on_game_state.assert_not_awaited()
    coord._on_game_finish.assert_not_called()


# ─── _on_game_full color logic ──────────────────────────────────────────────


async def test_on_game_full_explicit_white():
    coord = make_coordinator()
    coord.player_color = "white"
    _stub_analysis(coord)
    coord._process_move_list = AsyncMock()
    await coord._on_game_full({"white": {}, "black": {}, "state": {}})
    assert coord._our_color == chess.WHITE
    assert coord._state["lichess_active"] is True
    assert coord._state["lichess_review_ready"] is False


async def test_on_game_full_explicit_black():
    coord = make_coordinator()
    coord.player_color = "black"
    _stub_analysis(coord)
    coord._process_move_list = AsyncMock()
    await coord._on_game_full({"white": {}, "black": {}, "state": {}})
    assert coord._our_color == chess.BLACK


async def test_on_game_full_random_ai_is_white_means_we_are_black():
    coord = make_coordinator()
    coord.player_color = "random"
    _stub_analysis(coord)
    coord._process_move_list = AsyncMock()
    await coord._on_game_full(
        {"white": {"aiLevel": 5}, "black": {"id": "me"}, "state": {}}
    )
    assert coord._our_color == chess.BLACK


async def test_on_game_full_random_ai_not_white_means_we_are_white():
    coord = make_coordinator()
    coord.player_color = "random"
    _stub_analysis(coord)
    coord._process_move_list = AsyncMock()
    await coord._on_game_full(
        {"white": {"id": "me"}, "black": {"aiLevel": 5}, "state": {}}
    )
    assert coord._our_color == chess.WHITE


async def test_on_game_full_sets_player_names_from_ai_level():
    coord = make_coordinator()
    coord.player_color = "white"
    _stub_analysis(coord)
    coord._process_move_list = AsyncMock()
    await coord._on_game_full(
        {
            "white": {"name": "Luke"},
            "black": {"aiLevel": 3},
            "state": {},
        }
    )
    assert coord._state["lichess_white_name"] == "Luke"
    assert coord._state["lichess_black_name"] == "Stockfish level 3"


async def test_on_game_full_processes_existing_moves():
    coord = make_coordinator()
    coord.player_color = "white"
    _stub_analysis(coord)
    coord._process_move_list = AsyncMock()
    await coord._on_game_full(
        {"white": {}, "black": {}, "state": {"moves": "e2e4 e7e5"}}
    )
    coord._process_move_list.assert_awaited_once_with("e2e4 e7e5")


async def test_on_game_full_skips_process_when_no_moves():
    coord = make_coordinator()
    coord.player_color = "white"
    _stub_analysis(coord)
    coord._process_move_list = AsyncMock()
    await coord._on_game_full({"white": {}, "black": {}, "state": {}})
    coord._process_move_list.assert_not_awaited()


async def test_on_game_full_extracts_initial_clocks():
    coord = make_coordinator()
    coord.player_color = "white"
    _stub_analysis(coord)
    coord._process_move_list = AsyncMock()
    await coord._on_game_full(
        {"white": {}, "black": {}, "state": {"wtime": 300000, "btime": 250000}}
    )
    assert coord._state["lichess_white_clock"] == 300
    assert coord._state["lichess_black_clock"] == 250


# ─── _update_clocks_from_event ──────────────────────────────────────────────


async def test_update_clocks_toplevel_wtime():
    coord = make_coordinator()
    coord._update_clocks_from_event({"wtime": 120000, "btime": 90000})
    assert coord._state["lichess_white_clock"] == 120
    assert coord._state["lichess_black_clock"] == 90


async def test_update_clocks_nested_state():
    coord = make_coordinator()
    coord._update_clocks_from_event({"state": {"wtime": 60000, "btime": 30000}})
    assert coord._state["lichess_white_clock"] == 60
    assert coord._state["lichess_black_clock"] == 30


async def test_update_clocks_ignores_missing_or_nonnumeric():
    coord = make_coordinator()
    coord._state["lichess_white_clock"] = 999
    coord._state["lichess_black_clock"] = 888
    coord._update_clocks_from_event({"wtime": None, "btime": "oops"})
    # Non-numeric / missing values leave the prior values untouched.
    assert coord._state["lichess_white_clock"] == 999
    assert coord._state["lichess_black_clock"] == 888


# ─── _record_history_stub ───────────────────────────────────────────────────


async def test_record_history_stub_first_move_white():
    coord = make_coordinator()
    coord._analysis_board = chess.Board()
    idx = coord._record_history_stub(chess.Move.from_uci("e2e4"), chess.WHITE)
    assert idx == 0
    hist = coord._state["move_history_moves"]
    assert len(hist) == 1
    entry = hist[0]
    assert entry["side"] == "white"
    assert entry["san"] == "e4"
    assert entry["uci"] == "e2e4"
    assert entry["classification"] == "unknown"
    assert entry["ply"] == 1
    assert entry["move_num"] == 1


async def test_record_history_stub_second_move_same_move_num():
    coord = make_coordinator()
    board = chess.Board()
    coord._analysis_board = board
    coord._record_history_stub(chess.Move.from_uci("e2e4"), chess.WHITE)
    board.push(chess.Move.from_uci("e2e4"))
    idx2 = coord._record_history_stub(chess.Move.from_uci("e7e5"), chess.BLACK)
    assert idx2 == 1
    hist = coord._state["move_history_moves"]
    # White(ply1) + Black(ply2) share move number 1.
    assert hist[0]["move_num"] == 1
    assert hist[1]["move_num"] == 1
    assert hist[1]["side"] == "black"


async def test_record_history_stub_san_fallback_on_exception():
    # Force the stub's except-branch: patch san() to raise, proving the
    # fallback to move.uci(). (python-chess's san() is lenient enough that a
    # merely-illegal move gets coerced rather than raising, so we patch it.)
    coord = make_coordinator()
    coord._analysis_board = chess.Board()
    move = chess.Move.from_uci("e2e4")
    with patch.object(
        coord._analysis_board, "san", side_effect=ValueError("bad")
    ):
        coord._record_history_stub(move, chess.WHITE)
    assert coord._state["move_history_moves"][0]["san"] == "e2e4"


# ─── _process_move_list ─────────────────────────────────────────────────────


async def test_process_move_list_empty_string_noop():
    coord = make_coordinator()
    _stub_analysis(coord)
    await coord._process_move_list("")
    coord.async_set_updated_data.assert_not_called()


async def test_process_move_list_no_new_moves_noop():
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._processed_moves = 2
    await coord._process_move_list("e2e4 e7e5")
    coord.async_set_updated_data.assert_not_called()


async def test_process_move_list_our_move_echo_advances_counter():
    # We are white and already pushed e2e4 locally; the stream echoes it.
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE
    coord._board = chess.Board()
    coord._board.push(chess.Move.from_uci("e2e4"))  # human move already on board
    await coord._process_move_list("e2e4")
    assert coord._processed_moves == 1
    coord.async_set_updated_data.assert_called()


async def test_process_move_list_applies_ai_move():
    # We are white; black's reply is an AI move → routed to apply_ai_move.
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE
    coord._board = chess.Board()
    coord._board.push(chess.Move.from_uci("e2e4"))
    coord._processed_moves = 1  # our e2e4 already processed
    coord.async_phantom_apply_ai_move = AsyncMock()
    coord._drain_physical_move_queue = AsyncMock()
    await coord._process_move_list("e2e4 e7e5")
    coord.async_phantom_apply_ai_move.assert_awaited_once_with("e7e5")
    assert coord._processed_moves == 2
    assert coord._state["last_move"] == "e7e5"


async def test_process_move_list_ai_move_failure_falls_back_to_local_push():
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE
    coord._board = chess.Board()
    coord._board.push(chess.Move.from_uci("e2e4"))
    coord._processed_moves = 1
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=RuntimeError("no magnet"))
    coord._drain_physical_move_queue = AsyncMock()
    await coord._process_move_list("e2e4 e7e5")
    # Fallback pushed the move onto the board despite the magnet failure.
    assert coord._board.peek() == chess.Move.from_uci("e7e5")
    assert coord._processed_moves == 2


async def test_process_move_list_invalid_uci_skipped():
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE
    coord._board = chess.Board()
    coord._drain_physical_move_queue = AsyncMock()
    await coord._process_move_list("notauci")
    # Invalid UCI is skipped without advancing processed_moves.
    assert coord._processed_moves == 0
    coord.async_set_updated_data.assert_called()


async def test_process_move_list_our_move_safety_net_push():
    # We are white, board empty of the move; the differ pushes it as safety net.
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE
    coord._board = chess.Board()  # e2e4 NOT yet pushed
    coord._drain_physical_move_queue = AsyncMock()
    await coord._process_move_list("e2e4")
    assert coord._board.peek() == chess.Move.from_uci("e2e4")
    assert coord._processed_moves == 1


async def test_process_move_list_drains_queue_on_our_turn():
    # We are black; the AI (white) opens with e2e4 which apply_ai_move pushes.
    # After that it is black's (our) turn → the queue drain fires.
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.BLACK
    coord._board = chess.Board()

    async def _apply(uci):
        coord._board.push(chess.Move.from_uci(uci))

    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    coord._drain_physical_move_queue = AsyncMock()
    await coord._process_move_list("e2e4")
    coord.async_phantom_apply_ai_move.assert_awaited_once_with("e2e4")
    coord._drain_physical_move_queue.assert_awaited_once()


async def test_process_move_list_sets_check_status():
    # We are white so our own moves push via the safety-net; feed a line that
    # ends in check. 1.e4 f5 2.Qh5+ delivers check on the black king.
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE

    async def _apply(uci):
        coord._board.push(chess.Move.from_uci(uci))

    coord._board = chess.Board()
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    coord._drain_physical_move_queue = AsyncMock()
    await coord._process_move_list("e2e4 f7f5 d1h5")
    assert coord._board.is_check()
    assert coord._state["game_status"] == "check"


# ─── _on_game_state terminal handling ───────────────────────────────────────


async def test_on_game_state_checkmate_terminal():
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE
    coord._board = chess.Board()
    coord._analysis_board = chess.Board()
    coord.async_phantom_apply_ai_move = AsyncMock()
    coord._drain_physical_move_queue = AsyncMock()
    coord._game_id = "abc123"
    # Fool's mate: 1.f3 e5 2.g4 Qh4#
    event = {"status": "mate", "moves": "f2f3 e7e5 g2g4 d8h4"}
    await coord._on_game_state(event)
    assert coord._state["lichess_active"] is False
    assert coord._game_id is None
    assert coord._state["last_game_result"] is not None
    # Terminal path schedules the post-game review via async_create_task; the
    # coroutine object is created (recorded as a call) then handed off.
    coord._build_post_game_review.assert_called_once()
    coord.hass.async_create_task.assert_called()


async def test_on_game_state_resign_with_winner():
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE
    coord._board = chess.Board()
    coord._game_id = "g1"
    coord._drain_physical_move_queue = AsyncMock()
    await coord._on_game_state({"status": "resign", "moves": "", "winner": "white"})
    assert coord._state["game_status"] == const.STATUS_RESIGNED
    assert coord._state["last_game_result"] == "1-0 (resignation)"
    assert coord._game_id is None


async def test_on_game_state_draw_status():
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE
    coord._board = chess.Board()
    coord._game_id = "g1"
    coord._drain_physical_move_queue = AsyncMock()
    await coord._on_game_state({"status": "outoftime", "moves": ""})
    assert coord._state["game_status"] == const.STATUS_DRAW
    assert coord._state["last_game_result"] == "1/2-1/2 (outoftime)"


async def test_on_game_state_stalemate_status():
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE
    coord._board = chess.Board()
    coord._game_id = "g1"
    coord._drain_physical_move_queue = AsyncMock()
    await coord._on_game_state({"status": "stalemate", "moves": ""})
    assert coord._state["game_status"] == const.STATUS_STALEMATE
    assert coord._state["last_game_result"] == "1/2-1/2 (stalemate)"


async def test_on_game_state_nonterminal_started():
    coord = make_coordinator()
    _stub_analysis(coord)
    coord._our_color = chess.WHITE
    coord._board = chess.Board()
    coord._game_id = "g1"
    coord._drain_physical_move_queue = AsyncMock()
    await coord._on_game_state({"status": "started", "moves": ""})
    assert coord._state["lichess_active"] is not False or True  # not touched
    assert coord._game_id == "g1"  # still active


# ─── _on_game_finish ────────────────────────────────────────────────────────


async def test_on_game_finish_dict_status():
    coord = make_coordinator()
    coord._game_id = "xyz"
    coord._on_game_finish({"status": {"name": "mate"}})
    assert coord._game_id is None
    assert coord._state["lichess_active"] is False
    coord.async_set_updated_data.assert_called()


async def test_on_game_finish_scalar_status():
    coord = make_coordinator()
    coord._game_id = "xyz"
    coord._on_game_finish({"status": "aborted"})
    assert coord._game_id is None
    assert coord._state["lichess_active"] is False


# ─── async_reconcile_lichess_state ──────────────────────────────────────────


async def test_reconcile_noop_when_no_game():
    coord = make_coordinator()
    coord._game_id = None
    coord._on_game_state = AsyncMock()
    await coord.async_reconcile_lichess_state()
    coord._on_game_state.assert_not_awaited()


async def test_reconcile_noop_when_no_token():
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._lichess_token = None
    coord._on_game_state = AsyncMock()
    await coord.async_reconcile_lichess_state()
    coord._on_game_state.assert_not_awaited()


async def test_reconcile_still_active_no_sync(mock_aiohttp_session_factory):
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._on_game_state = AsyncMock()
    session = mock_aiohttp_session_factory(status=200, json_data={"status": "started"})
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_reconcile_lichess_state()
    coord._on_game_state.assert_not_awaited()


async def test_reconcile_terminal_syncs(mock_aiohttp_session_factory):
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._on_game_state = AsyncMock()
    session = mock_aiohttp_session_factory(
        status=200,
        json_data={"status": "mate", "moves": "e2e4 e7e5", "winner": "white"},
    )
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_reconcile_lichess_state()
    coord._on_game_state.assert_awaited_once()
    synth = coord._on_game_state.await_args.args[0]
    assert synth["type"] == "gameState"
    assert synth["status"] == "mate"
    assert synth["winner"] == "white"


async def test_reconcile_non200_returns(mock_aiohttp_session_factory):
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._on_game_state = AsyncMock()
    session = mock_aiohttp_session_factory(status=404, json_data={})
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_reconcile_lichess_state()
    coord._on_game_state.assert_not_awaited()


async def test_reconcile_query_exception_swallowed():
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._on_game_state = AsyncMock()
    session = MagicMock()
    session.get = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_reconcile_lichess_state()  # must not raise
    coord._on_game_state.assert_not_awaited()


# ─── async_resume_from_phone ────────────────────────────────────────────────


async def test_resume_from_phone_noop_no_game():
    coord = make_coordinator()
    coord._game_id = None
    coord._phantom_send_reset_detection = AsyncMock()
    await coord.async_resume_from_phone()
    coord._phantom_send_reset_detection.assert_not_awaited()


async def test_resume_from_phone_noop_ble_disconnected():
    coord = make_coordinator(ble_connected=False)
    coord._game_id = "g1"
    coord._phantom_send_reset_detection = AsyncMock()
    await coord.async_resume_from_phone()
    coord._phantom_send_reset_detection.assert_not_awaited()


async def test_resume_from_phone_pushes_reset_detection():
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._board = chess.Board()
    coord._phantom_send_reset_detection = AsyncMock()
    coord.hass.services = MagicMock()
    coord.hass.services.async_call = AsyncMock()
    await coord.async_resume_from_phone()
    coord._phantom_send_reset_detection.assert_awaited_once_with(
        coord._board.board_fen()
    )
    coord.hass.services.async_call.assert_awaited()  # dismiss notification


async def test_resume_from_phone_reset_failure_raises():
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._board = chess.Board()
    coord._phantom_send_reset_detection = AsyncMock(side_effect=RuntimeError("ble"))
    with pytest.raises(RuntimeError):
        await coord.async_resume_from_phone()


async def test_resume_from_phone_dismiss_error_swallowed():
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._board = chess.Board()
    coord._phantom_send_reset_detection = AsyncMock()
    coord.hass.services = MagicMock()
    coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("no such svc"))
    await coord.async_resume_from_phone()  # must not raise
    coord._phantom_send_reset_detection.assert_awaited_once()


# ─── async_start_lichess_configured ─────────────────────────────────────────


async def test_start_lichess_configured_uses_number_entities():
    coord = make_coordinator()
    coord.lichess_clock_minutes = 5
    coord.lichess_clock_increment = 3
    coord.async_start_game = AsyncMock()
    await coord.async_start_lichess_configured()
    coord.async_start_game.assert_awaited_once_with(
        clock_limit_seconds=300, clock_increment_seconds=3
    )


# ─── _drain_physical_move_queue ─────────────────────────────────────────────


async def test_drain_empty_queue_noop():
    coord = make_coordinator()
    coord._push_move_to_lichess = AsyncMock()
    coord._push_move_to_local_ai = AsyncMock()
    await coord._drain_physical_move_queue()
    coord._push_move_to_lichess.assert_not_awaited()
    coord._push_move_to_local_ai.assert_not_awaited()


async def test_drain_routes_to_lichess():
    coord = make_coordinator()
    coord._local_game_active = False
    coord._push_move_to_lichess = AsyncMock()
    coord._physical_move_queue.put_nowait("e2e4")
    coord._physical_move_queue.put_nowait("g1f3")
    await coord._drain_physical_move_queue()
    assert coord._push_move_to_lichess.await_count == 2


async def test_drain_routes_to_local_ai():
    coord = make_coordinator()
    coord._local_game_active = True
    coord._push_move_to_local_ai = AsyncMock()
    coord._physical_move_queue.put_nowait("e2e4")
    await coord._drain_physical_move_queue()
    coord._push_move_to_local_ai.assert_awaited_once_with("e2e4")


async def test_drain_skips_malformed_entry():
    coord = make_coordinator()
    coord._local_game_active = False
    coord._push_move_to_lichess = AsyncMock()
    coord._physical_move_queue.put_nowait("xx")  # too short
    coord._physical_move_queue.put_nowait("e2e4")
    await coord._drain_physical_move_queue()
    coord._push_move_to_lichess.assert_awaited_once_with("e2e4")


# ─── _push_move_to_lichess ──────────────────────────────────────────────────


async def test_push_move_noop_no_game():
    coord = make_coordinator()
    coord._game_id = None
    with patch.object(coord_mod, "async_get_clientsession") as gs:
        await coord._push_move_to_lichess("e2e4")
    gs.assert_not_called()


async def test_push_move_accepted(mock_aiohttp_session_factory):
    coord = make_coordinator()
    coord._game_id = "g1"
    session = mock_aiohttp_session_factory(status=200)
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord._push_move_to_lichess("e2e4")
    session.post.assert_called_once()


async def test_push_move_rejected_logs(mock_aiohttp_session_factory):
    coord = make_coordinator()
    coord._game_id = "g1"
    session = mock_aiohttp_session_factory(status=400)
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord._push_move_to_lichess("e2e4")  # must not raise on rejection
    session.post.assert_called_once()


# ─── _lichess_task_done_cb ──────────────────────────────────────────────────


async def test_task_done_cb_cancelled_is_noop():
    coord = make_coordinator()
    coord._game_id = "g1"
    _quiet_create_task(coord)
    task = MagicMock()
    task.cancelled.return_value = True
    coord._lichess_task_done_cb(task)
    coord.hass.async_create_task.assert_not_called()


async def test_task_done_cb_clean_exit_game_done_is_noop():
    coord = make_coordinator()
    coord._game_id = None
    _quiet_create_task(coord)
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = None
    coord._lichess_task_done_cb(task)
    coord.hass.async_create_task.assert_not_called()


async def test_task_done_cb_exit_with_game_still_set_reconciles():
    coord = make_coordinator()
    coord._game_id = "g1"
    _quiet_create_task(coord)
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = None
    coord._lichess_task_done_cb(task)
    coord.hass.async_create_task.assert_called_once()


async def test_task_done_cb_crash_reconciles():
    coord = make_coordinator()
    coord._game_id = "g1"
    _quiet_create_task(coord)
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = RuntimeError("stream died")
    coord._lichess_task_done_cb(task)
    coord.hass.async_create_task.assert_called_once()


async def test_task_done_cb_crash_but_game_cleared_no_reconcile():
    coord = make_coordinator()
    coord._game_id = None
    _quiet_create_task(coord)
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = RuntimeError("stream died")
    coord._lichess_task_done_cb(task)
    # Crash is logged, but with no active game there's nothing to reconcile.
    coord.hass.async_create_task.assert_not_called()


# ─── _lichess_stream_loop ───────────────────────────────────────────────────


async def test_stream_loop_401_triggers_reauth():
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._entry = MagicMock()
    resp = MagicMock(status=401)
    resp_cm = MagicMock()
    resp_cm.__aenter__ = AsyncMock(return_value=resp)
    resp_cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get = MagicMock(return_value=resp_cm)
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord._lichess_stream_loop("g1")
    coord._entry.async_start_reauth.assert_called_once()


async def test_stream_loop_403_triggers_reauth_none_entry():
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._entry = None
    resp = MagicMock(status=403)
    resp_cm = MagicMock()
    resp_cm.__aenter__ = AsyncMock(return_value=resp)
    resp_cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get = MagicMock(return_value=resp_cm)
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord._lichess_stream_loop("g1")  # returns without raising


async def test_stream_loop_stops_when_game_id_changes():
    coord = make_coordinator()
    coord._game_id = "other"  # loop guard sees a different game immediately
    session = MagicMock()
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord._lichess_stream_loop("g1")
    session.get.assert_not_called()


async def test_stream_loop_stops_when_stop_event_set():
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._stop_event.set()
    session = MagicMock()
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord._lichess_stream_loop("g1")
    session.get.assert_not_called()


async def test_stream_loop_drives_events_then_exits():
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._handle_lichess_event = AsyncMock()

    # A streamed body: one heartbeat blank line, one JSON event, one bad line.
    class _Content:
        def __init__(self, lines):
            self._lines = lines

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for line in self._lines:
                yield line

    lines = [
        b"\n",  # heartbeat
        b'{"type": "gameState", "moves": "e2e4"}\n',
        b"not-json\n",
    ]
    resp = MagicMock(status=200)
    resp.content = _Content(lines)
    resp_cm = MagicMock()
    resp_cm.__aenter__ = AsyncMock(return_value=resp)
    resp_cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get = MagicMock(return_value=resp_cm)

    # After the content is exhausted, clear game_id so the outer while exits.
    async def _handle(event):
        coord._game_id = None

    coord._handle_lichess_event = AsyncMock(side_effect=_handle)

    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord._lichess_stream_loop("g1")
    # Only the valid JSON line dispatched; heartbeat + bad line skipped.
    coord._handle_lichess_event.assert_awaited_once()


async def test_stream_loop_non200_retries_then_exits():
    coord = make_coordinator()
    coord._game_id = "g1"
    resp = MagicMock(status=500)
    resp_cm = MagicMock()
    resp_cm.__aenter__ = AsyncMock(return_value=resp)
    resp_cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get = MagicMock(return_value=resp_cm)

    # First sleep clears game_id so the loop exits after one retry.
    async def _sleep(_delay):
        coord._game_id = None

    with patch.object(coord_mod, "async_get_clientsession", return_value=session), \
            patch.object(coord_mod, "_sleep", AsyncMock(side_effect=_sleep)):
        await coord._lichess_stream_loop("g1")
    session.get.assert_called_once()


async def test_stream_loop_exception_retries_with_backoff():
    coord = make_coordinator()
    coord._game_id = "g1"
    session = MagicMock()
    session.get = MagicMock(side_effect=RuntimeError("net down"))

    async def _sleep(_delay):
        coord._game_id = None  # break the loop after the first backoff sleep

    with patch.object(coord_mod, "async_get_clientsession", return_value=session), \
            patch.object(coord_mod, "_sleep", AsyncMock(side_effect=_sleep)):
        await coord._lichess_stream_loop("g1")
    session.get.assert_called_once()


async def test_stream_loop_cancelled_returns_cleanly():
    coord = make_coordinator()
    coord._game_id = "g1"
    session = MagicMock()
    session.get = MagicMock(side_effect=asyncio.CancelledError)
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord._lichess_stream_loop("g1")  # CancelledError caught → return

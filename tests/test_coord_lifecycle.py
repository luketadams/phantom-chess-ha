"""Lifecycle / orchestration coverage for PhantomChessCoordinator.

Targets the game-start / apply-move / local-game orchestration methods:
``async_phantom_start_game``, ``async_phantom_apply_ai_move``,
``async_start_game``, ``async_start_local_game``, ``async_stop_local_game``,
``_push_move_to_local_ai``, ``_local_ai_turn``, ``_get_ai_move``,
``_replace_local_game_task``, ``_record_and_analyze_local_move``.

Every BLE / analysis / TTS sub-call is stubbed with an AsyncMock (or MagicMock)
on the instance so the tests exercise the pure orchestration logic — argument
validation, state mutations, branch selection and exception handling — without
touching hardware, aiohttp, or Stockfish.

Runs in the minimal matrix env (no Home Assistant, bleak absent). Uses the
shared ``ble_mock`` harness.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import chess
import pytest

from custom_components.phantom_chess import const
from custom_components.phantom_chess import coordinator as coord_mod
from custom_components.phantom_chess.coordinator import (
    BleakError,
)

from .ble_mock import make_coordinator


# ─────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────


def _quiet_announcements(coord) -> None:
    """Stub the TTS / announcement + analysis helpers used across start paths."""
    coord._announce_via_tts = AsyncMock()
    coord._analyze_starting_position = AsyncMock()
    coord._build_move_speech = MagicMock(return_value="")
    coord._should_announce_active_game = MagicMock(return_value=False)
    coord._post_move_event_speech = MagicMock(return_value="")
    coord._set_last_ai_move = MagicMock()
    coord._phantom_send_game_assistance = AsyncMock()
    coord._build_phantom_matrix_from_fen = MagicMock(return_value="." * 64)


# ─────────────────────────────────────────────────────────────────────────
# async_phantom_start_game
# ─────────────────────────────────────────────────────────────────────────


async def test_phantom_start_game_not_connected_raises():
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="BLE not connected"):
        await coord.async_phantom_start_game()


async def test_phantom_start_game_bad_side_raises():
    coord = make_coordinator(ble_connected=True)
    coord._phantom_execute_position = AsyncMock(return_value=True)
    with pytest.raises(ValueError, match="side must be"):
        await coord.async_phantom_start_game(side="X")


async def test_phantom_start_game_happy_path():
    coord = make_coordinator(ble_connected=True)
    coord._phantom_session_initialized = True
    coord._phantom_execute_position = AsyncMock(return_value=True)

    await coord.async_phantom_start_game(fen=chess.STARTING_FEN, side="b")

    # session flag reset, side upper-cased and forwarded to execute_position
    assert coord._phantom_session_initialized is False
    coord._phantom_execute_position.assert_awaited_once()
    kwargs = coord._phantom_execute_position.await_args.kwargs
    assert kwargs["side"] == "B"
    assert kwargs["select_chess_mode"] is True
    assert kwargs["side_opcode"] == "2"


async def test_phantom_start_game_timeout_warns_but_returns():
    coord = make_coordinator(ble_connected=True)
    coord._phantom_execute_position = AsyncMock(return_value=False)
    # Should not raise even when execute_position reports a move-done timeout.
    await coord.async_phantom_start_game()
    coord._phantom_execute_position.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────
# async_phantom_apply_ai_move
# ─────────────────────────────────────────────────────────────────────────


async def test_apply_ai_move_not_connected_raises():
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="BLE not connected"):
        await coord.async_phantom_apply_ai_move("e2e4")


async def test_apply_ai_move_short_uci_raises():
    coord = make_coordinator(ble_connected=True)
    with pytest.raises(ValueError, match="Invalid UCI move"):
        await coord.async_phantom_apply_ai_move("e2")


async def test_apply_ai_move_illegal_move_skips():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord._phantom_execute_position = AsyncMock(return_value=True)
    # a4a5 is not legal from the starting position.
    await coord.async_phantom_apply_ai_move("a4a5")
    coord._phantom_execute_position.assert_not_awaited()
    # board unchanged
    assert coord._board.fen() == chess.STARTING_FEN


async def test_apply_ai_move_happy_path_updates_state():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord._our_color = chess.WHITE
    coord._phantom_execute_position = AsyncMock(return_value=True)

    await coord.async_phantom_apply_ai_move("e2e4")

    # move pushed onto board
    assert coord._board.peek().uci() == "e2e4"
    # execute_position called with board-player side + SIDE "1"
    kwargs = coord._phantom_execute_position.await_args.kwargs
    assert kwargs["side"] == "W"
    assert kwargs["side_opcode"] == "1"
    # derived state updated
    assert coord._state["last_move"] == "e2e4"
    assert coord._state["live_fen"] == coord._board.board_fen()
    coord.async_set_updated_data.assert_called()
    coord._set_last_ai_move.assert_called_once()


async def test_apply_ai_move_black_board_player_side():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord._our_color = chess.BLACK
    coord._phantom_execute_position = AsyncMock(return_value=True)
    await coord.async_phantom_apply_ai_move("e2e4")
    assert coord._phantom_execute_position.await_args.kwargs["side"] == "B"


async def test_apply_ai_move_announces_when_active():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord._our_color = chess.WHITE
    coord._should_announce_active_game = MagicMock(return_value=True)
    coord._build_move_speech = MagicMock(return_value="Pawn to e4")
    coord._post_move_event_speech = MagicMock(return_value="")
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    coord._phantom_execute_position = AsyncMock(return_value=True)

    await coord.async_phantom_apply_ai_move("e2e4")
    # a TTS task was scheduled
    assert coord.hass.async_create_task.called


async def test_apply_ai_move_move_done_timeout_still_updates():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord._our_color = chess.WHITE
    # ok=False means move-done timeout — but the loop still breaks cleanly and
    # state is updated (no raise).
    coord._phantom_execute_position = AsyncMock(return_value=False)
    await coord.async_phantom_apply_ai_move("e2e4")
    assert coord._state["last_move"] == "e2e4"


async def test_apply_ai_move_retries_then_raises_and_notifies():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord._our_color = chess.WHITE
    # Always raises → both attempts fail → RuntimeError, board NOT rolled back.
    coord._phantom_execute_position = AsyncMock(side_effect=BleakError("boom"))
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    coord.hass.services = MagicMock()
    coord.hass.services.async_call = AsyncMock()

    # patch the backoff sleep so the retry is instant
    slept = []

    async def _sleep(sec):
        slept.append(sec)

    orig = coord_mod._sleep
    coord_mod._sleep = _sleep
    try:
        with pytest.raises(RuntimeError, match="apply_ai_move BLE write failed"):
            await coord.async_phantom_apply_ai_move("e2e4")
    finally:
        coord_mod._sleep = orig

    # two attempts → one backoff sleep between them
    assert coord._phantom_execute_position.await_count == 2
    assert len(slept) == 1
    # board keeps the move (deliberately NOT rolled back)
    assert coord._board.peek().uci() == "e2e4"
    # a persistent_notification task was scheduled
    assert coord.hass.async_create_task.called


# ─────────────────────────────────────────────────────────────────────────
# async_start_game (Lichess)
# ─────────────────────────────────────────────────────────────────────────


async def test_start_game_not_connected_raises():
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="Board not connected"):
        await coord.async_start_game()


class _FakeResp:
    def __init__(self, status, json_data=None, text_data=""):
        self.status = status
        self._json = json_data or {}
        self._text = text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._resp


def _patch_session(coord, resp):
    session = _FakeSession(resp)
    coord_mod.async_get_clientsession = MagicMock(return_value=session)
    return session


async def test_start_game_happy_path(monkeypatch):
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord._ble_write = AsyncMock()
    coord._phantom_execute_position = AsyncMock(return_value=True)
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    coord._lichess_stream_loop = MagicMock()  # never awaited (task creation stubbed)
    coord._lichess_task_done_cb = MagicMock()

    # Stub task creation so the stream loop never actually runs.
    created = []

    def _create_task(coro, name=None):
        try:
            coro.close()
        except Exception:
            pass
        # Emulate the stream loop parsing the gameFull event and setting our
        # color, so the poll loop below picks WHITE → SIDE opcode "1".
        coord._our_color = chess.WHITE
        t = MagicMock()
        t.done.return_value = False
        t.add_done_callback = MagicMock()
        created.append((coro, name))
        return t

    coord.hass.loop = MagicMock()
    coord.hass.loop.create_task = MagicMock(side_effect=_create_task)

    resp = _FakeResp(201, json_data={"id": "abc123"})
    session = _patch_session(coord, resp)

    orig_get = coord_mod.async_get_clientsession
    try:
        await coord.async_start_game(clock_limit_seconds=600, clock_increment_seconds=5)
    finally:
        coord_mod.async_get_clientsession = orig_get

    # game id captured + state marked playing
    assert coord._game_id == "abc123"
    assert coord._state["game_status"] == const.STATUS_PLAYING
    assert coord._state["lichess_game_id"] == "abc123"
    # SELECT_MODE write happened
    coord._ble_write.assert_any_await(const.UUID_SELECT_MODE, str(const.MODE_CHESS_PLAY).encode())
    # clock params forwarded in payload
    _url, kwargs = session.calls[0]
    assert kwargs["data"]["clock.limit"] == 600
    assert kwargs["data"]["clock.increment"] == 5
    # white → SIDE opcode "1"
    assert coord._phantom_execute_position.await_args.kwargs["side_opcode"] == "1"


async def test_start_game_lichess_error_raises():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord._ble_write = AsyncMock()
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())

    resp = _FakeResp(400, text_data="Invalid clock")
    _patch_session(coord, resp)

    orig_get = coord_mod.async_get_clientsession
    try:
        with pytest.raises(RuntimeError, match="Lichess challenge failed"):
            await coord.async_start_game()
    finally:
        coord_mod.async_get_clientsession = orig_get


async def test_start_game_black_uses_side_opcode_2():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord._ble_write = AsyncMock()
    coord._phantom_execute_position = AsyncMock(return_value=True)
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())

    def _create_task(coro, name=None):
        try:
            coro.close()
        except Exception:
            pass
        # Stream sets us to BLACK → AI moves first → SIDE opcode "2".
        coord._our_color = chess.BLACK
        t = MagicMock()
        t.done.return_value = False
        t.add_done_callback = MagicMock()
        return t

    coord.hass.loop = MagicMock()
    coord.hass.loop.create_task = MagicMock(side_effect=_create_task)

    resp = _FakeResp(200, json_data={"id": "xyz"})
    _patch_session(coord, resp)

    orig_get = coord_mod.async_get_clientsession
    try:
        await coord.async_start_game()
    finally:
        coord_mod.async_get_clientsession = orig_get

    assert coord._phantom_execute_position.await_args.kwargs["side_opcode"] == "2"


async def test_start_game_stream_slow_falls_back_to_preference():
    """our_color never resolves → fallback uses player_color; black → "2"."""
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord.player_color = "black"
    coord._ble_write = AsyncMock()
    coord._phantom_execute_position = AsyncMock(return_value=True)
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())

    def _create_task(coro, name=None):
        try:
            coro.close()
        except Exception:
            pass
        t = MagicMock()
        t.done.return_value = False
        t.add_done_callback = MagicMock()
        return t

    coord.hass.loop = MagicMock()
    coord.hass.loop.create_task = MagicMock(side_effect=_create_task)

    resp = _FakeResp(200, json_data={"id": "slow"})
    _patch_session(coord, resp)

    orig_get = coord_mod.async_get_clientsession
    try:
        await coord.async_start_game()
    finally:
        coord_mod.async_get_clientsession = orig_get

    # our_color stayed None → fallback branch: player_color "black" → "2"
    assert coord._phantom_execute_position.await_args.kwargs["side_opcode"] == "2"


async def test_start_game_execute_position_failure_is_swallowed():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord._ble_write = AsyncMock()
    coord._phantom_execute_position = AsyncMock(side_effect=RuntimeError("fw fail"))
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())

    def _create_task(coro, name=None):
        try:
            coro.close()
        except Exception:
            pass
        t = MagicMock()
        t.done.return_value = False
        t.add_done_callback = MagicMock()
        return t

    coord.hass.loop = MagicMock()
    coord.hass.loop.create_task = MagicMock(side_effect=_create_task)

    resp = _FakeResp(200, json_data={"id": "q"})
    _patch_session(coord, resp)
    coord._our_color = chess.WHITE

    orig_get = coord_mod.async_get_clientsession
    try:
        # execute_position raising must NOT propagate (warning-logged only).
        await coord.async_start_game()
    finally:
        coord_mod.async_get_clientsession = orig_get
    assert coord._game_id == "q"


# ─────────────────────────────────────────────────────────────────────────
# async_start_local_game
# ─────────────────────────────────────────────────────────────────────────


async def test_start_local_game_not_connected_raises():
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="Board not connected"):
        await coord.async_start_local_game()


async def test_start_local_game_white_no_ai_first():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord.player_color = "white"
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    coord.async_phantom_start_game = AsyncMock()
    coord._replace_local_game_task = AsyncMock()

    await coord.async_start_local_game()

    assert coord._local_game_active is True
    assert coord._our_color == chess.WHITE
    assert coord._state["game_status"] == const.STATUS_PLAYING
    assert coord._state["lichess_game_id"] == "local"
    assert coord._state["local_game_active"] is True
    # white → we move first → no AI-first task
    coord._replace_local_game_task.assert_not_awaited()
    # start_game called with side_opcode "1"
    assert coord.async_phantom_start_game.await_args.kwargs["side_opcode"] == "1"


async def test_start_local_game_black_ai_first():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord.player_color = "black"
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    coord.async_phantom_start_game = AsyncMock()
    coord._replace_local_game_task = AsyncMock()

    await coord.async_start_local_game()

    assert coord._our_color == chess.BLACK
    # black → AI first → replacement task kicked off, side_opcode "2"
    coord._replace_local_game_task.assert_awaited_once()
    assert coord.async_phantom_start_game.await_args.kwargs["side_opcode"] == "2"


async def test_start_local_game_game_assistance_failure_swallowed():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord.player_color = "white"
    coord._phantom_send_game_assistance = AsyncMock(side_effect=RuntimeError("nope"))
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    coord.async_phantom_start_game = AsyncMock()
    coord._replace_local_game_task = AsyncMock()
    # GAME_ASSISTANCE failure is caught/logged; game still starts.
    await coord.async_start_local_game()
    assert coord._local_game_active is True


async def test_start_local_game_cancels_prior_lichess_task():
    coord = make_coordinator(ble_connected=True)
    _quiet_announcements(coord)
    coord.player_color = "white"
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    coord.async_phantom_start_game = AsyncMock()
    coord._replace_local_game_task = AsyncMock()
    prior = MagicMock()
    prior.done.return_value = False
    coord._lichess_task = prior
    await coord.async_start_local_game()
    prior.cancel.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────
# async_stop_local_game
# ─────────────────────────────────────────────────────────────────────────


async def test_stop_local_game_clears_flags_and_writes_idle():
    coord = make_coordinator(ble_connected=True)
    coord._local_game_active = True
    coord._ai_vs_ai_active = True
    coord._sculpture_active = True
    coord._game_id = "abc"
    coord._ble_write = AsyncMock()

    await coord.async_stop_local_game()

    assert coord._local_game_active is False
    assert coord._ai_vs_ai_active is False
    assert coord._sculpture_active is False
    assert coord._state["game_status"] == const.STATUS_IDLE
    assert coord._state["lichess_game_id"] is None
    assert coord._state["local_game_active"] is False
    assert coord._game_id is None
    coord._ble_write.assert_awaited_once_with(const.UUID_SELECT_MODE, b"3")


async def test_stop_local_game_cancels_task():
    coord = make_coordinator(ble_connected=True)
    coord._ble_write = AsyncMock()
    task = MagicMock()
    task.done.return_value = False
    coord._local_game_task = task
    await coord.async_stop_local_game()
    task.cancel.assert_called_once()


async def test_stop_local_game_ble_write_failure_swallowed():
    coord = make_coordinator(ble_connected=True)
    coord._ble_write = AsyncMock(side_effect=RuntimeError("disconnected"))
    # The final SELECT_MODE write is best-effort; failure must not propagate.
    await coord.async_stop_local_game()
    assert coord._state["game_status"] == const.STATUS_IDLE


# ─────────────────────────────────────────────────────────────────────────
# _push_move_to_local_ai
# ─────────────────────────────────────────────────────────────────────────


async def test_push_move_inactive_returns():
    coord = make_coordinator(ble_connected=True)
    coord._local_game_active = False
    coord._replace_local_game_task = AsyncMock()
    await coord._push_move_to_local_ai("e2e4")
    coord._replace_local_game_task.assert_not_awaited()
    assert coord._board.fen() == chess.STARTING_FEN


async def test_push_move_invalid_uci_returns():
    coord = make_coordinator(ble_connected=True)
    coord._local_game_active = True
    coord._replace_local_game_task = AsyncMock()
    await coord._push_move_to_local_ai("garbage")
    coord._replace_local_game_task.assert_not_awaited()


async def test_push_move_illegal_move_rejects_via_ble():
    coord = make_coordinator(ble_connected=True)
    coord._local_game_active = True
    coord._ble_write = AsyncMock()
    coord._replace_local_game_task = AsyncMock()
    # e2e5 is a well-formed UCI but illegal from start.
    await coord._push_move_to_local_ai("e2e5")
    coord._ble_write.assert_awaited_once_with(const.UUID_GAME, b"\x032")
    coord._replace_local_game_task.assert_not_awaited()


async def test_push_move_illegal_move_ble_reject_failure_swallowed():
    coord = make_coordinator(ble_connected=True)
    coord._local_game_active = True
    coord._ble_write = AsyncMock(side_effect=RuntimeError("gone"))
    coord._replace_local_game_task = AsyncMock()
    # Rejection write failing must not raise.
    await coord._push_move_to_local_ai("e2e5")
    coord._replace_local_game_task.assert_not_awaited()


async def test_push_move_legal_schedules_ai_turn():
    coord = make_coordinator(ble_connected=True)
    coord._local_game_active = True
    coord._record_and_analyze_local_move = MagicMock()
    coord._replace_local_game_task = AsyncMock()
    await coord._push_move_to_local_ai("e2e4")
    assert coord._board.peek().uci() == "e2e4"
    assert coord._state["last_move"] == "e2e4"
    assert coord._state["game_status"] == const.STATUS_PLAYING
    coord._record_and_analyze_local_move.assert_called_once()
    coord._replace_local_game_task.assert_awaited_once()


async def test_push_move_checkmate_ends_game():
    coord = make_coordinator(ble_connected=True)
    coord._local_game_active = True
    coord._record_and_analyze_local_move = MagicMock()
    coord._replace_local_game_task = AsyncMock()
    # Fool's mate: 1. f3 e5 2. g4 Qh4#
    coord._board = chess.Board()
    for uci in ("f2f3", "e7e5", "g2g4"):
        coord._board.push(chess.Move.from_uci(uci))
    await coord._push_move_to_local_ai("d8h4")  # Qh4#
    assert coord._state["game_status"] == const.STATUS_CHECKMATE
    assert coord._local_game_active is False
    assert coord._state["local_game_active"] is False
    coord._replace_local_game_task.assert_not_awaited()


async def test_push_move_stalemate_ends_game():
    coord = make_coordinator(ble_connected=True)
    coord._local_game_active = True
    coord._record_and_analyze_local_move = MagicMock()
    coord._replace_local_game_task = AsyncMock()
    # Classic stalemate: black to move after ...  set up one move short.
    # Position: white Kc6, Qc7... use a known one-move-to-stalemate FEN.
    # Black king h8, white queen g6, white king f7 → Kg6-g7? Use FEN with
    # black to move being stalemated after white's move. Simpler: build a
    # position where white's move produces stalemate.
    # FEN: black king a8, white king c6, white queen b1; white plays Qb6 → stalemate.
    coord._board = chess.Board("k7/8/2K5/8/8/8/8/1Q6 w - - 0 1")
    await coord._push_move_to_local_ai("b1b6")  # stalemate
    assert coord._state["game_status"] == const.STATUS_DRAW
    assert coord._local_game_active is False


# ─────────────────────────────────────────────────────────────────────────
# _replace_local_game_task
# ─────────────────────────────────────────────────────────────────────────


async def test_replace_local_game_task_creates_task():
    coord = make_coordinator(ble_connected=True)
    coord._local_ai_turn = AsyncMock()
    coord._local_game_task = None
    # real loop from make_hass
    coord.hass.loop = asyncio.get_event_loop()

    await coord._replace_local_game_task(name="test_task")
    assert coord._local_game_task is not None
    assert coord._local_game_task_lock is not None
    # let the created task run to completion + cleanup
    await coord._local_game_task
    coord._local_ai_turn.assert_called()


async def test_replace_local_game_task_cancels_old():
    coord = make_coordinator(ble_connected=True)
    coord.hass.loop = asyncio.get_event_loop()

    started = asyncio.Event()

    async def _long_turn():
        started.set()
        await asyncio.sleep(100)

    # first turn
    coord._local_ai_turn = _long_turn
    await coord._replace_local_game_task(name="first")
    old = coord._local_game_task
    await started.wait()

    # replace with a quick second turn — old must be cancelled + awaited.
    coord._local_ai_turn = AsyncMock()
    await coord._replace_local_game_task(name="second")
    assert old.cancelled() or old.done()
    assert coord._local_game_task is not old
    await coord._local_game_task


async def test_replace_local_game_task_old_raises_is_logged():
    coord = make_coordinator(ble_connected=True)
    coord.hass.loop = asyncio.get_event_loop()

    started = asyncio.Event()

    async def _raising_turn():
        started.set()
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            # Convert the cancellation into a normal exception so the helper's
            # `except Exception` branch (not the CancelledError branch) runs.
            raise RuntimeError("cleanup boom") from None

    coord._local_ai_turn = _raising_turn
    await coord._replace_local_game_task(name="first")
    await started.wait()

    coord._local_ai_turn = AsyncMock()
    # The old task raises a non-cancel error on cancellation → logged,
    # not propagated. The replacement still starts.
    await coord._replace_local_game_task(name="second")
    assert coord._local_game_task is not None
    await coord._local_game_task


# ─────────────────────────────────────────────────────────────────────────
# _local_ai_turn
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Make the coordinator module's _sleep instant for loop paths."""
    async def _instant(_s):
        return None

    monkeypatch.setattr(coord_mod, "_sleep", _instant)


async def test_local_ai_turn_no_move_returns():
    coord = make_coordinator(ble_connected=True)
    coord._get_ai_move = AsyncMock(return_value=None)
    coord.async_phantom_apply_ai_move = AsyncMock()
    await coord._local_ai_turn()
    coord.async_phantom_apply_ai_move.assert_not_awaited()


async def test_local_ai_turn_invalid_uci_returns():
    coord = make_coordinator(ble_connected=True)
    coord._get_ai_move = AsyncMock(return_value="notauci")
    coord.async_phantom_apply_ai_move = AsyncMock()
    await coord._local_ai_turn()
    coord.async_phantom_apply_ai_move.assert_not_awaited()


async def test_local_ai_turn_illegal_move_returns():
    coord = make_coordinator(ble_connected=True)
    coord._get_ai_move = AsyncMock(return_value="e2e5")  # illegal from start
    coord.async_phantom_apply_ai_move = AsyncMock()
    await coord._local_ai_turn()
    coord.async_phantom_apply_ai_move.assert_not_awaited()


async def test_local_ai_turn_happy_path():
    coord = make_coordinator(ble_connected=True)
    coord._get_ai_move = AsyncMock(return_value="e2e4")
    coord._record_and_analyze_local_move = MagicMock()

    async def _apply(uci):
        coord._board.push(chess.Move.from_uci(uci))

    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    await coord._local_ai_turn()
    coord.async_phantom_apply_ai_move.assert_awaited_once_with("e2e4")
    coord._record_and_analyze_local_move.assert_called_once()
    assert coord._state["game_status"] == const.STATUS_PLAYING


async def test_local_ai_turn_apply_raises_fallback_push():
    coord = make_coordinator(ble_connected=True)
    coord._get_ai_move = AsyncMock(return_value="e2e4")
    coord._record_and_analyze_local_move = MagicMock()
    # apply raises BUT does not push → fallback push runs (move legal).
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=RuntimeError("BLE"))
    await coord._local_ai_turn()
    assert coord._board.peek().uci() == "e2e4"
    assert coord._state["last_move"] == "e2e4"
    coord._record_and_analyze_local_move.assert_called_once()


async def test_local_ai_turn_apply_raises_move_not_landed():
    coord = make_coordinator(ble_connected=True)
    coord._get_ai_move = AsyncMock(return_value="e2e4")
    coord._record_and_analyze_local_move = MagicMock()

    async def _apply_then_mutate(uci):
        # simulate a race: apply pushes e2e4 then raises; the move is now
        # NOT legal to push again → fallback skipped, bookkeeping skipped.
        coord._board.push(chess.Move.from_uci(uci))
        raise RuntimeError("post-push BLE fail")

    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply_then_mutate)
    await coord._local_ai_turn()
    # analysis pipeline NOT run because move_landed stayed False in the
    # fallback branch (move already on board, not legal to re-push).
    coord._record_and_analyze_local_move.assert_not_called()


async def test_local_ai_turn_checkmate_status():
    coord = make_coordinator(ble_connected=True)
    coord._record_and_analyze_local_move = MagicMock()
    # Fool's-mate setup, AI (black) delivers Qh4#.
    coord._board = chess.Board()
    for uci in ("f2f3", "e7e5", "g2g4"):
        coord._board.push(chess.Move.from_uci(uci))
    coord._get_ai_move = AsyncMock(return_value="d8h4")

    async def _apply(uci):
        coord._board.push(chess.Move.from_uci(uci))

    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    await coord._local_ai_turn()
    assert coord._state["game_status"] == const.STATUS_CHECKMATE
    assert coord._local_game_active is False


async def test_local_ai_turn_check_status():
    coord = make_coordinator(ble_connected=True)
    coord._record_and_analyze_local_move = MagicMock()
    # White plays Bb5+ giving check (not mate): d7 is empty (black pawn on d5)
    # so the f1 bishop checks the e8 king along b5-c6-d7-e8.
    coord._board = chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 1")
    coord._get_ai_move = AsyncMock(return_value="f1b5")

    async def _apply(uci):
        coord._board.push(chess.Move.from_uci(uci))

    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    await coord._local_ai_turn()
    assert coord._state["game_status"] == "check"


# ─────────────────────────────────────────────────────────────────────────
# _get_ai_move
# ─────────────────────────────────────────────────────────────────────────


async def test_get_ai_move_via_stockfish():
    coord = make_coordinator(ble_connected=True)
    coord._analysis_client = MagicMock()
    coord._analysis_client.best_move_for_ai_level = AsyncMock(return_value="d2d4")
    uci = await coord._get_ai_move(chess.Board())
    assert uci == "d2d4"


async def test_get_ai_move_stockfish_error_falls_to_cloud():
    coord = make_coordinator(ble_connected=True)
    coord._analysis_client = MagicMock()
    coord._analysis_client.best_move_for_ai_level = AsyncMock(
        side_effect=RuntimeError("engine gone")
    )
    resp = _FakeResp(200, json_data={"pvs": [{"moves": "e2e4 e7e5"}]})

    class _GetSession:
        def get(self, url, **kwargs):
            return resp

    coord_mod.async_get_clientsession = MagicMock(return_value=_GetSession())
    orig = coord_mod.async_get_clientsession
    try:
        uci = await coord._get_ai_move(chess.Board())
    finally:
        coord_mod.async_get_clientsession = orig
    assert uci == "e2e4"


async def test_get_ai_move_cloud_empty_falls_to_random():
    coord = make_coordinator(ble_connected=True)
    coord._analysis_client = None
    resp = _FakeResp(200, json_data={"pvs": []})

    class _GetSession:
        def get(self, url, **kwargs):
            return resp

    coord_mod.async_get_clientsession = MagicMock(return_value=_GetSession())
    orig = coord_mod.async_get_clientsession
    try:
        board = chess.Board()
        uci = await coord._get_ai_move(board)
    finally:
        coord_mod.async_get_clientsession = orig
    # random legal move
    assert chess.Move.from_uci(uci) in board.legal_moves


async def test_get_ai_move_cloud_exception_falls_to_random():
    coord = make_coordinator(ble_connected=True)
    coord._analysis_client = None

    class _GetSession:
        def get(self, url, **kwargs):
            raise RuntimeError("network down")

    coord_mod.async_get_clientsession = MagicMock(return_value=_GetSession())
    orig = coord_mod.async_get_clientsession
    try:
        board = chess.Board()
        uci = await coord._get_ai_move(board)
    finally:
        coord_mod.async_get_clientsession = orig
    assert chess.Move.from_uci(uci) in board.legal_moves


async def test_get_ai_move_no_legal_returns_none():
    coord = make_coordinator(ble_connected=True)
    coord._analysis_client = None

    class _GetSession:
        def get(self, url, **kwargs):
            raise RuntimeError("down")

    coord_mod.async_get_clientsession = MagicMock(return_value=_GetSession())
    orig = coord_mod.async_get_clientsession
    try:
        # checkmate position → no legal moves.
        board = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
        uci = await coord._get_ai_move(board)
    finally:
        coord_mod.async_get_clientsession = orig
    assert uci is None


# ─────────────────────────────────────────────────────────────────────────
# _record_and_analyze_local_move
# ─────────────────────────────────────────────────────────────────────────


async def test_record_and_analyze_happy_path():
    coord = make_coordinator(ble_connected=True)
    coord._record_history_stub = MagicMock(return_value=0)
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    coord._analyze_move = MagicMock()
    move = chess.Move.from_uci("e2e4")
    coord._record_and_analyze_local_move(move, mover_is_white=True)
    coord._record_history_stub.assert_called_once()
    # analysis-board advanced
    assert coord._analysis_board.peek().uci() == "e2e4"
    coord.hass.async_create_task.assert_called_once()


async def test_record_and_analyze_history_stub_failure_returns():
    coord = make_coordinator(ble_connected=True)
    coord._record_history_stub = MagicMock(side_effect=RuntimeError("stub fail"))
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    move = chess.Move.from_uci("e2e4")
    coord._record_and_analyze_local_move(move, mover_is_white=True)
    # bailed before scheduling analysis
    coord.hass.async_create_task.assert_not_called()


async def test_record_and_analyze_illegal_on_analysis_board():
    coord = make_coordinator(ble_connected=True)
    coord._record_history_stub = MagicMock(return_value=2)
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    coord._analyze_move = MagicMock()
    # a move illegal on the (starting) analysis board → not pushed, but the
    # analysis task is still scheduled with before==after boards.
    move = chess.Move.from_uci("e2e5")
    coord._record_and_analyze_local_move(move, mover_is_white=True)
    assert coord._analysis_board.fen() == chess.STARTING_FEN
    coord.hass.async_create_task.assert_called_once()


async def test_record_and_analyze_clears_two_player_out_of_sync():
    coord = make_coordinator(ble_connected=True)
    coord._two_player_active = True
    coord._state["two_player_out_of_sync"] = True
    coord._clear_two_player_out_of_sync = MagicMock()
    coord._record_history_stub = MagicMock(return_value=0)
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro, **k: coro.close())
    coord._analyze_move = MagicMock()
    coord._record_and_analyze_local_move(chess.Move.from_uci("e2e4"), mover_is_white=True)
    coord._clear_two_player_out_of_sync.assert_called_once()

"""Service-orchestration + lifecycle coverage for PhantomChessCoordinator.

Targets the async service entry points (send_move, dashboard_move, takeback,
resign, resync_detection, reset_position, pause/sound/speed, back_to_modes),
the setup/shutdown/update lifecycle, the fw0.3.2 GAME_START diagnostics, the
GATT-staleness discriminator, the mismatch-notification create/clear fan-out,
and the debug-dump executor helpers.

These are the parts of coordinator.py that the pure-state-mutator tests in
test_coordinator_state.py / test_matrix_error_state.py don't reach: the
methods here mostly ORCHESTRATE (validate → call a sender/HTTP → mutate state),
so we stub the leaf senders (``_phantom_*``, ``_push_move_*``,
``async_phantom_apply_ai_move``) with AsyncMocks and assert the orchestration,
or drive a real ``FakeBleakClient`` and assert the wire bytes.

Run:
    pytest tests/test_coord_services.py -q
"""
from __future__ import annotations

import asyncio

import chess
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.phantom_chess import const
from custom_components.phantom_chess import coordinator as coord_mod
from custom_components.phantom_chess.coordinator import BleakError, PhantomChessCoordinator

from .ble_mock import FakeBleakClient, make_coordinator

pytestmark = pytest.mark.asyncio


# ─────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────


def _drain_tasks(coord: PhantomChessCoordinator) -> None:
    """Give hass.async_create_task a real, coroutine-closing implementation so
    the mismatch/tts fan-out doesn't leak un-awaited-coroutine warnings.
    """
    def _run(coro):
        # Close the coroutine so it doesn't warn; we only care that the
        # service method attempted to schedule it.
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    coord.hass.async_create_task = MagicMock(side_effect=_run)


# ─────────────────────────────────────────────────────────────────────────
# async_setup / async_shutdown / _async_update_data
# ─────────────────────────────────────────────────────────────────────────


async def test_async_setup_creates_analysis_client_and_ble_task() -> None:
    coord = make_coordinator()
    coord._analysis_client = None
    coord._ble_task = None
    # Give the coordinator a fully synthetic hass whose .loop is a MagicMock —
    # do NOT touch the real event loop pytest-asyncio runs the test on.
    fake_hass = MagicMock()
    fake_hass.config.path = MagicMock(return_value="/tmp/pc_cfg")

    def _capture_task(coro, name=None):
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    fake_hass.loop.create_task = MagicMock(side_effect=_capture_task)
    coord.hass = fake_hass
    with patch(
        "custom_components.phantom_chess.lichess_analysis.LichessAnalysisClient"
    ):
        await coord.async_setup()
    assert coord._analysis_client is not None
    # A BLE task was scheduled on the loop.
    fake_hass.loop.create_task.assert_called_once()
    assert not coord._stop_event.is_set()


async def test_async_shutdown_cancels_tasks_and_disconnects() -> None:
    coord = make_coordinator()

    async def _sleeper():
        await asyncio.sleep(60)

    coord._ble_task = asyncio.create_task(_sleeper())
    coord._lichess_task = asyncio.create_task(_sleeper())
    coord._matrix_poll_task = None
    client = FakeBleakClient()
    client.is_connected = True
    coord._ble_client = client
    coord._analysis_client = MagicMock()
    coord._analysis_client.shutdown = AsyncMock()

    await coord.async_shutdown()

    assert coord._stop_event.is_set()
    assert coord._ble_task.cancelled() or coord._ble_task.done()
    assert client.is_connected is False
    coord._analysis_client.shutdown.assert_awaited_once()


async def test_async_shutdown_tolerates_none_tasks_and_no_client() -> None:
    coord = make_coordinator(client=None)
    coord._ble_task = None
    coord._lichess_task = None
    coord._matrix_poll_task = None
    coord._ble_client = None
    coord._analysis_client = None
    # Must not raise.
    await coord.async_shutdown()
    assert coord._stop_event.is_set()


async def test_async_shutdown_swallows_analysis_shutdown_error() -> None:
    coord = make_coordinator(client=None)
    coord._ble_task = None
    coord._lichess_task = None
    coord._matrix_poll_task = None
    coord._ble_client = None
    coord._analysis_client = MagicMock()
    coord._analysis_client.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
    await coord.async_shutdown()  # no raise


async def test_async_update_data_returns_cached_state() -> None:
    coord = make_coordinator()
    coord._state = {"foo": "bar"}
    out = await coord._async_update_data()
    assert out is coord._state


# ─────────────────────────────────────────────────────────────────────────
# async_send_move
# ─────────────────────────────────────────────────────────────────────────


async def test_send_move_routes_to_local_ai_when_local_active() -> None:
    coord = make_coordinator()
    coord._local_game_active = True
    coord._push_move_to_local_ai = AsyncMock()
    coord._push_move_to_lichess = AsyncMock()
    await coord.async_send_move("e2e4")
    coord._push_move_to_local_ai.assert_awaited_once_with("e2e4")
    coord._push_move_to_lichess.assert_not_awaited()


async def test_send_move_routes_to_lichess_when_not_local() -> None:
    coord = make_coordinator()
    coord._local_game_active = False
    coord._push_move_to_local_ai = AsyncMock()
    coord._push_move_to_lichess = AsyncMock()
    await coord.async_send_move("e2e4")
    coord._push_move_to_lichess.assert_awaited_once_with("e2e4")
    coord._push_move_to_local_ai.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# async_execute_dashboard_move — validation + orchestration
# ─────────────────────────────────────────────────────────────────────────


async def test_dashboard_move_bad_promotion_raises() -> None:
    coord = make_coordinator()
    with pytest.raises(ValueError, match="Invalid promotion"):
        await coord.async_execute_dashboard_move("e7e8", promotion="k")


async def test_dashboard_move_promotion_disagreement_raises() -> None:
    coord = make_coordinator()
    with pytest.raises(ValueError, match="disagree"):
        await coord.async_execute_dashboard_move("e7e8q", promotion="r")


async def test_dashboard_move_promotion_appends_suffix() -> None:
    coord = make_coordinator()
    # White pawn on e7, promote to queen. Black king on a8 so e8 is free.
    coord._board = chess.Board("k7/4P3/8/8/8/8/8/4K3 w - - 0 1")
    coord._our_color = chess.WHITE
    coord._game_id = None
    coord._local_game_active = True
    coord._ble_connected = True
    coord.async_phantom_apply_ai_move = AsyncMock()
    coord._replace_local_game_task = AsyncMock()
    coord._record_and_analyze_local_move = MagicMock()
    _drain_tasks(coord)
    await coord.async_execute_dashboard_move("e7e8", promotion="q")
    # apply_ai_move received the suffixed UCI.
    coord.async_phantom_apply_ai_move.assert_awaited_once_with("e7e8q")


async def test_dashboard_move_invalid_uci_raises_value_error() -> None:
    coord = make_coordinator()
    with pytest.raises(ValueError, match="Invalid UCI move"):
        await coord.async_execute_dashboard_move("zzzz")


async def test_dashboard_move_no_active_game_raises_runtime() -> None:
    coord = make_coordinator()
    coord._local_game_active = False
    coord._game_id = None
    with pytest.raises(RuntimeError, match="No active game"):
        await coord.async_execute_dashboard_move("e2e4")


async def test_dashboard_move_illegal_move_raises_value_error() -> None:
    coord = make_coordinator()
    coord._local_game_active = True
    coord._board = chess.Board()
    with pytest.raises(ValueError, match="not legal"):
        await coord.async_execute_dashboard_move("e2e5")


async def test_dashboard_move_wrong_turn_raises_runtime() -> None:
    coord = make_coordinator()
    coord._local_game_active = True
    coord._board = chess.Board()  # white to move
    coord._our_color = chess.BLACK  # but it's not our turn
    with pytest.raises(RuntimeError, match="Not your turn"):
        await coord.async_execute_dashboard_move("e2e4")


async def test_dashboard_move_requires_ble_connected() -> None:
    coord = make_coordinator(ble_connected=False)
    coord._local_game_active = True
    coord._board = chess.Board()
    coord._our_color = chess.WHITE
    with pytest.raises(RuntimeError, match="Board not connected"):
        await coord.async_execute_dashboard_move("e2e4")


async def test_dashboard_move_lichess_backend_posts() -> None:
    coord = make_coordinator()
    coord._local_game_active = False
    coord._game_id = "abcd1234"
    coord._board = chess.Board()
    coord._our_color = chess.WHITE
    coord._ble_connected = True
    coord.async_phantom_apply_ai_move = AsyncMock()
    coord._push_move_to_lichess = AsyncMock()
    await coord.async_execute_dashboard_move("e2e4")
    coord.async_phantom_apply_ai_move.assert_awaited_once_with("e2e4")
    coord._push_move_to_lichess.assert_awaited_once_with("e2e4")


async def test_dashboard_move_local_playing_branch_triggers_ai() -> None:
    coord = make_coordinator()
    coord._local_game_active = True
    coord._game_id = None
    # A non-terminal move → the game continues and the AI turn is scheduled.
    coord._board = chess.Board()
    coord._our_color = chess.WHITE
    coord._ble_connected = True

    async def _apply(uci):
        coord._board.push_uci(uci)

    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    coord._record_and_analyze_local_move = MagicMock()
    coord._replace_local_game_task = AsyncMock()
    _drain_tasks(coord)
    await coord.async_execute_dashboard_move("e2e4")
    assert coord._state["game_status"] == const.STATUS_PLAYING
    coord._replace_local_game_task.assert_awaited_once()


async def test_dashboard_move_local_real_checkmate() -> None:
    coord = make_coordinator()
    coord._local_game_active = True
    coord._game_id = None
    # Back-rank mate: white rook delivers Ra8# (king g8, own pawns f7/g7/h7).
    coord._board = chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1")
    coord._our_color = chess.WHITE
    coord._ble_connected = True

    async def _apply(uci):
        coord._board.push_uci(uci)

    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    coord._record_and_analyze_local_move = MagicMock()
    coord._replace_local_game_task = AsyncMock()
    _drain_tasks(coord)
    await coord.async_execute_dashboard_move("a1a8")
    assert coord._board.is_checkmate()
    assert coord._state["game_status"] == const.STATUS_CHECKMATE
    assert coord._local_game_active is False
    coord._replace_local_game_task.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# async_takeback
# ─────────────────────────────────────────────────────────────────────────


async def test_takeback_requires_ble() -> None:
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="Board not connected"):
        await coord.async_takeback()


async def test_takeback_bad_count_raises() -> None:
    coord = make_coordinator()
    with pytest.raises(ValueError, match="count must be"):
        await coord.async_takeback(count=0)


async def test_takeback_local_game_writes_opcode5() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._game_id = None
    coord._board = chess.Board()
    coord._board.push_uci("e2e4")
    coord._board.push_uci("e7e5")
    coord._our_color = chess.WHITE
    await coord.async_takeback(count=1)
    payload = client.last_write_to(const.UUID_GAME)
    assert payload is not None
    assert payload[0] == 0x05  # TAKE_BACK opcode
    # count popped = 1, encoded as "1,<fen>,<side>"
    body = payload[1:].decode()
    assert body.startswith("1,")


async def test_takeback_no_moves_is_noop() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._game_id = None
    coord._board = chess.Board()  # no move stack
    coord._our_color = chess.WHITE
    await coord.async_takeback(count=1)
    assert client.last_write_to(const.UUID_GAME) is None


async def test_takeback_lichess_refusal_aborts_before_ble() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._game_id = "game123"
    coord._board = chess.Board()
    coord._board.push_uci("e2e4")

    # Mock the aiohttp session so the POST returns 400 (refused).
    resp = MagicMock()
    resp.status = 400
    resp.text = AsyncMock(return_value="not allowed")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_takeback(count=1)
    # No BLE write — board must have stayed at 1 move (not popped).
    assert client.last_write_to(const.UUID_GAME) is None
    assert len(coord._board.move_stack) == 1


async def test_takeback_lichess_accept_then_ble_write() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._game_id = "game123"
    coord._board = chess.Board()
    coord._board.push_uci("e2e4")
    coord._board.push_uci("e7e5")
    coord._our_color = chess.WHITE

    resp = MagicMock()
    resp.status = 200
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_takeback(count=1)
    payload = client.last_write_to(const.UUID_GAME)
    assert payload is not None and payload[0] == 0x05


async def test_takeback_lichess_exception_aborts() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._game_id = "game123"
    coord._board = chess.Board()
    coord._board.push_uci("e2e4")
    session = MagicMock()
    session.post = MagicMock(side_effect=RuntimeError("network down"))
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_takeback(count=1)
    assert client.last_write_to(const.UUID_GAME) is None
    assert len(coord._board.move_stack) == 1


async def test_takeback_ble_write_failure_propagates() -> None:
    client = FakeBleakClient()
    client.fail_write[const.UUID_GAME.lower()] = BleakError("write failed")
    coord = make_coordinator(client=client)
    coord._game_id = None
    coord._board = chess.Board()
    coord._board.push_uci("e2e4")
    coord._our_color = chess.WHITE
    with pytest.raises(BleakError):
        await coord.async_takeback(count=1)


# ─────────────────────────────────────────────────────────────────────────
# async_resign
# ─────────────────────────────────────────────────────────────────────────


async def test_resign_no_game_is_noop() -> None:
    coord = make_coordinator()
    coord._game_id = None
    # Should return immediately without touching the network.
    with patch.object(coord_mod, "async_get_clientsession") as sess:
        await coord.async_resign()
    sess.assert_not_called()


async def test_resign_success_clears_game() -> None:
    coord = make_coordinator()
    coord._game_id = "abc"
    resp = MagicMock()
    resp.status = 200
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_resign()
    assert coord._game_id is None
    assert coord._state["game_status"] == const.STATUS_RESIGNED


async def test_resign_failure_leaves_state() -> None:
    coord = make_coordinator()
    coord._game_id = "abc"
    coord._state["game_status"] = const.STATUS_PLAYING
    resp = MagicMock()
    resp.status = 500
    resp.text = AsyncMock(return_value="err")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_resign()
    # Game left intact.
    assert coord._game_id == "abc"
    assert coord._state["game_status"] == const.STATUS_PLAYING


# ─────────────────────────────────────────────────────────────────────────
# async_resync_detection
# ─────────────────────────────────────────────────────────────────────────


async def test_resync_detection_requires_ble() -> None:
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="Board not connected"):
        await coord.async_resync_detection()


async def test_resync_detection_sends_reset_and_dismisses() -> None:
    coord = make_coordinator()
    coord._board = chess.Board()
    coord._phantom_send_reset_detection = AsyncMock()
    coord.hass.services.async_call = AsyncMock()
    _drain_tasks(coord)
    await coord.async_resync_detection()
    coord._phantom_send_reset_detection.assert_awaited_once()
    # Best-effort dismiss of both notification IDs.
    assert coord.hass.services.async_call.await_count == 2


async def test_resync_detection_dismiss_errors_are_swallowed() -> None:
    coord = make_coordinator()
    coord._board = chess.Board()
    coord._phantom_send_reset_detection = AsyncMock()
    coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("nope"))
    _drain_tasks(coord)
    await coord.async_resync_detection()  # no raise
    coord._phantom_send_reset_detection.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────
# async_back_to_modes
# ─────────────────────────────────────────────────────────────────────────


async def test_back_to_modes_stops_local_loop_and_idles_firmware() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._sculpture_active = True
    coord._ai_vs_ai_active = False
    coord._local_game_active = True
    coord._local_game_task = None
    coord.async_reset_position = AsyncMock()
    await coord.async_back_to_modes()
    assert coord._sculpture_active is False
    assert coord._local_game_active is False
    assert coord.setup_mode == const.DEFAULT_SETUP_MODE
    assert coord._state["lichess_review_ready"] is False
    # Firmware idled via SELECT_MODE 3.
    assert client.last_write_to(const.UUID_SELECT_MODE) == b"3"
    coord.async_reset_position.assert_awaited_once()


async def test_back_to_modes_rehome_failure_is_swallowed() -> None:
    coord = make_coordinator()
    coord._sculpture_active = False
    coord._ai_vs_ai_active = False
    coord._local_game_active = False
    coord.async_reset_position = AsyncMock(side_effect=RuntimeError("ble gone"))
    await coord.async_back_to_modes()  # no raise
    assert coord.setup_mode == const.DEFAULT_SETUP_MODE


# ─────────────────────────────────────────────────────────────────────────
# async_reset_position
# ─────────────────────────────────────────────────────────────────────────


async def test_reset_position_requires_ble() -> None:
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="BLE not connected"):
        await coord.async_reset_position()


async def test_reset_position_resets_board_and_executes() -> None:
    coord = make_coordinator()
    coord._two_player_active = False
    coord._board = chess.Board()
    coord._board.push_uci("e2e4")
    coord._phantom_execute_position = AsyncMock(return_value=True)
    await coord.async_reset_position()
    assert coord._board.fen() == chess.Board().fen()
    assert coord._state["last_move"] is None
    assert coord._phantom_session_initialized is False
    coord._phantom_execute_position.assert_awaited_once()


async def test_reset_position_finalizes_two_player() -> None:
    coord = make_coordinator()
    coord._two_player_active = True
    coord._board = chess.Board()
    coord._finalize_two_player_game = AsyncMock()
    coord._phantom_execute_position = AsyncMock(return_value=True)
    await coord.async_reset_position()
    coord._finalize_two_player_game.assert_awaited_once()


async def test_reset_position_logs_on_timeout() -> None:
    coord = make_coordinator()
    coord._two_player_active = False
    coord._board = chess.Board()
    coord._phantom_execute_position = AsyncMock(return_value=False)
    # Should not raise even when the physical drive times out.
    await coord.async_reset_position()
    coord._phantom_execute_position.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────
# async_set_pause / async_play_sound / speed / sound_level  (wire assertions
# not duplicated from test_coordinator_state — here we cover the not-connected
# guards + resume path via the real FakeBleakClient)
# ─────────────────────────────────────────────────────────────────────────


async def test_set_pause_true_writes_mode_3_via_client() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord.async_set_pause(True)
    assert coord.paused is True
    assert coord._state["game_status"] == const.STATUS_PAUSED
    assert client.last_write_to(const.UUID_SELECT_MODE) == b"3"


async def test_set_pause_false_resumes_chess_play() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord.async_set_pause(False)
    assert coord.paused is False
    assert coord._state["game_status"] == const.STATUS_PLAYING
    assert client.last_write_to(const.UUID_SELECT_MODE) == str(
        const.MODE_CHESS_PLAY
    ).encode()


async def test_play_sound_requires_ble() -> None:
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="BLE not connected"):
        await coord.async_play_sound("check")


async def test_set_mechanism_speed_writes_wire_bytes() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord.async_set_mechanism_speed(4)
    assert coord.mechanism_speed == 4
    assert client.last_write_to(const.UUID_MECHANISM_SPEED) == b"4"


async def test_set_sound_level_writes_wire_bytes() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord.async_set_sound_level(20)
    assert coord.sound_level == 20
    assert client.last_write_to(const.UUID_SOUND_LEVEL) == b"20,11110,0"


# ─────────────────────────────────────────────────────────────────────────
# _handle_gatt_staleness — the known-uuid (force reconnect) vs unknown branch
# ─────────────────────────────────────────────────────────────────────────


async def test_gatt_staleness_known_uuid_forces_reconnect() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._discovered_uuids = {const.UUID_GAME.lower()}
    with patch.object(coord_mod, "async_delete_issue", MagicMock()):
        result = await coord._handle_gatt_staleness(
            Exception("Characteristic not found"), const.UUID_GAME, op="write"
        )
    assert result is True
    assert coord._ble_connected is False
    assert client.is_connected is False  # disconnect was called


async def test_gatt_staleness_unknown_uuid_returns_false() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._discovered_uuids = set()  # never discovered
    result = await coord._handle_gatt_staleness(
        Exception("Characteristic not found"), const.UUID_GAME, op="write"
    )
    assert result is False
    # No forced disconnect.
    assert client.is_connected is True


async def test_gatt_staleness_unrelated_error_returns_false() -> None:
    coord = make_coordinator()
    result = await coord._handle_gatt_staleness(
        Exception("some unrelated timeout"), const.UUID_GAME, op="read"
    )
    assert result is False


async def test_gatt_staleness_disconnect_error_swallowed() -> None:
    client = FakeBleakClient()

    async def _boom():
        raise RuntimeError("disconnect blew up")

    client.disconnect = _boom
    coord = make_coordinator(client=client)
    coord._discovered_uuids = {const.UUID_GAME.lower()}
    result = await coord._handle_gatt_staleness(
        Exception("Characteristic not found"), const.UUID_GAME, op="write"
    )
    # Staleness detected → still returns True even though disconnect raised.
    assert result is True


# ─────────────────────────────────────────────────────────────────────────
# mismatch-notification create/clear fan-out (uncovered branch of
# _update_mismatch_notification)
# ─────────────────────────────────────────────────────────────────────────


def _inconsistent_payload() -> dict:
    """A grid with a piece where the sensor reports empty (and vice-versa),
    guaranteeing _check_consistency returns inconsistent so the notification
    create branch fires.
    """
    # Piece on the grid at index 12 (a rook) but sensor bit is 0 there → diff.
    grid = list("." * 100)
    grid[12] = "r"
    sensor = list("0" * 100)
    # sensor sees a piece at index 13 that the grid says is empty.
    sensor[13] = "1"
    return {
        "raw": b"ERROR: mismatch,x,y",
        "piece_grid": "".join(grid),
        "sensor_bitmap": "".join(sensor),
        "status": "Error",
        "status_message": "Chessboard and sensor matrix do not match",
    }


async def test_mismatch_notification_creates_then_clears() -> None:
    coord = make_coordinator()
    coord._last_mismatch_signature = None
    coord.hass.services.async_call = AsyncMock()
    creates = []

    def _cap(coro):
        # Coroutine is hass.services.async_call(...); close it, record it.
        if asyncio.iscoroutine(coro):
            coro.close()
        creates.append(1)
        return MagicMock()

    coord.hass.async_create_task = MagicMock(side_effect=_cap)

    payload = _inconsistent_payload()
    coord._apply_matrix_state(payload)
    # A create was scheduled and a signature stored.
    assert coord._last_mismatch_signature is not None
    n_after_create = len(creates)
    assert n_after_create >= 1

    # Now a CLEAN/consistent payload → dismiss branch.
    coord._update_mismatch_notification("." * 100, "0" * 100, True)
    assert coord._last_mismatch_signature is None
    assert len(creates) > n_after_create


async def test_mismatch_notification_dedups_same_signature() -> None:
    coord = make_coordinator()
    coord._last_mismatch_signature = None
    scheduled = []
    coord.hass.async_create_task = MagicMock(
        side_effect=lambda coro: (coro.close() if asyncio.iscoroutine(coro) else None, scheduled.append(1))[1]
    )
    grid = "." * 100
    sensor = list("0" * 100)
    sensor[20] = "1"
    sensor = "".join(sensor)
    coord._update_mismatch_notification(grid, sensor, False)
    first = len(scheduled)
    # Same disagreement set again → no new schedule.
    coord._update_mismatch_notification(grid, sensor, False)
    assert len(scheduled) == first


async def test_mismatch_notification_consistent_without_prior_is_noop() -> None:
    coord = make_coordinator()
    coord._last_mismatch_signature = None
    coord.hass.async_create_task = MagicMock()
    coord._update_mismatch_notification("." * 100, "0" * 100, True)
    coord.hass.async_create_task.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# debug-dump executor helpers
# ─────────────────────────────────────────────────────────────────────────


def _entry_with_debug(enabled: bool) -> MagicMock:
    e = MagicMock()
    e.options = {"debug_dump": enabled}
    return e


async def test_debug_dump_disabled_is_noop(tmp_path) -> None:
    coord = make_coordinator(entry=_entry_with_debug(False))
    coord.hass.config.path = lambda *a: str(tmp_path.joinpath(*a))
    assert coord._debug_dump_enabled() is False
    coord._write_gatt_dump(["line\n"])
    coord._append_matrix_log("line\n")
    coord._write_char_values(["line\n"])
    # No files written.
    assert not list(tmp_path.rglob("*.txt"))


async def test_debug_dump_enabled_writes_files(tmp_path) -> None:
    coord = make_coordinator(entry=_entry_with_debug(True))
    coord.hass.config.path = lambda *a: str(tmp_path.joinpath(*a))
    assert coord._debug_dump_enabled() is True
    coord._write_gatt_dump(["=== gatt ===\n"])
    coord._append_matrix_log("matrixline\n")
    coord._write_char_values(["charval\n"])
    gatt = tmp_path / "phantom_chess" / "debug" / "gatt.txt"
    mlog = tmp_path / "phantom_chess" / "debug" / "matrix_log.txt"
    cval = tmp_path / "phantom_chess" / "debug" / "char_values.txt"
    assert gatt.read_text() == "=== gatt ===\n"
    assert mlog.read_text() == "matrixline\n"
    assert cval.read_text() == "charval\n"


async def test_debug_path_composes_under_debug_dir() -> None:
    coord = make_coordinator()
    coord.hass.config.path = lambda *a: "/".join(("/config",) + a)
    assert coord._debug_path("x.txt") == "/config/phantom_chess/debug/x.txt"


# ─────────────────────────────────────────────────────────────────────────
# async_diagnose_game_start / _diagnose_game_start_variants /
# _game_channel_write_diag
# ─────────────────────────────────────────────────────────────────────────


async def test_diagnose_game_start_requires_ble() -> None:
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="Board not connected"):
        await coord.async_diagnose_game_start()


async def test_diagnose_game_start_probe_ok() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._board = chess.Board()
    coord._phantom_send_reset_detection = AsyncMock()
    summary = await coord.async_diagnose_game_start(experimental=False)
    assert "RESET_DETECTION probe OK" in summary
    assert "0.3.2 diag" in summary
    coord._phantom_send_reset_detection.assert_awaited_once()


async def test_diagnose_game_start_probe_length_reject() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._board = chess.Board()
    coord._phantom_send_reset_detection = AsyncMock(
        side_effect=BleakError("INVALID_ATTRIBUTE_VALUE_LENGTH")
    )
    summary = await coord.async_diagnose_game_start(experimental=False)
    assert "RESET_DETECTION probe FAILED" in summary
    assert "0x0D length rejection" in summary


async def test_diagnose_game_start_probe_other_error() -> None:
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._board = chess.Board()
    coord._phantom_send_reset_detection = AsyncMock(
        side_effect=BleakError("something else broke")
    )
    summary = await coord.async_diagnose_game_start(experimental=False)
    assert "RESET_DETECTION probe FAILED" in summary
    assert "0x0D" not in summary or "length rejection" not in summary


async def test_diagnose_game_start_variant_ladder_first_ok() -> None:
    # A client that accepts writes → the first variant succeeds.
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._board = chess.Board()
    coord._phantom_send_reset_detection = AsyncMock()
    summary = await coord.async_diagnose_game_start(experimental=True)
    assert "VARIANT OK" in summary
    # Only the first variant (103B) was attempted before returning.
    game_writes = client.writes_to(const.UUID_GAME)
    assert any(len(w) == 103 for w in game_writes)


async def test_diagnose_game_start_variant_ladder_all_fail() -> None:
    client = FakeBleakClient()
    client.fail_write[const.UUID_GAME.lower()] = BleakError(
        "INVALID_ATTRIBUTE_VALUE_LENGTH"
    )
    coord = make_coordinator(client=client)
    coord._board = chess.Board()
    coord._phantom_send_reset_detection = AsyncMock()
    variants = await coord._diagnose_game_start_variants()
    # All three variants failed → escalation line present.
    assert any("attr_max_len regression" in line for line in variants)
    assert sum(1 for line in variants if "0x0D length-reject" in line) == 3


async def test_game_channel_write_diag_no_client() -> None:
    coord = make_coordinator(client=None)
    coord._ble_client = None
    s = coord._game_channel_write_diag(103)
    assert "UUID_GAME payload=103B" in s
    assert "mtu_size=None" in s


async def test_game_channel_write_diag_with_client_fits() -> None:
    client = FakeBleakClient(mtu_size=247)
    coord = make_coordinator(client=client)
    s = coord._game_channel_write_diag(103)
    # 247-3 = 244 cap → 103 fits.
    assert "payload fits single write: yes" in s
    assert "max_write_without_response_size=512B" in s

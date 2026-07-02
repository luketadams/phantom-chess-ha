"""Coverage for the BLE connect path + the game-channel discovery callback.

``_ble_connect_and_run`` registers an inner ``_make_discovery_cb`` closure on
every notify-capable characteristic that isn't in the known/skip sets — on a
default Phantom layout that's UUID_GAME (and UUID_SCULPTURE). That closure is
where firmware-0.3.0 human-move parsing, AI-echo suppression, settle-window /
reset-mode filtering, CLEAN:Match live-fen updates, and BLE_MOVE_DONE
resolution all live. These tests run the connect method against the
FakeBleakClient, capture the registered UUID_GAME callback, and drive it with
representative notifications to exercise each branch — plus the connect-time
subscribe-degraded, debug-dump, and GATT-staleness branches.

Run (phacc not required)::

    pytest tests/test_coord_discovery.py -p no:pytest_homeassistant_custom_component
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import chess
import pytest

from custom_components.phantom_chess import const
from custom_components.phantom_chess import coordinator as coord_mod
from custom_components.phantom_chess.coordinator import BleakError

from .ble_mock import FakeBleakClient, make_coordinator

UUID_GAME = const.UUID_GAME


async def _run_connect(coord, client, *, expect_raise=False):
    """Run _ble_connect_and_run just long enough to register callbacks."""
    device = MagicMock()
    device.address = coord._ble_address
    # tame TTS + task-scheduling fan-out so notifications don't spawn real work
    coord.hass.async_create_task = lambda coro, *a, **k: coro.close()
    with patch.object(coord_mod, "BleakClient", lambda *a, **k: client), \
         patch.object(coord_mod, "async_ble_device_from_address", return_value=device), \
         patch.object(coord_mod, "async_delete_issue", lambda *a, **k: None):
        task = asyncio.create_task(coord._ble_connect_and_run())
        if expect_raise:
            with pytest.raises(Exception):
                await asyncio.wait_for(task, timeout=5)
            return None
        for _ in range(200):
            await asyncio.sleep(0.005)
            if UUID_GAME.lower() in client.notify_callbacks:
                break
        # Cancel rather than waiting out the connect's 1s keep-alive sleep.
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return client.notify_callbacks[UUID_GAME.lower()]


async def _connected_coord(**kw):
    client = FakeBleakClient(read_values={const.UUID_VERSION: b"0.3.0"}, **kw)
    coord = make_coordinator(ble_connected=False, client=None)
    cb = await _run_connect(coord, client)
    coord._ble_client = client
    return coord, client, cb


# ── connect-time branches ───────────────────────────────────────────────────


async def test_connect_reads_version_and_registers_game_cb():
    coord, client, cb = await _connected_coord()
    assert coord._state["firmware_version"] == "0.3.0"
    assert cb is not None
    assert UUID_GAME in client.started_notifies


async def test_connect_version_read_failure_is_tolerated():
    client = FakeBleakClient()
    client.fail_read[const.UUID_VERSION.lower()] = Exception("read fail")
    coord = make_coordinator(ble_connected=False, client=None)
    cb = await _run_connect(coord, client)
    assert cb is not None
    assert coord._state["firmware_version"] is None


async def test_connect_degraded_subscribe_matrix_and_battery():
    client = FakeBleakClient(read_values={const.UUID_VERSION: b"0.3.2"})
    client.fail_start_notify[const.UUID_SEND_MATRIX.lower()] = Exception("WRITE_NOT_PERMITTED")
    client.fail_start_notify[const.UUID_BATTERY_INFO.lower()] = Exception("WRITE_NOT_PERMITTED")
    coord = make_coordinator(ble_connected=False, client=None)
    cb = await _run_connect(coord, client)
    assert cb is not None
    # degraded set remembers both so the second occurrence logs at debug
    assert "SEND_MATRIX" in coord._subscribe_degraded_logged
    assert "BATTERY_INFO" in coord._subscribe_degraded_logged


async def test_connect_degraded_subscribe_debug_branch_second_time():
    client = FakeBleakClient()
    client.fail_start_notify[const.UUID_SEND_MATRIX.lower()] = Exception("nope")
    coord = make_coordinator(ble_connected=False, client=None)
    coord._subscribe_degraded_logged = {"SEND_MATRIX"}  # pre-seed → debug branch
    cb = await _run_connect(coord, client)
    assert cb is not None


async def test_connect_nonfatal_subscribe_failure_warns():
    client = FakeBleakClient()
    client.fail_start_notify[const.UUID_STATUS_BOARD.lower()] = Exception("bad")
    coord = make_coordinator(ble_connected=False, client=None)
    cb = await _run_connect(coord, client)
    assert cb is not None  # connect survives a non-degradable subscribe failure


async def test_connect_debug_dump_enabled(tmp_path):
    client = FakeBleakClient(read_values={const.UUID_VERSION: b"0.3.0"})
    entry = MagicMock()
    entry.options = {"debug_dump": True}
    coord = make_coordinator(ble_connected=False, client=None, entry=entry)
    coord.hass.config.path = lambda *a: str(tmp_path.joinpath(*a))
    cb = await _run_connect(coord, client)
    assert cb is not None
    # GATT dump + char values written under the debug dir
    assert (tmp_path / "phantom_chess" / "debug" / "gatt.txt").exists()


async def test_connect_sculpture_probe_staleness_raises():
    client = FakeBleakClient(read_values={const.UUID_VERSION: b"0.3.0"})
    client.fail_read[const.UUID_SCULPTURE.lower()] = BleakError("stale handle")
    coord = make_coordinator(ble_connected=False, client=None)
    coord._handle_gatt_staleness = AsyncMock(return_value=True)
    await _run_connect(coord, client, expect_raise=True)
    coord._handle_gatt_staleness.assert_awaited()


async def test_connect_sculpture_probe_nonstale_tolerated():
    client = FakeBleakClient(read_values={const.UUID_VERSION: b"0.3.0"})
    client.fail_read[const.UUID_SCULPTURE.lower()] = BleakError("other")
    coord = make_coordinator(ble_connected=False, client=None)
    coord._handle_gatt_staleness = AsyncMock(return_value=False)
    cb = await _run_connect(coord, client)
    assert cb is not None


# ── discovery callback: opcode routing ──────────────────────────────────────


async def test_cb_move_done_resolves_future_and_clears_settle():
    coord, client, cb = await _connected_coord()
    fut = coord.hass.loop.create_future()
    coord._move_done_future = fut
    coord._activation_settle_until = coord.hass.loop.time() + 600
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x0c"))
    assert fut.result() is True
    assert coord._activation_settle_until == 0.0


async def test_cb_clean_match_updates_live_fen():
    coord, client, cb = await _connected_coord()
    coord._last_target_fen = "8/8/8/8/8/8/8/K6k"
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x08CLEAN: Match.,,"))
    await asyncio.sleep(0)
    assert coord._state["live_fen"] == "8/8/8/8/8/8/8/K6k"


async def test_cb_opcode_08_routes_matrix_bytes():
    coord, client, cb = await _connected_coord()
    coord._handle_matrix_bytes = MagicMock()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x08ERROR: mism,,"))
    coord._handle_matrix_bytes.assert_called_once()


# ── discovery callback: human-move parsing ──────────────────────────────────


async def test_cb_legal_move_pushes_board_no_game():
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] == "e2e4"
    assert coord._state["firmware_last_move"] == "M 1 e2-e4"
    assert coord._board.fen().startswith("rnbqkbnr/pppppppp/8/8/4P3")


async def test_cb_legal_move_local_game_schedules_ai():
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    coord._local_game_active = True
    coord._replace_local_game_task = AsyncMock()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0.02)
    coord._replace_local_game_task.assert_awaited()


async def test_cb_legal_move_two_player_records():
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    coord._two_player_active = True
    coord._record_and_analyze_local_move = MagicMock()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0.02)
    coord._record_and_analyze_local_move.assert_called_once()


async def test_cb_legal_move_lichess_queues():
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    coord._game_id = "abc123"
    coord._drain_physical_move_queue = AsyncMock()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0.02)
    assert coord._physical_move_queue.get_nowait() == "e2e4"


async def test_cb_ai_echo_is_suppressed():
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    # mark e2e4 as an expected AI-move echo
    coord._last_ai_uci = "e2e4"
    coord._last_ai_echo_ucis = {"e2e4"}
    coord._last_ai_uci_set_at = coord.hass.loop.time()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    # move NOT applied
    assert coord._state["last_move"] is None
    assert coord._board.fen() == chess.Board().fen()


async def test_cb_ai_echo_rotated_form_is_suppressed():
    """A 180°-rotated echo of the AI move must also be suppressed.

    Uses the production ``_set_last_ai_move`` so the echo set (primary +
    rotated) is built by real code, not hand-seeded.
    """
    coord, client, cb = await _connected_coord()
    # rotated(e2e4) = rank-mirror + from/to swap = "e5e7" (see
    # _rotate_uci_180's involution docstring). "e5e7" is also illegal here,
    # so board state alone can't discriminate echo-suppression from
    # legality-rejection — but ``firmware_last_move`` can: the echo branch
    # returns BEFORE recording it, while the real-move path records it even
    # for moves that later fail to apply.
    coord._board = chess.Board()
    coord._board.push_uci("e2e4")
    post_ai_fen = coord._board.fen()
    coord._set_last_ai_move("e2e4")
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e5-e7"))
    await asyncio.sleep(0)
    assert coord._state["firmware_last_move"] is None  # echo branch hit
    assert coord._state["last_move"] is None
    assert coord._board.fen() == post_ai_fen


async def test_cb_ai_echo_window_expiry_applies_move():
    """After the 60s echo window, the same UCI is treated as a real move."""
    import time as _time

    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    coord._set_last_ai_move("e2e4")
    coord._last_ai_uci_set_at = _time.monotonic() - 61.0  # expire the window
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] == "e2e4"


async def test_cb_dedup_allows_different_move():
    """The per-connect last_seen dedup only drops IDENTICAL payloads —
    a different move right after must still be applied."""
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] == "e2e4"
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e7-e5"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] == "e7e5"


async def test_cb_settle_window_suppresses_move():
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    coord._activation_settle_until = coord.hass.loop.time() + 600
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    assert coord._state["firmware_last_move"] == "M 1 e2-e4"
    assert coord._state["last_move"] is None  # not applied to board


async def test_cb_reset_mode_suppresses_move():
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    coord._state["firmware_mode"] = "Setting Up"
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 d2-d7"))
    await asyncio.sleep(0)
    assert coord._state["firmware_last_move"] == "M 1 d2-d7"
    assert coord._state["last_move"] is None


async def test_cb_illegal_move_two_player_flags_out_of_sync():
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    coord._two_player_active = True
    coord._flag_two_player_out_of_sync = MagicMock()
    # e4-e5 from the start position is illegal for either interpretation
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e4-e5"))
    await asyncio.sleep(0.02)
    coord._flag_two_player_out_of_sync.assert_called_once()


async def test_cb_dedup_ignores_repeated_payload():
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    coord._board = chess.Board()  # reset; a second identical notify must no-op
    coord._state["last_move"] = None
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] is None


async def test_cb_non_move_payload_ignored():
    coord, client, cb = await _connected_coord()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"HOME"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] is None

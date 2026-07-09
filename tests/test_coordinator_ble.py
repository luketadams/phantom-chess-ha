"""BLE-mocked coordinator tests: senders, write path, notify handlers, loops.

These exercise the coordinator's BLE surface against the FakeBleakClient in
``tests/ble_mock.py`` — the opcode senders (GAME_START/END/SIDE/etc.), the
``_ble_write`` success + error paths, the notification callbacks, the matrix
poll loop, the connect/reconnect loops, and the snapshot-move orchestration.

Run (fast, minimal-style env — no phacc needed)::

    pytest tests/test_coordinator_ble.py -p no:pytest_homeassistant_custom_component
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import chess
import pytest

from custom_components.phantom_chess import const
from custom_components.phantom_chess import coordinator as coord_mod
from custom_components.phantom_chess.coordinator import BleakError, PhantomChessCoordinator

from .ble_mock import FakeBleakClient, make_coordinator

UUID_GAME = const.UUID_GAME


# ── harness drift guard ─────────────────────────────────────────────────────


async def test_make_coordinator_covers_every_init_attribute():
    """Guard against ble_mock.make_coordinator drifting from __init__.

    make_coordinator replicates ``PhantomChessCoordinator.__init__``
    attribute-for-attribute (bypassing HA scheduling). If a future change
    adds a ``self._x = ...`` to ``__init__`` without updating the harness,
    every harness-based test would exercise a coordinator missing that
    attribute — passing while the real object behaves differently.

    Two checks:
    1. Name coverage — every attribute set in ``__init__`` exists on the
       harness coordinator.
    2. Value coverage — for attributes whose ``__init__`` default is a plain
       constant (None, bool, int, float, str), the harness must set the same
       value. ``make_coordinator(ble_connected=False)`` is used so
       ``_ble_connected`` matches ``__init__``'s ``False`` default.
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(PhantomChessCoordinator.__init__))
    tree = ast.parse(src)
    init_attrs: set[str] = set()
    init_const_defaults: dict[str, object] = {}
    for node in ast.walk(tree):
        targets = []
        value_node = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value_node = node.value
        for t in targets:
            if (
                isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == "self"
            ):
                init_attrs.add(t.attr)
                if isinstance(value_node, ast.Constant):
                    init_const_defaults[t.attr] = value_node.value

    # Check 1: all attribute names present.
    coord = make_coordinator()
    missing = {a for a in init_attrs if not hasattr(coord, a)}
    assert not missing, (
        f"make_coordinator is missing attributes set by __init__: {sorted(missing)} "
        "— update tests/ble_mock.py"
    )

    # Check 2: constant default VALUES match (use ble_connected=False to match
    # __init__'s `self._ble_connected = False`).
    coord_defaults = make_coordinator(ble_connected=False)
    wrong = {
        attr: (init_const_defaults[attr], getattr(coord_defaults, attr))
        for attr in init_const_defaults
        if hasattr(coord_defaults, attr)
        and getattr(coord_defaults, attr) != init_const_defaults[attr]
    }
    assert not wrong, (
        "make_coordinator sets wrong default value(s) vs __init__: "
        + ", ".join(f"{a}: expected {exp!r} got {got!r}" for a, (exp, got) in sorted(wrong.items()))
        + " — update tests/ble_mock.py"
    )


# ── opcode senders (wire bytes) ─────────────────────────────────────────────


async def test_send_game_end_writes_opcode_1():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord._phantom_send_game_end()
    assert client.last_write_to(UUID_GAME) == b"\x01"


async def test_send_side_writes_opcode_0a_plus_value():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord._phantom_send_side("2")
    assert client.last_write_to(UUID_GAME) == b"\x0a2"


async def test_send_movement_verify_writes_opcode_3():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord._phantom_send_movement_verify("1")
    assert client.last_write_to(UUID_GAME) == b"\x031"


async def test_send_check_sound_check_and_mate():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord._phantom_send_check_sound("1")
    assert client.last_write_to(UUID_GAME) == b"\x091"
    await coord._phantom_send_check_sound("2")
    assert client.last_write_to(UUID_GAME) == b"\x092"


async def test_send_check_sound_rejects_bad_type():
    coord = make_coordinator(client=FakeBleakClient())
    with pytest.raises(ValueError):
        await coord._phantom_send_check_sound("9")


async def test_send_reset_detection_strips_fen_metadata():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord._phantom_send_reset_detection(chess.STARTING_FEN)
    payload = client.last_write_to(UUID_GAME)
    assert payload[:1] == b"\x0e"
    # only the board-position field survives (no " w KQkq - 0 1")
    assert payload[1:].decode() == chess.STARTING_FEN.split(" ")[0]


async def test_send_game_assistance_defaults_8_fields():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord._phantom_send_game_assistance()
    assert client.last_write_to(UUID_GAME) == b"\x0b" + b"1,1,1,0,0,0,1,0"


async def test_send_game_assistance_all_on():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord._phantom_send_game_assistance(
        auto_castling=True, auto_en_passant=True, auto_snap_to_center=True,
        auto_correct_wrong_move=True, advanced_capture=True, strict_gameplay=True,
        slide_detection=True, jump_to_center=True,
    )
    assert client.last_write_to(UUID_GAME) == b"\x0b" + b"1,1,1,1,1,1,1,1"


async def test_send_ai_move_builds_m_string():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord._phantom_send_ai_move("e2e4")
    assert client.last_write_to(UUID_GAME) == b"\x02M e2-e4 E"


async def test_send_ai_move_capture_separator():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    # set up a capture on the board so is_capture() is True
    coord._board = chess.Board(
        "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    )
    await coord._phantom_send_ai_move("e4d5")
    assert client.last_write_to(UUID_GAME) == b"\x02M e4xd5 E"


async def test_select_chess_play_mode_writes_select_mode_2():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord._phantom_select_chess_play_mode()
    assert client.last_write_to(const.UUID_SELECT_MODE) == b"2"


# ── _ble_write ──────────────────────────────────────────────────────────────


async def test_ble_write_encodes_str():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord._ble_write(UUID_GAME, "hello")
    assert client.last_write_to(UUID_GAME) == b"hello"


async def test_ble_write_raises_when_not_connected():
    coord = make_coordinator(client=None)
    with pytest.raises(RuntimeError, match="BLE not connected"):
        await coord._ble_write(UUID_GAME, b"x")


async def test_ble_write_raises_when_client_disconnected():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    client.is_connected = False  # simulate a mid-session drop
    with pytest.raises(RuntimeError, match="BLE not connected"):
        await coord._ble_write(UUID_GAME, b"x")


async def test_ble_write_propagates_and_handles_staleness():
    client = FakeBleakClient()
    client.is_connected = True
    client.fail_write[UUID_GAME.lower()] = BleakError("boom")
    coord = make_coordinator(client=client)
    coord._handle_gatt_staleness = AsyncMock(return_value=False)
    with pytest.raises(BleakError):
        await coord._ble_write(UUID_GAME, b"x")
    coord._handle_gatt_staleness.assert_awaited_once()


async def test_debug_ble_write_hex_and_utf8():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    await coord.async_debug_ble_write(UUID_GAME, "hex:0102ff")
    assert client.last_write_to(UUID_GAME) == b"\x01\x02\xff"
    await coord.async_debug_ble_write(UUID_GAME, "abc")
    assert client.last_write_to(UUID_GAME) == b"abc"


async def test_debug_ble_write_reraises_on_failure():
    client = FakeBleakClient()
    client.fail_write[UUID_GAME.lower()] = BleakError("nope")
    coord = make_coordinator(client=client)
    coord._handle_gatt_staleness = AsyncMock(return_value=False)
    with pytest.raises(BleakError):
        await coord.async_debug_ble_write(UUID_GAME, "abc")


# ── notification callbacks ──────────────────────────────────────────────────


async def test_on_battery_applies_state():
    coord = make_coordinator()
    coord._on_battery(MagicMock(), bytearray(b"87,1,1,0"))
    await asyncio.sleep(0)  # let call_soon_threadsafe run
    assert coord._state["battery_percent"] == 87
    assert coord._state["battery_charging"] is True


async def test_on_battery_clamps_over_100_percent():
    """fw0.3.3 reports >100% on wall power (105–106 observed live
    2026-07-02); the parser must clamp to the sane 0–100 range."""
    coord = make_coordinator()
    coord._on_battery(MagicMock(), bytearray(b"106,1,1,0"))
    await asyncio.sleep(0)
    assert coord._state["battery_percent"] == 100


async def test_on_battery_ignores_malformed():
    coord = make_coordinator()
    coord._on_battery(MagicMock(), bytearray(b"garbage"))
    await asyncio.sleep(0)
    assert coord._state["battery_percent"] is None


async def test_on_error_msg_logs(caplog):
    coord = make_coordinator()
    before = dict(coord._state)
    coord._on_error_msg(MagicMock(), bytearray(b"ERROR: something,,"))
    await asyncio.sleep(0)
    # no exception + no state change is the contract
    assert coord._state == before
    coord.async_set_updated_data.assert_not_called()


async def test_on_matrix_notify_routes_to_handler():
    coord = make_coordinator()
    coord._handle_matrix_bytes = MagicMock()
    char = MagicMock()
    char.uuid = const.UUID_SEND_MATRIX
    coord._on_matrix_notify(char, bytearray(b"CLEAN: Match.,,"))
    coord._handle_matrix_bytes.assert_called_once()


async def test_on_firmware_mode_notify_routes_to_handler():
    coord = make_coordinator()
    coord._handle_firmware_mode_bytes = MagicMock()
    char = MagicMock()
    char.uuid = const.UUID_FIRMWARE_STATE
    coord._on_firmware_mode_notify(char, bytearray(b"Running"))
    coord._handle_firmware_mode_bytes.assert_called_once()


async def test_on_physical_move_queues():
    coord = make_coordinator()
    coord._on_physical_move(MagicMock(), bytearray(b"M e2-e4"))
    await asyncio.sleep(0)
    assert coord._physical_move_queue.get_nowait() == "M e2-e4"


async def test_on_ble_disconnect_resets_and_fails_future():
    coord = make_coordinator(ble_connected=True)
    fut = coord.hass.loop.create_future()
    coord._move_done_future = fut
    coord._on_ble_disconnect(MagicMock())
    assert coord._ble_connected is False
    assert coord._ble_client is None
    assert coord._phantom_session_initialized is False
    # B1: the waiter rejection is now marshalled onto the loop (set_exception
    # is not thread-safe), so it resolves on the next loop turn — not inline.
    await asyncio.sleep(0)
    with pytest.raises(ConnectionError):
        fut.result()
    # B7: the disconnect fan-out pushes a dict copy, not the live _state.
    pushed = coord.async_set_updated_data.call_args.args[0]
    assert pushed == coord._state
    assert pushed is not coord._state


async def test_on_ble_disconnect_rejects_future_on_loop_thread():
    """B1 thread-safety: bleak disconnect callbacks may run off the event
    loop, and asyncio.Future.set_exception is NOT thread-safe. Deliver the
    disconnect from a worker thread and assert the future rejection executes
    on the LOOP thread. Before the fix set_exception ran on the worker thread.
    """
    import threading

    coord = make_coordinator(ble_connected=True)
    fut = coord.hass.loop.create_future()
    coord._move_done_future = fut

    loop_thread_ident = threading.get_ident()
    reject_thread: dict = {}
    real_reject = coord._reject_move_done_future  # staticmethod → plain function

    def _spy_reject(f):
        reject_thread["ident"] = threading.get_ident()
        return real_reject(f)

    coord._reject_move_done_future = _spy_reject

    t = threading.Thread(target=coord._on_ble_disconnect, args=(MagicMock(),))
    t.start()
    t.join(timeout=5)
    for _ in range(10):
        await asyncio.sleep(0)
        if reject_thread:
            break
    assert reject_thread.get("ident") == loop_thread_ident
    with pytest.raises(ConnectionError):
        fut.result()


# ── matrix poll loop ────────────────────────────────────────────────────────


async def test_matrix_poll_loop_reads_then_stops():
    client = FakeBleakClient(
        read_values={
            const.UUID_SEND_MATRIX: b"CLEAN: Match.,,",
            const.UUID_FIRMWARE_STATE: b"Running",
            const.UUID_BATTERY_INFO: b"55,1,0,0",
        }
    )
    coord = make_coordinator(client=client)
    coord._handle_matrix_bytes = MagicMock()
    coord._handle_firmware_mode_bytes = MagicMock()
    with patch.object(coord_mod, "_sleep", AsyncMock(side_effect=asyncio.CancelledError)):
        await coord._matrix_poll_loop()
    coord._handle_matrix_bytes.assert_called_once()
    coord._handle_firmware_mode_bytes.assert_called_once()
    assert coord._state["battery_percent"] == 55


async def test_matrix_poll_loop_battery_clamps_over_100_percent():
    """E4: the battery clamp lives in the shared _parse_battery_payload, so
    the 2s poll-read path inherits it too — not just the notify callback.
    fw0.3.3 reports 105–106% on wall power; the poll must clamp to 100.

    Falsifiable: with the clamp removed from _parse_battery_payload the poll
    path would surface the raw 106 (both paths call the same parser)."""
    client = FakeBleakClient(
        read_values={
            const.UUID_SEND_MATRIX: b"CLEAN: Match.,,",
            const.UUID_FIRMWARE_STATE: b"Running",
            const.UUID_BATTERY_INFO: b"106,1,1,0",
        }
    )
    coord = make_coordinator(client=client)
    coord._handle_matrix_bytes = MagicMock()
    coord._handle_firmware_mode_bytes = MagicMock()
    with patch.object(coord_mod, "_sleep", AsyncMock(side_effect=asyncio.CancelledError)):
        await coord._matrix_poll_loop()
    assert coord._state["battery_percent"] == 100


async def test_matrix_poll_loop_exits_on_stop_event():
    coord = make_coordinator(client=None)
    coord._stop_event.set()
    await coord._matrix_poll_loop()  # returns immediately, no client access


# ── connect + reconnect loops ───────────────────────────────────────────────


async def test_ble_connect_and_run_discovers_and_subscribes():
    client = FakeBleakClient(read_values={const.UUID_VERSION: b"0.3.2"})
    coord = make_coordinator(ble_connected=False, client=None)
    device = MagicMock()
    device.address = coord._ble_address
    with patch.object(coord_mod, "BleakClient", lambda *a, **k: client), \
         patch.object(coord_mod, "async_ble_device_from_address", return_value=device), \
         patch.object(coord_mod, "async_delete_issue", lambda *a, **k: None):
        task = asyncio.create_task(coord._ble_connect_and_run())
        await asyncio.sleep(0.05)
        coord._stop_event.set()
        await asyncio.wait_for(task, timeout=5)
    assert coord._state["firmware_version"] == "0.3.2"
    # subscribed to the five known notify characteristics
    assert const.UUID_STATUS_BOARD in client.started_notifies
    assert const.UUID_SEND_MATRIX in client.started_notifies
    assert coord._discovered_uuids  # discovery populated


async def test_ble_connect_raises_when_device_missing():
    coord = make_coordinator(ble_connected=False, client=None)
    with patch.object(coord_mod, "async_ble_device_from_address", return_value=None):
        with pytest.raises(BleakError, match="not found"):
            await coord._ble_connect_and_run()


async def test_ble_loop_runs_once_then_stops():
    coord = make_coordinator(ble_connected=False, client=None)
    calls = {"n": 0}

    async def fake_connect():
        calls["n"] += 1
        coord._stop_event.set()

    coord._ble_connect_and_run = fake_connect
    with patch.object(coord_mod, "_sleep", AsyncMock()):
        await coord._ble_loop()
    assert calls["n"] == 1


async def test_ble_loop_logs_and_retries_on_exception():
    coord = make_coordinator(ble_connected=True, client=None)
    calls = {"n": 0}

    async def failing_connect():
        calls["n"] += 1
        if calls["n"] >= 2:
            coord._stop_event.set()
        raise RuntimeError("link down")

    coord._ble_connect_and_run = failing_connect
    with patch.object(coord_mod, "_sleep", AsyncMock()):
        await coord._ble_loop()
    assert calls["n"] == 2
    assert coord._ble_connected is False


# ── snapshot move orchestration ─────────────────────────────────────────────


async def _resolve_move_done_soon(coord, result=True, delay=0.05):
    await asyncio.sleep(delay)
    # wait until the coroutine created the future
    for _ in range(50):
        if coord._move_done_future is not None and not coord._move_done_future.done():
            coord._move_done_future.set_result(result)
            return
        await asyncio.sleep(0.01)


async def test_execute_position_happy_path():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._phantom_session_initialized = True  # skip drop-to-home
    coord._state["firmware_mode"] = "Waiting Side"  # skip the 5s wait
    resolver = asyncio.create_task(_resolve_move_done_soon(coord, True))
    ok = await coord._phantom_execute_position(chess.STARTING_FEN, side="B")
    await resolver
    assert ok is True
    # GAME_START (opcode 0) then SIDE (opcode 0x0a) were written
    game_writes = client.writes_to(UUID_GAME)
    assert game_writes[0][:1] == b"\x00"
    assert any(w[:1] == b"\x0a" for w in game_writes)


async def test_execute_position_timeout_returns_false():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._phantom_session_initialized = True
    coord._state["firmware_mode"] = "Waiting Side"
    with patch.object(coord_mod.asyncio, "wait_for", AsyncMock(side_effect=asyncio.TimeoutError)):
        ok = await coord._phantom_execute_position(chess.STARTING_FEN, side="B")
    assert ok is False


async def test_execute_position_validates_side():
    coord = make_coordinator(client=FakeBleakClient())
    with pytest.raises(ValueError):
        await coord._phantom_execute_position(chess.STARTING_FEN, side="X")


async def test_execute_position_validates_side_opcode():
    coord = make_coordinator(client=FakeBleakClient())
    with pytest.raises(ValueError):
        await coord._phantom_execute_position(chess.STARTING_FEN, side="B", side_opcode="9")


async def test_execute_position_raises_when_disconnected():
    coord = make_coordinator(ble_connected=False, client=None)
    with pytest.raises(RuntimeError, match="BLE not connected"):
        await coord._phantom_execute_position(chess.STARTING_FEN)


async def test_move_piece_updates_state():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._phantom_execute_position = AsyncMock(return_value=True)
    await coord.async_move_piece("e2", "e4")
    assert coord._state["last_move"] == "e2e4"
    assert coord._state["live_fen"] is not None
    coord._phantom_execute_position.assert_awaited_once()


async def test_move_piece_rejects_bad_squares():
    coord = make_coordinator(client=FakeBleakClient())
    with pytest.raises(ValueError):
        await coord.async_move_piece("z9", "e4")


async def test_move_piece_raises_when_disconnected():
    coord = make_coordinator(ble_connected=False, client=None)
    with pytest.raises(RuntimeError, match="BLE not connected"):
        await coord.async_move_piece("e2", "e4")


async def test_drop_to_home_returns_when_home():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._state["firmware_mode"] = "HOME"
    await coord._phantom_drop_to_home()
    assert client.last_write_to(UUID_GAME) == b"\x01"  # GAME_END sent


async def test_drop_to_home_times_out():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._state["firmware_mode"] = "Running"  # never HOME
    with patch.object(coord_mod, "_sleep", AsyncMock()):
        # loop.time advances via real loop; force a tiny timeout
        with pytest.raises(TimeoutError):
            await coord._phantom_drop_to_home(timeout=-1)

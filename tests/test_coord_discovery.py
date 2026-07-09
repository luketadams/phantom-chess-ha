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


def _track_reads(client) -> list[str]:
    """Wrap client.read_gatt_char to record every UUID read (lowercased)."""
    reads: list[str] = []
    real_read = client.read_gatt_char

    async def _tracking_read(uuid):
        reads.append(uuid.lower())
        return await real_read(uuid)

    client.read_gatt_char = _tracking_read
    return reads


async def test_connect_gatt_read_sweep_skipped_without_debug_dump():
    """C1: the full readable-characteristic GATT sweep on connect is a
    debug-only diagnostic. With debug_dump OFF (entry=None) it is skipped
    entirely — so UUID_GAME, which ONLY the sweep reads during connect, is
    never read. Falsifiable: pre-C1 the sweep ran unconditionally, reading
    every readable characteristic (incl. UUID_GAME) on every reconnect."""
    client = FakeBleakClient(read_values={const.UUID_VERSION: b"0.3.0"})
    reads = _track_reads(client)
    coord = make_coordinator(ble_connected=False, client=None)  # entry=None → debug off
    cb = await _run_connect(coord, client)
    assert cb is not None
    assert UUID_GAME.lower() not in reads


async def test_connect_gatt_read_sweep_runs_with_debug_dump(tmp_path):
    """C1 counterpart: with debug_dump ON the sweep runs and UUID_GAME is
    read (the char-values dump is written)."""
    client = FakeBleakClient(read_values={const.UUID_VERSION: b"0.3.0"})
    reads = _track_reads(client)
    entry = MagicMock()
    entry.options = {"debug_dump": True}
    coord = make_coordinator(ble_connected=False, client=None, entry=entry)
    coord.hass.config.path = lambda *a: str(tmp_path.joinpath(*a))
    cb = await _run_connect(coord, client)
    assert cb is not None
    assert UUID_GAME.lower() in reads
    assert (tmp_path / "phantom_chess" / "debug" / "char_values.txt").exists()


# ── discovery callback: opcode routing ──────────────────────────────────────


async def test_cb_move_done_resolves_future_and_clears_settle():
    coord, client, cb = await _connected_coord()
    fut = coord.hass.loop.create_future()
    coord._move_done_future = fut
    coord._activation_settle_until = coord.hass.loop.time() + 600
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x0c"))
    # B1: the 0x0c handler is now marshalled onto the loop (future resolution
    # is not thread-safe), so it lands on the next loop turn — not inline.
    await asyncio.sleep(0)
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


async def test_cb_move_done_marshalled_to_loop_thread():
    """B1 race fix: bleak may deliver a 0x0c BLE_MOVE_DONE on a non-loop
    thread; asyncio Futures are NOT thread-safe, so the future resolution
    (and the settle/echo-expiry field writes) must run on the event loop.
    Deliver the notification from a worker thread and assert ``set_result``
    executed on the LOOP thread. Before the fix it ran on the worker thread,
    racing ``asyncio.wait_for`` in _phantom_execute_position."""
    import threading

    coord, client, cb = await _connected_coord()
    fut = MagicMock()
    fut.done.return_value = False
    set_result_thread: dict = {}

    def _spy_set_result(_val):
        set_result_thread["ident"] = threading.get_ident()

    fut.set_result.side_effect = _spy_set_result
    coord._move_done_future = fut

    loop_thread_ident = threading.get_ident()
    t = threading.Thread(
        target=cb, args=(MagicMock(uuid=UUID_GAME), bytearray(b"\x0c"))
    )
    t.start()
    t.join(timeout=5)
    for _ in range(10):
        await asyncio.sleep(0)
        if set_result_thread:
            break
    assert set_result_thread.get("ident") == loop_thread_ident
    fut.set_result.assert_called_once_with(True)


async def test_cb_clean_match_state_write_marshalled_to_loop_thread():
    """B1 race fix: the CLEAN: Match branch writes self._state["live_fen"]
    and fans out. Both belong on the loop (the branch runs on the bleak
    notify thread). Deliver from a worker thread and assert the live_fen
    write landed on the LOOP thread. Before the fix the write ran inline on
    the worker thread."""
    import threading

    coord, client, cb = await _connected_coord()
    coord._last_target_fen = "8/8/8/8/8/8/8/K6k"

    loop_thread_ident = threading.get_ident()
    write_thread: dict = {}

    class _SpyState(dict):
        def __setitem__(self, key, value):
            if key == "live_fen":
                write_thread["ident"] = threading.get_ident()
            super().__setitem__(key, value)

    coord._state = _SpyState(coord._state)

    t = threading.Thread(
        target=cb, args=(MagicMock(uuid=UUID_GAME), bytearray(b"\x08CLEAN: Match.,,"))
    )
    t.start()
    t.join(timeout=5)
    for _ in range(10):
        await asyncio.sleep(0)
        if write_thread:
            break
    assert write_thread.get("ident") == loop_thread_ident
    assert coord._state["live_fen"] == "8/8/8/8/8/8/8/K6k"


async def test_cb_move_then_clean_match_back_to_back_uses_post_move_target():
    """Beta5 audit fix U1: when a move frame and a CLEAN: Match frame arrive
    back-to-back from the notify thread (both scheduled before the loop runs
    either), the CLEAN handler must read ``_last_target_fen`` at EXECUTION
    time — after the FIFO-ordered ``_apply_move`` has updated it to the
    post-move board. The pre-fix code snapshotted the target at SCHEDULE time
    (closure default argument, evaluated on the notify thread), captured the
    stale pre-move target, and reverted ``live_fen`` after the move applied —
    a revert that persisted because matrix pushes don't fire during Board
    Playing."""
    import threading

    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    stale_pre_move_target = chess.Board().board_fen()
    coord._last_target_fen = stale_pre_move_target

    def _deliver_both():
        # Same notify thread, no loop turn in between: both callbacks are
        # scheduled onto the loop before either executes (FIFO).
        cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
        cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x08CLEAN: Match.,,"))

    t = threading.Thread(target=_deliver_both)
    t.start()
    t.join(timeout=5)
    for _ in range(10):
        await asyncio.sleep(0)

    post_move = chess.Board()
    post_move.push_uci("e2e4")
    # Move applied…
    assert coord._state["last_move"] == "e2e4"
    # …and CLEAN: Match did NOT revert live_fen to the stale pre-move target.
    assert coord._state["live_fen"] == post_move.board_fen()
    assert coord._state["live_fen"] != stale_pre_move_target


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
    """After the echo time backstop expires, the same UCI is a real move.

    The backstop was raised 60s -> 600s (AI_ECHO_BACKSTOP_SECONDS, M9) to match
    the 600s activation-settle window, so we push the set-at time past 600s.
    """
    import time as _time

    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    coord._set_last_ai_move("e2e4")
    coord._last_ai_uci_set_at = _time.monotonic() - 601.0  # expire the backstop
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
    """A second identical move payload immediately after the first is a no-op.

    Post-M4 the raw ``last_seen`` gate no longer swallows move frames; this
    now falls to the M2 double-fire window instead (the two fires land well
    within MOVE_DEDUP_WINDOW_SECONDS). Behaviour asserted is unchanged: the
    refire must not be applied a second time.
    """
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    coord._board = chess.Board()  # reset; a second identical notify must no-op
    coord._state["last_move"] = None
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] is None


async def test_cb_move_apply_marshalled_to_loop_thread():
    """M5 race fix: bleak delivers notifications on a non-loop thread; the
    move-apply block (board push + state mutation) must run on the event
    loop. Deliver the notification from a worker thread and assert both
    that the apply executed on the LOOP thread and that the move landed.
    Before the fix, the apply ran directly on the worker thread."""
    import threading

    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()

    loop_thread_ident = threading.get_ident()
    apply_thread: dict = {}
    real_echo = coord._is_ai_echo

    def _spy_echo(payload_str):
        # _is_ai_echo is the first statement of the marshalled apply block —
        # the thread it runs on is the thread the whole apply runs on.
        apply_thread["ident"] = threading.get_ident()
        return real_echo(payload_str)

    coord._is_ai_echo = _spy_echo

    t = threading.Thread(
        target=cb, args=(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    )
    t.start()
    t.join(timeout=5)
    # give the loop a few iterations to run the marshalled callback
    for _ in range(10):
        await asyncio.sleep(0)
        if apply_thread:
            break
    assert apply_thread.get("ident") == loop_thread_ident
    assert coord._state["last_move"] == "e2e4"


async def test_cb_non_move_payload_ignored():
    coord, client, cb = await _connected_coord()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"HOME"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] is None


# ── M2: double-fire move dedup (~400ms window) ──────────────────────────────


async def test_cb_double_fire_rotated_refire_dropped():
    """M2: a firmware double-fire arriving as the 180°-ROTATED form of the
    just-applied move, within the window, is dropped as a refire.

    Falsifiable: rotate_180("e2e4") == "e5e7". After e2e4 is applied, a second
    frame "M 1 e5-e7" resolves (via rotation) back to e2e4 and is dropped
    BEFORE ``firmware_last_move`` is recorded — so that sensor still shows the
    first move. On the pre-M2 code the frame fell through to the legality check
    (both interpretations illegal) but only AFTER overwriting firmware_last_move
    with "M 1 e5-e7"."""
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] == "e2e4"
    assert coord._state["firmware_last_move"] == "M 1 e2-e4"
    # Second (rotated) fire, same ~instant → inside MOVE_DEDUP_WINDOW_SECONDS.
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e5-e7"))
    await asyncio.sleep(0)
    # Dropped before firmware_last_move recording → sensor unchanged.
    assert coord._state["firmware_last_move"] == "M 1 e2-e4"
    # And the board still holds exactly the one move.
    assert len(coord._board.move_stack) == 1


async def test_cb_double_fire_distinct_move_within_window_applied():
    """M2: a DISTINCT legal move inside the window is a real premove, not a
    refire — it must still be applied (dedup keys on same-UCI/rotation only)."""
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    # d7-d5 right after — distinct from e2e4 and its rotation → applied.
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 d7-d5"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] == "d7d5"
    assert len(coord._board.move_stack) == 2


async def test_cb_same_uci_after_window_is_applied():
    """M2: once the ~400ms window elapses, an identical-UCI frame is treated
    as a legitimate repeat (e.g. a piece shuffled back) and applied — the drop
    is time-scoped, not permanent."""
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    # Simulate the same UCI becoming legal again later, past the window.
    coord._board = chess.Board()
    coord._state["last_move"] = None
    coord._last_applied_move_ts -= 10.0  # push the apply time outside the window
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] == "e2e4"


# ── M4: scope the last_seen dedup ───────────────────────────────────────────


async def test_cb_repeated_move_frame_reaches_apply_not_last_seen():
    """M4: a repeated identical MOVE frame is no longer swallowed by the raw
    last_seen gate — it reaches the apply path. With the M2 window expired the
    second frame is applied, proving last_seen did not drop it.

    Falsifiable: on the pre-M4 code the identical payload hit
    ``if decoded == prev: return`` and never reached the apply path, so the
    second frame produced no move."""
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    # Expire the M2 window and make the same UCI legal again.
    coord._board = chess.Board()
    coord._state["last_move"] = None
    coord._last_applied_move_ts -= 10.0
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    assert coord._state["last_move"] == "e2e4"  # reached apply → applied


async def test_cb_repeated_non_move_frame_still_deduped():
    """M4: heartbeat/status chatter is still de-duplicated. Two identical
    opcode-0x08 matrix frames route to _handle_matrix_bytes only once."""
    coord, client, cb = await _connected_coord()
    coord._handle_matrix_bytes = MagicMock()
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x08CLEAN: Match,,"))
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x08CLEAN: Match,,"))
    coord._handle_matrix_bytes.assert_called_once()


async def test_cb_repeated_move_done_both_delivered():
    """M4: a repeated BLE_MOVE_DONE (0x0c) must reach the handler each time —
    it isn't heartbeat chatter. Two 0x0c frames each resolve a fresh future.

    Falsifiable: on the pre-M4 code the second 0x0c (identical decoded payload)
    was dropped by last_seen, so the second future never resolved."""
    coord, client, cb = await _connected_coord()
    fut1 = coord.hass.loop.create_future()
    coord._move_done_future = fut1
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x0c"))
    await asyncio.sleep(0)  # B1: 0x0c handler is marshalled onto the loop
    assert fut1.result() is True
    fut2 = coord.hass.loop.create_future()
    coord._move_done_future = fut2
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x0c"))
    await asyncio.sleep(0)
    assert fut2.result() is True


# ── M9: echo expiry driven by BLE_MOVE_DONE ─────────────────────────────────


async def test_cb_ai_echo_cleared_after_move_done_grace():
    """M9: once BLE_MOVE_DONE has fired and the grace elapsed, a frame matching
    a (now stale) echo UCI is no longer suppressed.

    Falsifiable via ``firmware_last_move``: the echo branch records nothing, the
    real-move path records it even when it later fails legality. On the pre-M9
    code (no move-done expiry) the fresh 60s window kept suppressing it."""
    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    coord._board.push_uci("e2e4")
    coord._set_last_ai_move("e2e4")  # echo set {e2e4, e5e7}
    # BLE_MOVE_DONE arms the grace deadline (marshalled onto the loop, B1).
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x0c"))
    await asyncio.sleep(0)
    assert coord._ai_echo_move_done_expire_at > 0
    # Force the grace period to have elapsed.
    coord._ai_echo_move_done_expire_at = coord.hass.loop.time() - 1.0
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e5-e7"))
    await asyncio.sleep(0)
    assert coord._state["firmware_last_move"] == "M 1 e5-e7"  # not suppressed
    assert coord._last_ai_echo_ucis == set()  # cleared


async def test_cb_castle_rook_echo_suppressed_within_grace():
    """M9: the grace period spans a castle's king→rook echoes. After
    BLE_MOVE_DONE arms the grace, the trailing rook echo (within grace) is
    still suppressed — the echo set is NOT cleared prematurely."""
    coord, client, cb = await _connected_coord()
    pre = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    mv = chess.Move.from_uci("e1g1")  # kingside castle
    coord._board = pre.copy()
    coord._board.push(mv)
    coord._set_last_ai_move("e1g1", mv=mv, pre_move_board=pre)
    assert "h1f1" in coord._last_ai_echo_ucis  # rook pre-registered
    # move-done arms grace (deadline in the future); marshalled onto loop (B1).
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x0c"))
    await asyncio.sleep(0)
    assert coord._ai_echo_move_done_expire_at > coord.hass.loop.time()
    # Rook echo lands within the grace window → still suppressed.
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 h1-f1"))
    await asyncio.sleep(0)
    assert coord._state["firmware_last_move"] is None
    assert coord._last_ai_echo_ucis  # set retained through the grace


async def test_cb_ai_echo_backstop_holds_without_move_done():
    """M9: when no BLE_MOVE_DONE ever arrives, the time backstop (raised
    60s→600s to match the settle window) still suppresses a slow echo.

    Falsifiable: the set-at time is 100s old — beyond the OLD 60s backstop
    (which would apply the move) but within the new 600s one (still suppress)."""
    import time as _time

    coord, client, cb = await _connected_coord()
    coord._board = chess.Board()
    coord._set_last_ai_move("e2e4")
    coord._last_ai_uci_set_at = _time.monotonic() - 100.0  # 100s ago, no 0x0c
    cb(MagicMock(uuid=UUID_GAME), bytearray(b"\x03M 1 e2-e4"))
    await asyncio.sleep(0)
    assert coord._state["firmware_last_move"] is None  # still suppressed

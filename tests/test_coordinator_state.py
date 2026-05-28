"""Tests for the synchronous state-mutator methods on PhantomChessCoordinator.

The coordinator is mostly async BLE/Lichess infrastructure, but it also
contains a layer of pure-data state mutators that get called from
notification handlers and poll loops:

- ``_apply_firmware_mode_state`` — parses firmware_mode text payload
  (mode label OR move event) and updates ``self._state``.
- ``_apply_battery_state`` — stores percent + charging flag.
- ``_apply_matrix_state`` — parses the matrix notification payload
  (CLEAN/ERROR + 10×10 grid + 10×10 sensor bitmap), recomputes derived
  fields (live_fen, piece_count, position_consistent, mismatches), and
  drives the persistent_notification side effect.
- ``_blank_state`` — initial state-dict shape used at coordinator boot.

These methods are synchronous and only need a stubbed coordinator
instance with ``self._state``, ``self.hass``, and a no-op
``async_set_updated_data``.

Run:
    pytest tests/test_coordinator_state.py
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.phantom_chess.coordinator import PhantomChessCoordinator


# ─── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def coord() -> PhantomChessCoordinator:
    """A coordinator instance with only the attributes the state mutators
    need. Bypasses ``__init__`` (which calls DataUpdateCoordinator.__init__
    with HA scheduling primitives) via ``__new__``.
    """
    c = PhantomChessCoordinator.__new__(PhantomChessCoordinator)
    # The state mutators read/write self._state freely.
    c._state = {}
    # _apply_matrix_state's notification path checks this against the
    # current mismatch signature; None means "no prior notification" so
    # the first ERROR payload triggers a create.
    c._last_mismatch_signature = None
    # async_set_updated_data is the DataUpdateCoordinator API — stub it
    # so the mutators can call it without crashing.
    c.async_set_updated_data = MagicMock()
    # _update_mismatch_notification fans out to hass services. Stub hass
    # with the only attributes the path touches.
    c.hass = MagicMock()
    # _debug_dump_enabled checks self._entry.options — coordinator code
    # tolerates None via the getattr/None-check pattern.
    c._entry = None
    return c


# ─── _apply_firmware_mode_state ─────────────────────────────────────────


def test_apply_firmware_mode_state_mode_label_writes_state(
    coord: PhantomChessCoordinator,
) -> None:
    """A mode label like 'Running' is stored under firmware_mode."""
    coord._apply_firmware_mode_state("Running")
    assert coord._state["firmware_mode"] == "Running"
    assert "firmware_mode_last_updated" in coord._state
    coord.async_set_updated_data.assert_called_once()


def test_apply_firmware_mode_state_dedup_skips_unchanged(
    coord: PhantomChessCoordinator,
) -> None:
    """Same mode value twice is a no-op (no async_set_updated_data call
    the second time)."""
    coord._apply_firmware_mode_state("Running")
    coord.async_set_updated_data.reset_mock()
    coord._apply_firmware_mode_state("Running")
    coord.async_set_updated_data.assert_not_called()


def test_apply_firmware_mode_state_move_event_routed_separately(
    coord: PhantomChessCoordinator,
) -> None:
    """A 'K e1-e2' style move event goes into firmware_last_move, NOT
    firmware_mode."""
    coord._state["firmware_mode"] = "Running"
    coord._apply_firmware_mode_state("K e1-e2")
    # firmware_mode untouched
    assert coord._state["firmware_mode"] == "Running"
    # firmware_last_move updated
    assert coord._state["firmware_last_move"] == "K e1-e2"
    assert "firmware_last_move_updated" in coord._state


def test_apply_firmware_mode_state_move_with_capture_separator(
    coord: PhantomChessCoordinator,
) -> None:
    """The 'x' separator is also valid for capture move events."""
    coord._apply_firmware_mode_state("p d5xe4")
    assert coord._state["firmware_last_move"] == "p d5xe4"


def test_apply_firmware_mode_state_invalid_text_falls_through_to_mode(
    coord: PhantomChessCoordinator,
) -> None:
    """A string that's neither a recognised mode label NOR a move event
    is still stored as firmware_mode — the integration doesn't reject
    unknown labels because new firmware versions may add modes."""
    coord._apply_firmware_mode_state("SomeNewMode")
    assert coord._state["firmware_mode"] == "SomeNewMode"


def test_apply_firmware_mode_state_dedup_skips_same_move(
    coord: PhantomChessCoordinator,
) -> None:
    """Same move event twice is dedup'd."""
    coord._apply_firmware_mode_state("K e1-e2")
    coord.async_set_updated_data.reset_mock()
    coord._apply_firmware_mode_state("K e1-e2")
    coord.async_set_updated_data.assert_not_called()


# ─── _apply_battery_state ───────────────────────────────────────────────


def test_apply_battery_state_writes_fields(
    coord: PhantomChessCoordinator,
) -> None:
    coord._apply_battery_state(percent=75, charging=False)
    assert coord._state["battery_percent"] == 75
    assert coord._state["battery_charging"] is False
    coord.async_set_updated_data.assert_called_once()


def test_apply_battery_state_charging_flag(
    coord: PhantomChessCoordinator,
) -> None:
    coord._apply_battery_state(percent=100, charging=True)
    assert coord._state["battery_charging"] is True


# ─── _apply_matrix_state ────────────────────────────────────────────────


# CLEAN-Match notification payload (firmware 0.3.0). 100-char piece grid
# (10×10 row-major, starting position) + 100-char sensor bitmap (all
# ones where pieces are, zeros elsewhere). Borders + gutter rows are '.'.
_CLEAN_PAYLOAD = {
    "raw": b"CLEAN: Match.,starting,starting",
    "piece_grid": (
        "............rnbqkbnr..pppppppp..........................."  # 60
        "PPPPPPPP..RNBQKBNR..............."                             # +33 = 93
    ).ljust(100, "."),
    "sensor_bitmap": (
        "............11111111..11111111..........................."
        "11111111..11111111..............."
    ).ljust(100, "0"),
    "status": "Clean",
    "status_message": "Match.",
}


def test_apply_matrix_state_clean_payload_populates_state(
    coord: PhantomChessCoordinator,
) -> None:
    """A CLEAN payload updates piece_grid, sensor_bitmap, status,
    live_fen, piece_count, position_consistent."""
    coord._apply_matrix_state(_CLEAN_PAYLOAD)
    assert coord._state["matrix_status"] == "Clean"
    assert coord._state["matrix_status_message"] == "Match."
    assert coord._state["piece_grid"] == _CLEAN_PAYLOAD["piece_grid"]
    assert coord._state["sensor_bitmap"] == _CLEAN_PAYLOAD["sensor_bitmap"]
    # piece_count = number of non-'.' chars in piece_grid
    expected_count = sum(1 for c in _CLEAN_PAYLOAD["piece_grid"] if c != ".")
    assert coord._state["piece_count"] == expected_count
    assert "live_fen" in coord._state
    assert "matrix_last_updated" in coord._state
    coord.async_set_updated_data.assert_called_once()


def test_apply_matrix_state_dedup_skips_identical_payload(
    coord: PhantomChessCoordinator,
) -> None:
    """Same payload arriving twice is dedup'd at the apply layer."""
    coord._apply_matrix_state(_CLEAN_PAYLOAD)
    coord.async_set_updated_data.reset_mock()
    coord._apply_matrix_state(_CLEAN_PAYLOAD)
    coord.async_set_updated_data.assert_not_called()


def test_apply_matrix_state_recalculates_piece_count(
    coord: PhantomChessCoordinator,
) -> None:
    """piece_count is recomputed from the new piece_grid each time."""
    # Empty board: all dots → piece_count 0
    empty_payload = {
        **_CLEAN_PAYLOAD,
        "piece_grid": "." * 100,
        "sensor_bitmap": "0" * 100,
    }
    coord._apply_matrix_state(empty_payload)
    assert coord._state["piece_count"] == 0


# ─── _blank_state ──────────────────────────────────────────────────────


def test_blank_state_returns_dict_with_starting_fen(
    coord: PhantomChessCoordinator,
) -> None:
    """``_blank_state`` returns a dict pre-populated with the starting FEN
    so entities reading ``live_fen`` before the first BLE message don't
    crash."""
    state = coord._blank_state()
    assert isinstance(state, dict)
    # The starting position FEN should be present in some form.
    assert state.get("live_fen") is not None
    # Common state fields the entity platforms read on first paint.
    # Don't pin every key — just sanity-check a few load-bearing ones.
    for key in ("piece_grid", "sensor_bitmap"):
        assert key in state, f"_blank_state should pre-populate {key!r}"


def test_blank_state_returns_independent_dicts(
    coord: PhantomChessCoordinator,
) -> None:
    """Each call returns a fresh dict (so mutations don't leak between
    coordinator resets)."""
    a = coord._blank_state()
    b = coord._blank_state()
    a["custom_key"] = "marker"
    assert "custom_key" not in b


# ─── _build_move_speech / _post_move_event_speech ──────────────────────


import chess  # noqa: E402  - imported here for the speech-test fixtures


@pytest.fixture
def coord_with_board(coord: PhantomChessCoordinator) -> PhantomChessCoordinator:
    """Coordinator with a chess.Board ready for the speech builders."""
    coord._board = chess.Board()
    return coord


def test_build_move_speech_white_pawn_to_e4(coord_with_board) -> None:
    """The opening 1.e4 → 'White pawn to e4'."""
    move = chess.Move.from_uci("e2e4")
    out = coord_with_board._build_move_speech(move)
    assert out == "White pawn to e4"


def test_build_move_speech_capture_uses_takes(coord_with_board) -> None:
    """A capture move uses 'takes' instead of 'to'."""
    # Build a position where Nxe5 is legal: 1.e4 e5 2.Nf3
    board = chess.Board()
    board.push_uci("e2e4")
    board.push_uci("e7e5")
    board.push_uci("g1f3")
    coord_with_board._board = board
    # Black moves; pawn on e5 is now black-to-defend; but we want WHITE's
    # next move = Nxe5. Hmm — board.turn is BLACK now. Let me skip a black
    # move first.
    board.push_uci("b7b6")  # black tempo move
    # Now white: Nxe5
    out = coord_with_board._build_move_speech(chess.Move.from_uci("f3e5"))
    assert "takes" in out
    assert out.startswith("White ")
    assert out.endswith(" e5")


def test_build_move_speech_kingside_castle(coord_with_board) -> None:
    """Castling is described as 'White castles kingside'."""
    # Set up white's kingside castle: clear bishop + knight, then castle.
    board = chess.Board()
    for uci in ("e2e4", "e7e5", "g1f3", "g8f6", "f1c4", "f8c5"):
        board.push_uci(uci)
    coord_with_board._board = board
    out = coord_with_board._build_move_speech(chess.Move.from_uci("e1g1"))
    assert out == "White castles kingside"


def test_build_move_speech_invalid_from_square_returns_empty(coord_with_board) -> None:
    """If self._board has nothing on the from square, return empty string."""
    # Construct an empty-ish board
    coord_with_board._board = chess.Board.empty()
    out = coord_with_board._build_move_speech(chess.Move.from_uci("e2e4"))
    assert out == ""


def test_build_move_speech_black_move(coord_with_board) -> None:
    """Black's move is announced with 'Black' prefix."""
    board = chess.Board()
    board.push_uci("e2e4")
    coord_with_board._board = board
    out = coord_with_board._build_move_speech(chess.Move.from_uci("e7e5"))
    assert out == "Black pawn to e5"


def test_post_move_event_speech_check(coord_with_board) -> None:
    """A check is announced as 'Check on <side>'."""
    # Build a "Black in check" position: scholar's-mate-adjacent.
    # Position: Black king on e8, white queen on h5 attacking f7, no
    # piece blocking. Slightly artificial but simpler than real games.
    board = chess.Board("rnbqkbnr/ppp2ppp/8/3pp2Q/4P3/8/PPPP1PPP/RNB1KBNR b KQkq - 1 3")
    # Wait — this position has white queen on h5, black king on e8. Is
    # black in check? h5 doesn't attack e8. Let me use a real check pos.
    # 1.e4 e5 2.Bc4 Nc6 3.Qh5 Nf6?? 4.Qxf7# is mate; we want CHECK not mate.
    # Try: white queen on h5, black king on e8, black pawn on f7 protects.
    # We want check. Simplest: position with white rook on e1, black king
    # on e8, e-file clear.
    board = chess.Board("4k3/8/8/8/8/8/8/4R2K b - - 0 1")
    coord_with_board._board = board
    # Black is to move and is in check (white rook on e1 attacks e8).
    assert board.is_check()
    out = coord_with_board._post_move_event_speech()
    assert "Check" in out
    assert "Black" in out  # Black is the side now to move (in check)


def test_post_move_event_speech_checkmate(coord_with_board) -> None:
    """Checkmate is announced with the winner's name."""
    # Fool's mate: 1.f3 e5 2.g4 Qh4#
    board = chess.Board()
    for uci in ("f2f3", "e7e5", "g2g4", "d8h4"):
        board.push_uci(uci)
    coord_with_board._board = board
    assert board.is_checkmate()
    out = coord_with_board._post_move_event_speech()
    assert "Checkmate" in out
    assert "Black wins" in out  # White is to move and is mated → Black won


def test_post_move_event_speech_stalemate(coord_with_board) -> None:
    """Stalemate produces 'Stalemate. Draw.'."""
    # Stalemate position: Black king on a8 with no legal moves but not in
    # check. White king on c7, queen on b6 controlling escape squares.
    board = chess.Board("k7/8/1QK5/8/8/8/8/8 b - - 0 1")
    coord_with_board._board = board
    assert board.is_stalemate()
    out = coord_with_board._post_move_event_speech()
    assert "Stalemate" in out


def test_post_move_event_speech_nothing_to_announce(coord_with_board) -> None:
    """A normal position (no check, no mate, no stalemate) returns empty."""
    out = coord_with_board._post_move_event_speech()
    assert out == ""


# ─── _on_battery parse path ────────────────────────────────────────────


def test_on_battery_marshal_path_well_formed(
    coord: PhantomChessCoordinator,
) -> None:
    """A well-formed battery payload schedules an apply via call_soon_threadsafe.

    The marshal target receives (percent, charging) parsed from the
    payload's comma-separated fields.
    """
    from unittest.mock import MagicMock
    coord.hass.loop = MagicMock()
    # Payload: "percent,wallStatus,charging,doneCharging" — only [0] and [2]
    # are used.
    coord._on_battery(characteristic=None, data=bytearray(b"75,1,1,0"))
    coord.hass.loop.call_soon_threadsafe.assert_called_once()
    args = coord.hass.loop.call_soon_threadsafe.call_args[0]
    # First arg is the callable (apply_battery_state); rest are its args.
    assert args[1] == 75
    assert args[2] is True


def test_on_battery_malformed_payload_is_silent_noop(
    coord: PhantomChessCoordinator,
) -> None:
    """A malformed payload (non-numeric percent, missing fields, etc.)
    returns silently — no exception, no apply call."""
    from unittest.mock import MagicMock
    coord.hass.loop = MagicMock()
    for bad in (
        b"not,a,battery,payload",
        b"",
        b"75",          # missing fields
        b",,,",         # blank fields
    ):
        coord._on_battery(characteristic=None, data=bytearray(bad))
    coord.hass.loop.call_soon_threadsafe.assert_not_called()


def test_on_battery_charging_off(
    coord: PhantomChessCoordinator,
) -> None:
    """Field 2 = '0' → charging is False."""
    from unittest.mock import MagicMock
    coord.hass.loop = MagicMock()
    coord._on_battery(characteristic=None, data=bytearray(b"42,0,0,0"))
    args = coord.hass.loop.call_soon_threadsafe.call_args[0]
    assert args[1] == 42
    assert args[2] is False


# ─── async_set_sound_level: clamping + payload format ─────────────────


import asyncio  # noqa: E402  - used by the async-method tests below


def _run_async(coro):
    """asyncio.run wrapper — keeps tests sync so they work without
    pytest-asyncio in the matrix-tests minimal env."""
    return asyncio.run(coro)


@pytest.fixture
def coord_with_ble(coord: PhantomChessCoordinator):
    """Coordinator with a mocked _ble_write that captures (uuid, payload)
    pairs for assertion in tests."""
    from unittest.mock import AsyncMock
    coord._ble_write = AsyncMock()
    coord._ble_connected = True
    return coord


def test_async_set_sound_level_clamps_above_32(coord_with_ble) -> None:
    """A request for 50 clamps to 32 (firmware max)."""
    _run_async(coord_with_ble.async_set_sound_level(50))
    assert coord_with_ble.sound_level == 32
    uuid_arg, payload = coord_with_ble._ble_write.call_args[0]
    assert payload == b"32,11110,0"


def test_async_set_sound_level_clamps_below_zero(coord_with_ble) -> None:
    """Negative requests clamp to 0."""
    _run_async(coord_with_ble.async_set_sound_level(-5))
    assert coord_with_ble.sound_level == 0
    _uuid, payload = coord_with_ble._ble_write.call_args[0]
    assert payload == b"0,11110,0"


def test_async_set_sound_level_preserves_in_range(coord_with_ble) -> None:
    """A value within 0-32 passes through unchanged."""
    _run_async(coord_with_ble.async_set_sound_level(16))
    assert coord_with_ble.sound_level == 16
    _uuid, payload = coord_with_ble._ble_write.call_args[0]
    assert payload == b"16,11110,0"


def test_async_set_sound_level_keeps_sounds_bitmask_and_tutorial_defaults(
    coord_with_ble,
) -> None:
    """Per Efraín's doc, payload is `volume,sounds_bitmask,tutorial` —
    the integration keeps `11110` (all sounds on) + `0` (no tutorial)
    as fixed defaults."""
    _run_async(coord_with_ble.async_set_sound_level(20))
    _uuid, payload = coord_with_ble._ble_write.call_args[0]
    assert payload.decode().endswith(",11110,0")


# ─── async_set_mechanism_speed ────────────────────────────────────────


def test_async_set_mechanism_speed_writes_native_uuid(coord_with_ble) -> None:
    """Mechanism speed writes to UUID_MECHANISM_SPEED with ASCII integer."""
    _run_async(coord_with_ble.async_set_mechanism_speed(3))
    assert coord_with_ble.mechanism_speed == 3
    _uuid, payload = coord_with_ble._ble_write.call_args[0]
    assert payload == b"3"


# ─── async_set_pause: mode mapping ────────────────────────────────────


def test_async_set_pause_true_writes_mode_3(coord_with_ble) -> None:
    """paused=True → SELECT_MODE 3 (pause); also sets game_status."""
    from custom_components.phantom_chess.const import STATUS_PAUSED
    _run_async(coord_with_ble.async_set_pause(True))
    assert coord_with_ble.paused is True
    assert coord_with_ble._state["game_status"] == STATUS_PAUSED
    _uuid, payload = coord_with_ble._ble_write.call_args[0]
    assert payload == b"3"


def test_async_set_pause_false_writes_chess_play_mode(coord_with_ble) -> None:
    """paused=False → SELECT_MODE 2 (chess play); game_status = playing."""
    from custom_components.phantom_chess.const import MODE_CHESS_PLAY, STATUS_PLAYING
    _run_async(coord_with_ble.async_set_pause(False))
    assert coord_with_ble.paused is False
    assert coord_with_ble._state["game_status"] == STATUS_PLAYING
    _uuid, payload = coord_with_ble._ble_write.call_args[0]
    assert payload == str(MODE_CHESS_PLAY).encode()


# ─── async_play_sound: arg-mapping + validation ───────────────────────


def test_async_play_sound_check_routes_to_opcode_1(coord_with_ble) -> None:
    """'check' (any casing) routes to _phantom_send_check_sound('1')."""
    from unittest.mock import AsyncMock
    coord_with_ble._phantom_send_check_sound = AsyncMock()
    _run_async(coord_with_ble.async_play_sound("check"))
    coord_with_ble._phantom_send_check_sound.assert_called_once_with("1")


def test_async_play_sound_checkmate_routes_to_opcode_2(coord_with_ble) -> None:
    """'checkmate' (and aliases 'mate', '2') route to opcode 2."""
    from unittest.mock import AsyncMock
    coord_with_ble._phantom_send_check_sound = AsyncMock()
    _run_async(coord_with_ble.async_play_sound("checkmate"))
    coord_with_ble._phantom_send_check_sound.assert_called_once_with("2")


def test_async_play_sound_case_insensitive(coord_with_ble) -> None:
    """'Check' / 'CHECK' / ' check ' all normalize to opcode 1."""
    from unittest.mock import AsyncMock
    coord_with_ble._phantom_send_check_sound = AsyncMock()
    for variant in ("CHECK", "Check", " check "):
        coord_with_ble._phantom_send_check_sound.reset_mock()
        _run_async(coord_with_ble.async_play_sound(variant))
        coord_with_ble._phantom_send_check_sound.assert_called_once_with("1")


def test_async_play_sound_invalid_value_raises_value_error(coord_with_ble) -> None:
    """Anything other than check / checkmate raises ValueError."""
    with pytest.raises(ValueError, match="check.*checkmate"):
        _run_async(coord_with_ble.async_play_sound("draw"))


def test_async_play_sound_requires_ble_connected(coord: PhantomChessCoordinator) -> None:
    """If BLE is not connected, raises RuntimeError before any send."""
    coord._ble_connected = False
    with pytest.raises(RuntimeError, match="BLE not connected"):
        _run_async(coord.async_play_sound("check"))


# ─── _handle_gatt_staleness: BlueZ + bleak detection ──────────────────
#
# Added in alpha29 (2026-05-27) after an AI-vs-AI repro attempt failed
# because the integration's `_ble_connected` flag was stuck True for
# ~6h while BlueZ's underlying GATT objects had been torn down. The
# detector now matches both the bleak-translated "Characteristic ...
# not found" family AND the raw BlueZ
# `org.freedesktop.DBus.Error.UnknownObject` / "doesn't exist" family.


@pytest.fixture
def stale_coord(coord: PhantomChessCoordinator):
    """Coordinator with the bits `_handle_gatt_staleness` touches."""
    from unittest.mock import AsyncMock
    coord._ble_connected = True
    coord._ble_client = MagicMock()
    coord._ble_client.disconnect = AsyncMock()
    # The bleak-cache-miss path consults this set; populate with one
    # known UUID so we can test both "UUID in discovery" and "UUID was
    # never discovered" branches.
    coord._discovered_uuids = {"abc-known"}
    return coord


def test_handle_gatt_staleness_matches_bluez_unknownobject(stale_coord) -> None:
    """The raw BlueZ `UnknownObject` error on GATT char writes is staleness."""
    err = Exception(
        "[org.freedesktop.DBus.Error.UnknownObject] Method \"WriteValue\" "
        "with signature \"aya{sv}\" on interface "
        "\"org.bluez.GattCharacteristic1\" doesn't exist"
    )
    detected = _run_async(
        stale_coord._handle_gatt_staleness(err, "abc-unknown", op="write")
    )
    assert detected is True
    # _ble_connected was flipped False so binary_sensor reflects reality.
    assert stale_coord._ble_connected is False
    # Disconnect was kicked off so the loop reconnects.
    stale_coord._ble_client.disconnect.assert_awaited_once()


def test_handle_gatt_staleness_bluez_doesnt_need_discovery_gate(
    stale_coord,
) -> None:
    """A BlueZ UnknownObject for a UUID that was never in discovery is
    still staleness — the OS layer says the characteristic object is
    gone, so the discovery-gate would be a false negative."""
    err = Exception(
        "org.freedesktop.DBus.Error.UnknownObject ... "
        "org.bluez.GattCharacteristic1 ... doesn't exist"
    )
    detected = _run_async(
        stale_coord._handle_gatt_staleness(err, "never-discovered", op="write")
    )
    assert detected is True


def test_handle_gatt_staleness_matches_bleak_not_found(stale_coord) -> None:
    """bleak's translated 'Characteristic ... not found' on a known
    UUID is staleness (preserves alpha-pre behavior)."""
    err = Exception("Characteristic abc-known not found")
    detected = _run_async(
        stale_coord._handle_gatt_staleness(err, "abc-known", op="write")
    )
    assert detected is True
    assert stale_coord._ble_connected is False


def test_handle_gatt_staleness_bleak_not_found_unknown_uuid_skips(
    stale_coord,
) -> None:
    """bleak 'not found' for a UUID that was never in discovery is NOT
    staleness — that UUID just doesn't exist on this firmware."""
    err = Exception("Characteristic never-discovered not found")
    detected = _run_async(
        stale_coord._handle_gatt_staleness(err, "never-discovered", op="write")
    )
    assert detected is False
    # We did NOT flip the connected flag in this case.
    assert stale_coord._ble_connected is True
    stale_coord._ble_client.disconnect.assert_not_awaited()


def test_handle_gatt_staleness_unrelated_error_skips(stale_coord) -> None:
    """A generic error message that matches neither family returns False
    and leaves the link state untouched."""
    err = Exception("some other random BLE issue")
    detected = _run_async(
        stale_coord._handle_gatt_staleness(err, "abc-known", op="write")
    )
    assert detected is False
    assert stale_coord._ble_connected is True
    stale_coord._ble_client.disconnect.assert_not_awaited()


def test_handle_gatt_staleness_bluez_other_gatt_methods(stale_coord) -> None:
    """ReadValue / StartNotify / StopNotify variants of the BlueZ
    UnknownObject error all match — any GATT char op going through the
    same vanished D-Bus object should count as staleness."""
    for method in ("ReadValue", "StartNotify", "StopNotify"):
        # Reset for clean assertions per iteration.
        stale_coord._ble_connected = True
        stale_coord._ble_client.disconnect.reset_mock()
        err = Exception(
            f"org.freedesktop.DBus.Error.UnknownObject Method \"{method}\" "
            "on interface \"org.bluez.GattCharacteristic1\" doesn't exist"
        )
        detected = _run_async(
            stale_coord._handle_gatt_staleness(err, "abc-known", op="write")
        )
        assert detected is True, f"failed for method {method}"
        assert stale_coord._ble_connected is False, f"flag for method {method}"


def test_handle_gatt_staleness_swallows_disconnect_error(stale_coord) -> None:
    """If `disconnect()` itself raises, we still return True and the
    connected flag is still flipped — the caller is told to bail."""
    from unittest.mock import AsyncMock
    stale_coord._ble_client.disconnect = AsyncMock(
        side_effect=Exception("disconnect went sideways")
    )
    err = Exception(
        "org.freedesktop.DBus.Error.UnknownObject ... "
        "org.bluez.GattCharacteristic1 ... doesn't exist"
    )
    detected = _run_async(
        stale_coord._handle_gatt_staleness(err, "any-uuid", op="write")
    )
    assert detected is True
    assert stale_coord._ble_connected is False

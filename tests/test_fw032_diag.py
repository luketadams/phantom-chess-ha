"""fw0.3.2 BLE-compatibility — pure-logic tests.

Covers the firmware-0.3.2 work added 2026-06-14:
  - the GAME_START wire payload (exact bytes, doc §2.1) and that the
    one-shot "0.3.2 diag" line fires once per session;
  - the 8-field GAME_ASSISTANCE payload (doc §3.4);
  - the MTU/char diagnostic string (_game_channel_write_diag);
  - the import-free 0x0D classifier (_is_invalid_attr_value_length);
  - the actionable length-error builder (_game_start_length_error);
  - the shared battery parser (_parse_battery_payload);
  - the experimental GAME_START variant ladder ordering + payload sizes.

These follow the repo's bind-the-unbound-method-to-a-stub pattern (see
test_two_player.py) so they run in the minimal CI env with no Home Assistant
or bleak installed. The live BLE writes themselves are hardware paths and are
verified on the board.
"""
from __future__ import annotations

import asyncio
import types

import chess

from custom_components.phantom_chess.const import UUID_GAME
from custom_components.phantom_chess.coordinator import BleakError, PhantomChessCoordinator
from custom_components.phantom_chess.matrix import build_matrix_from_fen

# The coordinator only translates the 0x0D rejection when it arrives as a
# ``BleakError`` (the real bleak base class when bleak is installed, as in
# the ha-tests env; an ``Exception`` alias in the minimal matrix-tests env).
# Simulating the fault with a plain ``Exception`` therefore only exercised
# the catch clause in the minimal env and silently escaped it once bleak was
# present. Use the coordinator's own ``BleakError`` symbol so these tests are
# correct in BOTH environments. See FINDINGS_TEST_PORTABILITY.md.


# ── fakes for the diagnostic string ─────────────────────────────────────────


class _FakeChar:
    def __init__(self, props, max_wwr):
        self.properties = props
        self.max_write_without_response_size = max_wwr


class _FakeServices:
    def __init__(self, char):
        self._char = char

    def get_characteristic(self, _uuid):
        return self._char


class _FakeClient:
    def __init__(self, mtu, char):
        self.mtu_size = mtu
        self.services = _FakeServices(char)


class _RaisingMtuClient:
    """mtu_size raises — mirrors BlueZ warning/raise before MTU acquire."""

    def __init__(self, char):
        self.services = _FakeServices(char)

    @property
    def mtu_size(self):
        raise RuntimeError("MTU not acquired")


# ── _is_invalid_attr_value_length ───────────────────────────────────────────


def test_classifier_matches_enum_name():
    err = Exception(
        "(<BleakGATTProtocolErrorCode.INVALID_ATTRIBUTE_VALUE_LENGTH: 13>, "
        "'GATT Protocol Error: Invalid Attribute Value Length')"
    )
    assert PhantomChessCoordinator._is_invalid_attr_value_length(err) is True


def test_classifier_matches_human_text():
    err = Exception("GATT Protocol Error: Invalid Attribute Value Length")
    assert PhantomChessCoordinator._is_invalid_attr_value_length(err) is True


def test_classifier_rejects_unrelated_error():
    err = Exception("Write Not Permitted")
    assert PhantomChessCoordinator._is_invalid_attr_value_length(err) is False


# ── _game_channel_write_diag ────────────────────────────────────────────────


def _diag(mtu, props, max_wwr, payload_len=103):
    stub = types.SimpleNamespace()
    if mtu == "raise":
        stub._ble_client = _RaisingMtuClient(_FakeChar(props, max_wwr))
    elif mtu is None and props is None:
        stub._ble_client = None
    else:
        stub._ble_client = _FakeClient(mtu, _FakeChar(props, max_wwr))
    return PhantomChessCoordinator._game_channel_write_diag(stub, payload_len)


def test_diag_high_mtu_payload_fits():
    s = _diag(247, ["write", "write-without-response"], 244)
    assert "payload=103B" in s
    assert "mtu_size=247" in s
    assert "single-ATT-write cap=244B" in s
    assert "payload fits single write: yes" in s
    assert "max_write_without_response_size=244B" in s
    assert "properties=['write', 'write-without-response']" in s


def test_diag_low_mtu_payload_does_not_fit():
    s = _diag(23, ["write"], 20)
    assert "mtu_size=23" in s
    assert "single-ATT-write cap=20B" in s
    assert "payload fits single write: NO" in s


def test_diag_no_client_is_graceful():
    s = _diag(None, None, None)
    assert "mtu_size=None" in s
    assert "payload fits single write: unknown" in s


def test_diag_mtu_raise_is_graceful():
    # mtu_size raising must not break the diag; falls back to None.
    s = _diag("raise", ["write"], 244)
    assert "mtu_size=None" in s
    assert "max_write_without_response_size=244B" in s


# ── _game_start_length_error ────────────────────────────────────────────────


def test_length_error_message_is_actionable():
    stub = types.SimpleNamespace()
    err = PhantomChessCoordinator._game_start_length_error(
        stub, Exception("0x0D"), 103, "0.3.2 diag: stub"
    )
    assert isinstance(err, RuntimeError)
    msg = str(err)
    assert "INVALID_ATTRIBUTE_VALUE_LENGTH" in msg
    assert "103" in msg
    assert "attr_max_len" in msg
    assert "diagnose_game_start" in msg


# ── GAME_START exact wire bytes + one-shot diag ─────────────────────────────


def _game_start_stub():
    captured = {}

    async def fake_ble_write(uuid, payload, response=True):
        captured["uuid"] = uuid
        captured["payload"] = payload
        captured["response"] = response

    stub = types.SimpleNamespace()
    stub._ble_write = fake_ble_write
    stub._build_phantom_matrix_from_fen = build_matrix_from_fen
    stub._game_channel_write_diag = lambda n: f"diag({n})"
    stub._game_start_diag_logged = False
    return stub, captured


async def test_game_start_payload_is_doc_exact():
    stub, captured = _game_start_stub()
    await PhantomChessCoordinator._phantom_send_game_start(stub, side="W")
    payload = captured["payload"]
    assert isinstance(payload, (bytes, bytearray))
    assert len(payload) == 103          # opcode(1) + matrix(100) + ",W"(2)
    assert payload[0] == 0x00           # GAME_START opcode
    assert payload.endswith(b",W")      # doc §2.1 side suffix
    # The 100-char matrix is column-major and matches the standard start.
    matrix = payload[1:101].decode()
    assert len(matrix) == 100
    assert matrix == build_matrix_from_fen(chess.STARTING_FEN)
    # Side suffix honours the argument.
    stub2, captured2 = _game_start_stub()
    await PhantomChessCoordinator._phantom_send_game_start(stub2, side="B")
    assert captured2["payload"].endswith(b",B")


async def test_game_start_diag_logs_once_then_flips_flag():
    stub, _ = _game_start_stub()
    assert stub._game_start_diag_logged is False
    await PhantomChessCoordinator._phantom_send_game_start(stub)
    assert stub._game_start_diag_logged is True  # one-shot consumed


def _game_start_raising_stub(err):
    """Stub whose _ble_write raises `err`; wires the real classify+map helpers."""

    async def fake_ble_write(uuid, payload, response=True):
        raise err

    stub = types.SimpleNamespace()
    stub._ble_write = fake_ble_write
    stub._build_phantom_matrix_from_fen = build_matrix_from_fen
    stub._game_channel_write_diag = lambda n: f"0.3.2 diag({n})"
    stub._game_start_diag_logged = True  # skip the once-per-session INFO branch
    stub._is_invalid_attr_value_length = \
        PhantomChessCoordinator._is_invalid_attr_value_length
    stub._game_start_length_error = types.MethodType(
        PhantomChessCoordinator._game_start_length_error, stub
    )
    return stub


async def test_game_start_0x0d_raises_actionable_runtimeerror():
    err = BleakError("GATT Protocol Error: Invalid Attribute Value Length")
    stub = _game_start_raising_stub(err)
    raised = None
    try:
        await PhantomChessCoordinator._phantom_send_game_start(stub)
    except RuntimeError as e:
        raised = e
    assert raised is not None
    msg = str(raised)
    assert "attr_max_len" in msg and "103" in msg
    assert raised.__cause__ is err  # chained from the original BLE error


async def test_game_start_non_length_error_reraised_unchanged():
    err = Exception("Some other disconnect error")
    stub = _game_start_raising_stub(err)
    raised = None
    try:
        await PhantomChessCoordinator._phantom_send_game_start(stub)
    except Exception as e:  # noqa: BLE001 — asserting identity below
        raised = e
    assert raised is err  # re-raised as-is, not wrapped


# ── GAME_ASSISTANCE 8-field payload (doc §3.4) ──────────────────────────────


def _assist_stub():
    captured = {}

    async def fake_ble_write(uuid, payload, response=True):
        captured["payload"] = payload

    stub = types.SimpleNamespace()
    stub._ble_write = fake_ble_write
    return stub, captured


async def test_game_assistance_defaults_are_8_fields():
    stub, captured = _assist_stub()
    await PhantomChessCoordinator._phantom_send_game_assistance(stub)
    # opcode 0x0B + "C,E,S,W,A,G,SD,JC". Method defaults are tuned for
    # HA-driven play: C/E/S on, W(auto-correct) off, A/G off, SD on, JC off
    # → "1,1,1,0,0,0,1,0".
    assert captured["payload"] == b"\x0b" + b"1,1,1,0,0,0,1,0"
    # exactly 8 comma-separated fields after the opcode byte
    assert captured["payload"][1:].decode().split(",") == \
        ["1", "1", "1", "0", "0", "0", "1", "0"]


async def test_game_assistance_two_player_flags():
    stub, captured = _assist_stub()
    # The two-player caller passes auto_correct_wrong_move + advanced_capture on.
    await PhantomChessCoordinator._phantom_send_game_assistance(
        stub, auto_castling=True, auto_en_passant=True, auto_snap_to_center=True,
        auto_correct_wrong_move=True, advanced_capture=True, strict_gameplay=False,
    )
    assert captured["payload"] == b"\x0b" + b"1,1,1,1,1,0,1,0"


# ── _parse_battery_payload ──────────────────────────────────────────────────


def test_parse_battery_valid():
    assert PhantomChessCoordinator._parse_battery_payload(b"87,1,1,0") == (87, True)
    assert PhantomChessCoordinator._parse_battery_payload(b"42,0,0,0") == (42, False)


def test_parse_battery_malformed_returns_none():
    assert PhantomChessCoordinator._parse_battery_payload(b"") is None
    assert PhantomChessCoordinator._parse_battery_payload(b"not,a,number") is None
    assert PhantomChessCoordinator._parse_battery_payload(b"87") is None  # no charging field


# ── experimental GAME_START variant ladder ──────────────────────────────────


def _variant_stub(fail_until_index):
    """fail_until_index writes fail with 0x0D; the rest succeed."""
    calls = []

    async def fake_ble_write(uuid, payload, response=True):
        calls.append({"len": len(payload), "response": response, "payload": payload})
        if len(calls) <= fail_until_index:
            raise BleakError("GATT Protocol Error: Invalid Attribute Value Length")

    stub = types.SimpleNamespace()
    stub._board = chess.Board()
    stub._build_phantom_matrix_from_fen = build_matrix_from_fen
    stub._ble_write = fake_ble_write
    stub._is_invalid_attr_value_length = \
        PhantomChessCoordinator._is_invalid_attr_value_length
    return stub, calls


async def test_variant_ladder_first_success_stops():
    stub, calls = _variant_stub(fail_until_index=0)  # full-103B succeeds
    out = await PhantomChessCoordinator._diagnose_game_start_variants(stub)
    assert len(calls) == 1
    assert calls[0]["len"] == 103 and calls[0]["response"] is True
    assert any("VARIANT OK" in line and "full-103B" in line for line in out)


async def test_variant_ladder_falls_through_to_no_suffix():
    stub, calls = _variant_stub(fail_until_index=1)  # full fails, no-suffix ok
    out = await PhantomChessCoordinator._diagnose_game_start_variants(stub)
    assert [c["len"] for c in calls] == [103, 101]      # ordering + sizes
    assert calls[1]["response"] is True
    assert any("VARIANT OK" in line and "no-suffix" in line for line in out)


async def test_variant_ladder_all_fail_escalates():
    stub, calls = _variant_stub(fail_until_index=99)  # everything fails
    out = await PhantomChessCoordinator._diagnose_game_start_variants(stub)
    # full(103,resp=True), no-suffix(101,resp=True), write-without-response(103,resp=False)
    assert [(c["len"], c["response"]) for c in calls] == \
        [(103, True), (101, True), (103, False)]
    assert any("attr_max_len regression" in line for line in out)


# ── UUID_GAME write mode (fw0.3.2/0.3.3 investigation) ───────────────────────
# A write-WITHOUT-response auto-switch for UUID_GAME on fw>=0.3.2 was tried and
# REVERTED 2026-06-27: live testing showed the firmware silently DROPS
# write-without-response on UUID_GAME (it only advertises Write/Request), so it
# masked the 0x0D rejection with a fake success. HCI captures show the official
# app used write-WITH-response on 0.3.0. _ble_write therefore defaults to
# response=True for every caller; only the diagnostic A/B passes an explicit
# response= to force a mode. This guards against re-introducing the auto-switch.


class _CapClient:
    is_connected = True

    def __init__(self):
        self.calls = []

    async def write_gatt_char(self, uuid, data, response=True):
        self.calls.append({"uuid": uuid, "data": data, "response": response})


def _ble_write_stub():
    stub = types.SimpleNamespace()
    stub._ble_client = _CapClient()
    stub._ble_write = types.MethodType(PhantomChessCoordinator._ble_write, stub)
    return stub


async def test_ble_write_defaults_to_with_response_for_game_channel():
    stub = _ble_write_stub()
    await stub._ble_write(UUID_GAME, b"\x00abc")
    assert stub._ble_client.calls[0]["response"] is True


# ── _fw_at_least version gate ────────────────────────────────────────────────


def _fw_stub(fw):
    stub = types.SimpleNamespace(_state={"firmware_version": fw})
    stub._fw_at_least = types.MethodType(PhantomChessCoordinator._fw_at_least, stub)
    return stub


def test_fw_at_least_parses_and_compares():
    assert _fw_stub("0.3.3")._fw_at_least((0, 3, 2)) is True
    assert _fw_stub("0.3.2")._fw_at_least((0, 3, 2)) is True
    assert _fw_stub("0.3.1")._fw_at_least((0, 3, 2)) is False
    assert _fw_stub("0.3.0")._fw_at_least((0, 3, 2)) is False
    assert _fw_stub("0.4.0")._fw_at_least((0, 3, 2)) is True
    assert _fw_stub("0.3")._fw_at_least((0, 3, 2)) is False       # 0.3 < 0.3.2
    assert _fw_stub("0.3.3-beta")._fw_at_least((0, 3, 2)) is True
    assert _fw_stub("")._fw_at_least((0, 3, 2)) is False          # unknown → False
    assert _fw_stub(None)._fw_at_least((0, 3, 2)) is False


# ── SELECT_MODE 2 before GAME_START (the fw0.3.3 game-start fix) ──────────────
# Sniffer capture 2026-06-29 proved the app does GAME_END → SELECT_MODE 2 →
# GAME_START on fw0.3.3; without SELECT_MODE 2 the firmware 0x0D-rejects every
# UUID_GAME write. _phantom_execute_position(select_chess_mode=True) restores
# it, version-gated to fw>=0.3.2 so the 0.3.0 path is unchanged.


def _exec_pos_stub(fw_version):
    stub = types.SimpleNamespace()
    stub._ble_connected = True
    stub._phantom_session_initialized = True  # skip drop_to_home
    stub._state = {"firmware_version": fw_version, "firmware_mode": "Waiting Side"}
    stub._fw_at_least = types.MethodType(PhantomChessCoordinator._fw_at_least, stub)
    stub.hass = types.SimpleNamespace(loop=asyncio.get_running_loop())
    stub._activation_settle_until = 0.0
    stub._last_target_fen = None
    stub._move_done_future = None
    calls = {"select_mode": 0, "game_start": 0, "side": 0}

    async def _select():
        calls["select_mode"] += 1

    async def _game_start(fen=chess.STARTING_FEN, side="W"):
        calls["game_start"] += 1

    async def _send_side(side_value):
        calls["side"] += 1
        if stub._move_done_future and not stub._move_done_future.done():
            stub._move_done_future.set_result(True)  # release the wait

    stub._phantom_select_chess_play_mode = _select
    stub._phantom_send_game_start = _game_start
    stub._phantom_send_side = _send_side
    return stub, calls


async def test_start_sends_select_mode_on_fw033():
    stub, calls = _exec_pos_stub("0.3.3")
    ok = await PhantomChessCoordinator._phantom_execute_position(
        stub, fen=chess.STARTING_FEN, side="W", timeout_s=2.0, select_chess_mode=True
    )
    assert ok is True
    assert calls["select_mode"] == 1       # chess-play mode set before GAME_START
    assert calls["game_start"] == 1


async def test_start_skips_select_mode_on_fw030():
    stub, calls = _exec_pos_stub("0.3.0")
    await PhantomChessCoordinator._phantom_execute_position(
        stub, fen=chess.STARTING_FEN, side="W", timeout_s=2.0, select_chess_mode=True
    )
    assert calls["select_mode"] == 0       # 0.3.0 path unchanged
    assert calls["game_start"] == 1


async def test_position_execute_never_selects_mode_when_flag_false():
    # per-move AI / move_piece path must NOT re-enter chess-mode mid-game
    stub, calls = _exec_pos_stub("0.3.3")
    await PhantomChessCoordinator._phantom_execute_position(
        stub, fen=chess.STARTING_FEN, side="B", timeout_s=2.0
    )
    assert calls["select_mode"] == 0
    assert calls["game_start"] == 1


async def test_ble_write_honours_explicit_response_false():
    stub = _ble_write_stub()
    await stub._ble_write(UUID_GAME, b"\x00abc", response=False)
    assert stub._ble_client.calls[0]["response"] is False

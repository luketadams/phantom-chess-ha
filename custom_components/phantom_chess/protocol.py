"""Bluetooth game-channel commands, diagnostics and physical execution.

Methods of PhantomChessCoordinator, defined here as functions and bound
onto the class in coordinator.py. Moved verbatim from coordinator.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import PhantomChessCoordinator  # noqa: F401

import chess


from .issues import clear_ble_route_issue, raise_ble_route_issue
from . import runtime as rt
from .runtime import (  # noqa: F401 — shared names
    AI_VS_AI_TWO_STEP_SETTLE_S,
    BleakClient,
    BleakError,
    BleakGATTCharacteristic,
    _phantom_to_uci,
    _rotate_uci_180,
    _is_move_frame,
    _build_matrix_from_fen_module,
    _check_consistency,
    _diff_grid_vs_sensor,
    _format_mismatch_instructions,
    _grid_to_fen,
    _parse_matrix_notification,
)

from .const import (
    SETTLE_TIMEOUT_TRIM_SECONDS,
    STATUS_PAUSED,
    UUID_GAME,
    UUID_SELECT_MODE,
)

# Same logger as before the split, so log filters keep working.
_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".coordinator")


async def _ble_write(
    self, uuid: str, data: str | bytes, response: bool = True
) -> None:
    """Write to a characteristic.

    ``response`` selects write-WITH-response (ATT Write Request, the
    default and the only mode used by the 0.3.0 path) vs
    write-WITHOUT-response (ATT Write Command). The kwarg exists so the
    fw0.3.2 GAME_START diagnostics can A/B the two modes on the live
    board; every existing caller keeps the response=True behaviour.

    NOTE (fw0.3.2/0.3.3, 2026-06-27): a write-without-response auto-switch
    for UUID_GAME was tried and reverted. Live testing showed the firmware
    SILENTLY DROPS write-without-response on UUID_GAME (the characteristic
    only advertises Write/Request), so it merely masked the 0x0D rejection
    with a fake success. HCI captures show the official app used
    write-WITH-response (no bonding) on 0.3.0; 0.3.3 newly rejects it. The
    real cause (bonding/handshake?) is still under investigation — see
    FW032_GAME_START_FINDINGS.md, so we keep the honest with-response path.
    """
    if self._ble_client is None or not self._ble_client.is_connected:
        raise RuntimeError("BLE not connected")
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        await self._ble_client.write_gatt_char(uuid, data, response=response)
    except BleakError as err:
        await self._handle_gatt_staleness(err, uuid, op="write")
        raise  # always propagate; caller decides retry policy


async def async_debug_ble_write(self, uuid: str, data: str) -> None:
    """Diagnostic — write arbitrary payload to an arbitrary BLE characteristic.

    Data is UTF-8 by default. Prefix with "hex:" for raw bytes
    (e.g. "hex:0102FF" or "hex:01 02 FF"). Logs at WARNING so probe attempts
    are visible in system logs.
    """
    if data.startswith("hex:"):
        payload = bytes.fromhex(data[4:].replace(" ", ""))
    else:
        payload = data.encode("utf-8")
    _LOGGER.warning(
        "DEBUG_BLE_WRITE → uuid=%s data=%r (%d bytes)", uuid, payload, len(payload)
    )
    try:
        await self._ble_write(uuid, payload)
        _LOGGER.warning("DEBUG_BLE_WRITE OK uuid=%s", uuid)
    except Exception as err:
        _LOGGER.warning("DEBUG_BLE_WRITE FAIL uuid=%s: %s", uuid, err)
        raise


def _game_channel_write_diag(self, payload_len: int) -> str:
    """One-line '0.3.2 diag' describing UUID_GAME's write limits.

    Best-effort and never raises. Logs the negotiated MTU, the implied
    single-ATT-write cap (MTU-3) and whether ``payload_len`` fits it, plus
    the UUID_GAME characteristic's ``max_write_without_response_size`` and
    ``properties``.

    NOTE (BlueZ): ``BleakClient.mtu_size`` on the BlueZ backend can report
    the 23-byte default until an MTU exchange has been "acquired" (e.g. by
    a notify subscribe or a write-without-response). When the local adapter
    is BlueZ, trust ``max_write_without_response_size`` over ``mtu_size``;
    both are logged so the live reader can compare.
    """
    client = self._ble_client
    mtu: int | None = None
    max_wwr: int | None = None
    props: list[str] = []
    try:
        if client is not None:
            try:
                mtu = int(client.mtu_size)
            except Exception:  # noqa: BLE001 — BlueZ may warn/raise pre-acquire
                mtu = None
            char = None
            try:
                char = client.services.get_characteristic(UUID_GAME)
            except Exception:  # noqa: BLE001
                char = None
            if char is not None:
                try:
                    props = list(char.properties)
                except Exception:  # noqa: BLE001
                    props = []
                try:
                    max_wwr = int(char.max_write_without_response_size)
                except Exception:  # noqa: BLE001
                    max_wwr = None
    except Exception:  # noqa: BLE001 — diagnostics must never break a write
        pass
    single_cap = (mtu - 3) if isinstance(mtu, int) else None
    if single_cap is None:
        fits = "unknown"
    else:
        fits = "yes" if payload_len <= single_cap else "NO"
    return (
        f"0.3.2 diag: UUID_GAME payload={payload_len}B; mtu_size={mtu} "
        f"(single-ATT-write cap={single_cap}B; payload fits single write: "
        f"{fits}); max_write_without_response_size={max_wwr}B; "
        f"properties={props}"
    )


def _game_start_length_error(
    self, err: BaseException, payload_len: int, diag: str
) -> RuntimeError:
    """Build an actionable RuntimeError from a GAME_START 0x0D rejection.

    Turns the opaque ``BleakGATTProtocolError`` (which otherwise surfaces
    as a bare HTTP 500) into a message that interprets the diag numbers
    into the two candidate root causes, so the live operator knows whether
    this is fixable host-side (MTU) or needs an upstream firmware fix
    (attr_max_len).
    """
    msg = (
        f"GAME_START rejected by the board: the {payload_len}-byte write to "
        f"UUID_GAME was refused with INVALID_ATTRIBUTE_VALUE_LENGTH (ATT "
        f"0x0D) — a server-side 'value too long' rejection. The payload "
        f"matches the fw0.3.2 doc §2.1 byte-for-byte, so this is a length "
        f"limit, not a format bug. {diag}. Interpretation: if 'payload fits "
        f"single write: yes' then the link MTU is fine and the firmware's "
        f"UUID_GAME attr_max_len is < {payload_len} (a 0.3.2 firmware "
        f"regression — needs an upstream fix from Efraín; takeback/opcode-5 "
        f"and reset_detection/opcode-14 large writes will fail the same "
        f"way). If 'fits: NO' then the negotiated MTU is too small to carry "
        f"a single {payload_len}-byte ATT write and the board's GATT server "
        f"isn't accepting a long (prepared) write — raise the MTU / use a "
        f"higher-MTU transport. Run phantom_chess.diagnose_game_start for a "
        f"non-destructive confirmation. Original error: {err!r}"
    )
    return RuntimeError(msg)


async def async_diagnose_game_start(self, experimental: bool = False) -> str:
    """Operator-invoked fw0.3.2 GAME_START diagnostic.

    Step 1 (always, non-destructive): log the MTU/char diag, then probe
    UUID_GAME with a SAFE documented write — RESET_DETECTION (opcode 14)
    seeded with the *current* board FEN, which re-asserts the firmware's
    expected matrix WITHOUT driving the magnet (see async_resync_detection).
    Comparing that ~35-90B write against the known 103B GAME_START failure
    localises the attr_max_len ceiling.

    Step 2 (only if ``experimental=True``): A/B the candidate GAME_START
    fixes on the live board (doc-compliant 103B, experimental no-suffix
    101B, then write-without-response), stopping at the first that
    succeeds. A successful write starts a game (the desired end state); a
    rejected write has no side effect because an ATT-rejected value never
    reaches the firmware.

    Returns a human summary; every step is also logged at INFO.
    """
    if not self._ble_connected:
        raise RuntimeError("Board not connected via Bluetooth")
    lines: list[str] = []
    diag = self._game_channel_write_diag(103)
    lines.append(diag)
    _LOGGER.info("diagnose_game_start — %s", diag)

    board_fen = self._board.board_fen()
    rd_len = 1 + len(board_fen.encode("utf-8"))
    try:
        await self._phantom_send_reset_detection(board_fen)
        lines.append(
            f"RESET_DETECTION probe OK: a {rd_len}-byte UUID_GAME write "
            f"(opcode 14, non-destructive) succeeded — so the channel "
            f"accepts writes at least {rd_len}B. If GAME_START's 103B write "
            f"still fails, the firmware attr_max_len is between {rd_len} and "
            f"103 (a 0.3.2 regression)."
        )
    except BleakError as err:
        if self._is_invalid_attr_value_length(err):
            lines.append(
                f"RESET_DETECTION probe FAILED at {rd_len}B with the same "
                f"0x0D length rejection — attr_max_len is smaller than "
                f"{rd_len}B, or the MTU is too low even for that write."
            )
        else:
            lines.append(f"RESET_DETECTION probe FAILED ({rd_len}B): {err!r}")

    if experimental:
        lines.extend(await self._diagnose_game_start_variants())

    summary = " | ".join(lines)
    _LOGGER.info("diagnose_game_start summary: %s", summary)
    return summary


async def _diagnose_game_start_variants(self) -> list[str]:
    """EXPERIMENTAL: A/B GAME_START write variants on the live board.

    Ordered; stops at the first variant the firmware accepts. A successful
    write starts a game; failed writes are ATT-rejected (no side effect).
    Invoked only via diagnose_game_start(experimental=True).
    """
    matrix = self._build_phantom_matrix_from_fen(self._board.fen())
    full = bytes([0x00]) + (matrix + ",W").encode("utf-8")          # 103B, §2.1
    no_suffix = bytes([0x00]) + matrix.encode("utf-8")              # 101B, no ",W"
    attempts = [
        ("full-103B response=True (doc §2.1)", full, True),
        (
            "no-suffix-101B response=True (EXPERIMENTAL — deviates from "
            "§2.1; SIDE is set separately via opcode 10 so ',W' may be "
            "optional)",
            no_suffix,
            True,
        ),
        ("full-103B response=False (write-without-response)", full, False),
    ]
    out: list[str] = []
    for label, payload, resp in attempts:
        try:
            await self._ble_write(UUID_GAME, payload, response=resp)
            out.append(
                f"VARIANT OK → {label} ({len(payload)}B) accepted. This is "
                f"the working write mode — promote it to the start path."
            )
            return out
        except BleakError as err:
            kind = (
                "0x0D length-reject"
                if self._is_invalid_attr_value_length(err)
                else "other error"
            )
            out.append(f"variant [{label}] ({len(payload)}B) failed "
                       f"[{kind}]: {err!r}")
    out.append(
        "All GAME_START variants failed → not host-fixable from the write "
        "path; this is a firmware attr_max_len regression to escalate to "
        "Efraín (opcode-5 takeback and opcode-14 reset_detection large "
        "writes will fail the same way)."
    )
    return out


async def _phantom_send_game_start(self, fen: str = chess.STARTING_FEN, side: str = "W") -> None:
    """Send GameOPCode 0 (gameStart) with column-major matrix to UUID_GAME.

    Payload is opcode 0 + the 100-char column-major matrix + ",<side>",
    matching the fw0.3.2 doc §2.1 (a 103-byte write for a 1-char side).
    On fw0.3.2 the board's GATT server may reject this with ATT 0x0D
    (INVALID_ATTRIBUTE_VALUE_LENGTH); we emit a one-shot MTU/char diag
    before the write and translate the opaque rejection into an actionable
    error. The 0.3.0 happy path is unchanged (the write succeeds; the diag
    logs once at INFO, then DEBUG).
    """
    GAME_CHANNEL = UUID_GAME
    matrix = self._build_phantom_matrix_from_fen(fen)
    payload = bytes([0x00]) + (matrix + "," + side).encode("utf-8")
    diag = self._game_channel_write_diag(len(payload))
    if not self._game_start_diag_logged:
        _LOGGER.info("Phantom gameStart — %s", diag)
        self._game_start_diag_logged = True
    else:
        _LOGGER.debug("Phantom gameStart — %s", diag)
    _LOGGER.debug("Phantom gameStart: %d bytes (matrix=%s, side=%s)", len(payload), matrix, side)
    try:
        await self._ble_write(GAME_CHANNEL, payload)
    except BleakError as err:
        if self._is_invalid_attr_value_length(err):
            _LOGGER.error("Phantom gameStart length-rejected — %s", diag)
            self._set_route_issue(rejected=True)
            raise self._game_start_length_error(err, len(payload), diag) from err
        raise
    self._set_route_issue(rejected=False)


def _set_route_issue(self, *, rejected: bool) -> None:
    """Raise or clear the Bluetooth-route repair issue; never breaks play."""
    try:
        if rejected:
            raise_ble_route_issue(
                self.hass, self._ble_address, self._state.get("firmware_version")
            )
        else:
            clear_ble_route_issue(self.hass, self._ble_address)
    except Exception:  # noqa: BLE001 — the registry is advisory only
        _LOGGER.debug("Could not update the Bluetooth route repair issue", exc_info=True)


async def _phantom_send_side(self, side_value: str) -> None:
    """Send GameOPCode 10 (side) with payload '0', '1', or '2'.

    Per EFRAIN_GAMEPLAY_DOC and XOUXOU_PROTOCOL, SIDE encodes *who moves
    next*, NOT a piece colour:
      '0' = two local players (board waits for either human),
      '1' = the board/firmware side moves next,
      '2' = the BLE/app side moves next.
    (The older 'white/black-to-move' reading was a debunked heuristic.)
    """
    GAME_CHANNEL = UUID_GAME
    payload = bytes([0x0a]) + side_value.encode("utf-8")
    _LOGGER.debug("Phantom side: %r", side_value)
    await self._ble_write(GAME_CHANNEL, payload)


async def _phantom_send_movement_verify(self, value: str = "1") -> None:
    """Send GameOPCode 3 (movementVerify) — confirms a human move was detected."""
    GAME_CHANNEL = UUID_GAME
    payload = bytes([0x03]) + value.encode("utf-8")
    await self._ble_write(GAME_CHANNEL, payload)


async def _phantom_send_game_end(self) -> None:
    """Send GameOPCode 1 (gameEnd) — drops firmware to HOME state.

    No payload. Used as a precondition before the first GAME_START in a
    BLE session so that the subsequent snapshot actuates the magnet
    rather than just being absorbed as a state-initialization.
    """
    GAME_CHANNEL = UUID_GAME
    _LOGGER.debug("Phantom gameEnd: \\x01")
    await self._ble_write(GAME_CHANNEL, bytes([0x01]))


async def _phantom_send_game_assistance(
    self,
    auto_castling: bool = True,
    auto_en_passant: bool = True,
    auto_snap_to_center: bool = True,
    auto_correct_wrong_move: bool = False,
    advanced_capture: bool = False,
    strict_gameplay: bool = False,
    slide_detection: bool = True,
    jump_to_center: bool = False,
) -> None:
    """Send GameOPCode 11 (GAME_ASSISTANCE) with the firmware assistance flags.

    Firmware 0.3.2 added two fields → the 8-field form "C,E,S,W,A,G,SD,JC".
      SD = slideDetectionMode (debounced slide detection; firmware
           default ON — fixes the slow-slide double-move bug)
      JC = autoJumpToCenter   (firmware default OFF)
    We always send all 8 (SD on, JC off, matching the firmware defaults).

    DOC CONTRADICTION (flagged 2026-06-14): the authoritative 0.3.2 doc
    §3.4 NOTE claims "Sending only 6 fields is safe — firmware defaults
    SD=1, JC=0 if fields are absent." That directly contradicts the live
    capture (2026-06-09) where the 6-field write was rejected with
    INVALID_ATTRIBUTE_VALUE_LENGTH. Sending 8 fields is correct and safe
    under BOTH readings (it's a strict superset of the 6-field form and
    the 0.3.0 firmware simply ignored positions 6/7), so we send 8
    unconditionally rather than depend on the unverified default-fill.
    If the live board later confirms 6 fields really is accepted, only
    this comment needs revisiting — the wire write stays 8-field.

    Data format: "C,E,S,W,A,G,SD,JC" with each value "0" or "1":
      C = autoCastling           (firmware moves the rook for you on king-castle)
      E = autoEnPassant          (firmware removes the captured pawn)
      S = autoSnapToCenter       (firmware centers misaligned pieces)
      W = autoCorrectWrongMove   (centers/reconciles piece-placement mismatches;
                                  enabled for local and online play)
      A = advancedCapture        (firmware's advanced capture logic)
      G = strictGameplay         (firmware tracks graveyard and beeps on mismatch)

    Defaults here are tuned for HA-driven play where we own the game state:
    the firmware's auto-correct and strict-gameplay behaviors fight our snapshot
    writes and produce desyncs/beeps. The first three (castling, en passant,
    snap-to-center) are useful and stay on.

    Reference: EFRAIN_GAMEPLAY_DOC_2026-05-14.txt opcode 11.
    """
    GAME_CHANNEL = UUID_GAME
    flags = "{},{},{},{},{},{},{},{}".format(
        "1" if auto_castling else "0",
        "1" if auto_en_passant else "0",
        "1" if auto_snap_to_center else "0",
        "1" if auto_correct_wrong_move else "0",
        "1" if advanced_capture else "0",
        "1" if strict_gameplay else "0",
        "1" if slide_detection else "0",
        "1" if jump_to_center else "0",
    )
    payload = bytes([0x0B]) + flags.encode()
    _LOGGER.debug("Phantom game_assistance (8-field, fw0.3.2): %s", flags)
    await self._ble_write(GAME_CHANNEL, payload)


async def _phantom_send_check_sound(self, sound_type: str = "1") -> None:
    """Send GameOPCode 9 (CHECK_SOUND) — fires the firmware's native sound effect.

    Data: "1" for check, "2" for checkmate.

    Reference: EFRAIN_GAMEPLAY_DOC_2026-05-14.txt opcode 9.
    """
    if sound_type not in ("1", "2"):
        raise ValueError(f"sound_type must be '1' (check) or '2' (checkmate), got {sound_type!r}")
    GAME_CHANNEL = UUID_GAME
    payload = bytes([0x09]) + sound_type.encode()
    _LOGGER.debug("Phantom check_sound: %s", "check" if sound_type == "1" else "checkmate")
    await self._ble_write(GAME_CHANNEL, payload)


async def _phantom_send_reset_detection(self, fen: str) -> None:
    """Send GameOPCode 14 (RESET_DETECTION) — resync firmware's expected matrix to a FEN.

    Unlike GAME_END + GAME_START, this updates the firmware's *expected* position
    in place without driving pieces — useful when we want the firmware to stop
    complaining about a mismatch we've already accepted, or to reset its model
    after a manual repositioning. Pass the board-only FEN (no metadata fields).

    Reference: EFRAIN_GAMEPLAY_DOC_2026-05-14.txt opcode 14.
    """
    GAME_CHANNEL = UUID_GAME
    board_only_fen = fen.split(" ")[0]
    payload = bytes([0x0E]) + board_only_fen.encode()
    _LOGGER.debug("Phantom reset_detection: %s", board_only_fen)
    await self._ble_write(GAME_CHANNEL, payload)


async def _phantom_drop_to_home(self, timeout: float = 30.0) -> None:
    """Send GAME_END and block until firmware reports HOME mode.

    Required as a precondition before the first GAME_START in a BLE
    session. Without it, GAME_START silently transitions to BLE Playing
    without driving the motor. See XOUXOU_PROTOCOL.md "Critical
    precondition" section.

    Timeout was 5s originally — observed end-of-game scenarios where
    the firmware is still settling from the last move take longer. 30s
    is the same order as the GAME_START move-done timeout, and a true
    failure shouldn't take longer than that to surface.
    """
    await self._phantom_send_game_end()
    deadline = self.hass.loop.time() + timeout
    while self.hass.loop.time() < deadline:
        if self._state.get("firmware_mode") == "HOME":
            _LOGGER.debug("Phantom: firmware reached HOME after GAME_END")
            return
        await rt._sleep(0.1)
    raise TimeoutError(
        f"The board did not return to its home state within {timeout:.0f}s "
        f"(firmware mode: {self._state.get('firmware_mode')!r}). Make sure no "
        "piece is lifted or between squares, then try again."
    )


async def _phantom_execute_position(
    self, fen: str, side: str = "B", timeout_s: float = 30.0,
    side_opcode: str = "2", select_chess_mode: bool = False,
) -> bool:
    """Own the completion channel for one physical operation at a time."""
    if self._physical_operation_lock.locked():
        raise RuntimeError("The board is still moving. Wait for it to finish.")
    async with self._physical_operation_lock:
        self._state["physical_operation"] = "moving"
        self._state["position_confirmed"] = False
        self.async_set_updated_data(dict(self._state))
        try:
            confirmed = await self._execute_position_unlocked(
                fen, side, timeout_s, side_opcode, select_chess_mode,
            )
        except (Exception, asyncio.CancelledError):
            self._state["physical_operation"] = "uncertain"
            self.async_set_updated_data(dict(self._state))
            raise
        self._state["position_confirmed"] = confirmed
        self._state["physical_operation"] = "idle" if confirmed else "uncertain"
        self.async_set_updated_data(dict(self._state))
        return confirmed


async def _execute_position_unlocked(
    self,
    fen: str,
    side: str = "B",
    timeout_s: float = 30.0,
    side_opcode: str = "2",
    select_chess_mode: bool = False,
) -> bool:
    """Drive the magnet to match the given target FEN.

    Implements the validated xouxou snapshot model:
      1. (Once per BLE session) drop to HOME via GAME_END
      2. GAME_START (opcode 0) with the 100-char column-major matrix + side
      3. 300 ms gap
      4. SIDE (opcode 0x0A) with payload `side_opcode` — default "2" matches
         xouxou's spectator pattern (BLE-side is the next mover). Pass "1"
         for active-play scenarios where the human (board side) should
         move first — necessary for the Lichess/Stockfish flow when the
         human plays white. Once firmware transitions out of Waiting Side
         this opcode is no-op, so the SIDE must be written ON this initial
         snapshot sequence — there's no retry path.
      5. Block until BLE_MOVE_DONE (opcode 0x0C) notification or timeout

    Args:
        fen: target board-only FEN or full FEN; only the position field
             (chars before the first space) is used to build the matrix.
        side: "W" or "B" — xouxou convention is the color of the piece
              that just moved. For a fresh game start, use "B".
        timeout_s: max seconds to wait for BLE_MOVE_DONE before giving up.
        side_opcode: "0" (2-local-player), "1" (board moves next), "2"
              (BLE moves next). See Efraín's gameplay doc opcode 10.

    Returns:
        True if BLE_MOVE_DONE arrived before timeout, False otherwise.
    """
    if not self._ble_connected:
        raise RuntimeError("BLE not connected")
    side = side.upper()
    if side not in ("W", "B"):
        raise ValueError(f"side must be 'W' or 'B', got {side!r}")
    if side_opcode not in ("0", "1", "2"):
        raise ValueError(f"side_opcode must be '0'/'1'/'2', got {side_opcode!r}")

    # Session-init: drop to HOME before the first move of a BLE session.
    if not self._phantom_session_initialized:
        await self._phantom_drop_to_home()
        self._phantom_session_initialized = True

    # fw0.3.2/0.3.3: the firmware rejects EVERY UUID_GAME write (even a
    # 1-byte GAME_END) with INVALID_ATTRIBUTE_VALUE_LENGTH (0x0D) unless it
    # has first been put into chess-play mode via SELECT_MODE 2. The
    # official app does exactly GAME_END → SELECT_MODE 2 → GAME_START
    # (confirmed by nRF52840 sniffer capture 2026-06-29,
    # phantom_app_fw033_capture). The 0.3.0 "snapshot" bootstrap didn't
    # need this, so version-gate to avoid changing the 0.3.0 path (where
    # SELECT_MODE 2 re-introduced the manual piece-touch oscillation).
    # Only on a fresh game start (select_chess_mode=True), never on the
    # per-move AI/move_piece position-execute path.
    if select_chess_mode and self._fw_at_least((0, 3, 2)):
        _LOGGER.debug("Phantom: entering chess-play mode (SELECT_MODE 2) before GAME_START")
        await self._phantom_select_chess_play_mode()
        await rt._sleep(0.2)

    # Cache target for the CLEAN: Match parser and the move-done future.
    self._last_target_fen = fen.split(" ")[0]
    self._move_done_future = self.hass.loop.create_future()
    # Open the post-activation move-detection suppression window
    # BEFORE the GAME_START write so the firmware's first sensor
    # events (which arrive within ~50ms of the write) are already
    # filtered. 600s upper bound (safety net only); the
    # BLE_MOVE_DONE handler clears the window the moment the magnet
    # sequence completes, which is the real release condition. See
    # __init__ for full rationale.
    self._activation_settle_until = self.hass.loop.time() + 600.0
    try:
        await self._phantom_send_game_start(fen=fen, side=side)
        # FIX (2026-05-14 morning): poll for firmware_mode == "Waiting Side"
        # before sending SIDE. The previous fixed 300ms sleep was a guess;
        # observed in logs that firmware was still in "Setting Up" when SIDE
        # arrived, causing SIDE writes to be silently ignored and firmware
        # to stay stuck in Waiting Side forever (instead of transitioning to
        # Board Playing). Now we actively wait up to 5s for Waiting Side.
        deadline = self.hass.loop.time() + 5.0
        while self.hass.loop.time() < deadline:
            if self._state.get("firmware_mode") == "Waiting Side":
                break
            await rt._sleep(0.05)
        else:
            _LOGGER.warning(
                "Phantom: Waiting Side not reached within 5s after GAME_START "
                "(current mode: %r); sending SIDE anyway",
                self._state.get("firmware_mode"),
            )
        # Small additional gap so SIDE write doesn't race the state-notify path
        await rt._sleep(0.1)
        await self._phantom_send_side(side_opcode)
        try:
            await asyncio.wait_for(self._move_done_future, timeout=timeout_s)
            _LOGGER.debug("Phantom: BLE_MOVE_DONE received within timeout")
            return True
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Phantom: BLE_MOVE_DONE not received within %.1fs; "
                "move may not have actuated (current firmware_mode: %r)",
                timeout_s, self._state.get("firmware_mode"),
            )
            # Fix A1: 0x0c never arrived, so it can't clear the 600s settle
            # backstop — its remaining ~570s is a dead zone that ate the live
            # c4-d5 human frame (51s post-drive) on 2026-07-08. We already
            # waited timeout_s for the magnet; trim the window to a short tail
            # so a human move landing after the drive replays instead of
            # vanishing. `min` so we never EXTEND an already-shorter window.
            self._activation_settle_until = min(
                self._activation_settle_until,
                self.hass.loop.time() + SETTLE_TIMEOUT_TRIM_SECONDS,
            )
            return False
    finally:
        self._move_done_future = None


async def async_move_piece(
    self,
    from_square: str,
    to_square: str,
    capture: bool = False,
    piece: str = "E",
) -> None:
    """Move a piece on the physical board from any square to any other square.

    Snapshot-based primitive: takes the current internal board state,
    applies the move as a raw piece relocation (no chess legality check),
    builds the post-move matrix, and drives the magnet via the validated
    xouxou GAME_START flow.

    For arbitrary state transitions (e.g. setting up a position from a
    FEN), prefer `_phantom_execute_position` directly.

    Args:
        from_square: source square in algebraic notation, e.g. 'e2'.
        to_square: destination square in algebraic notation, e.g. 'e4'.
        capture: kept for API compatibility; currently unused (the firmware
                 resolves the move semantics from the matrix diff).
        piece: kept for API compatibility; currently unused.
    """
    if self._physical_operation_lock.locked():
        raise RuntimeError("The board is still moving. Wait for it to finish.")
    if not self._ble_connected:
        raise RuntimeError("BLE not connected")
    if not (len(from_square) == 2 and len(to_square) == 2):
        raise ValueError(
            f"from_square and to_square must be 2-char algebraic "
            f"notation (got {from_square!r}, {to_square!r})"
        )
    from_square = from_square.lower()
    to_square = to_square.lower()
    if (
        from_square[0] not in "abcdefgh"
        or from_square[1] not in "12345678"
        or to_square[0] not in "abcdefgh"
        or to_square[1] not in "12345678"
    ):
        raise ValueError(
            f"squares must be a-h + 1-8 (got {from_square!r}, {to_square!r})"
        )

    # Build the post-move position by manipulating self._board directly.
    # Using remove_piece_at + set_piece_at on the chess.Board sidesteps
    # turn-tracking and chess-rule validation, so this works for any
    # piece relocation (legal chess move or arbitrary positioning).
    src_sq = chess.parse_square(from_square)
    dst_sq = chess.parse_square(to_square)
    moving = self._board.remove_piece_at(src_sq)
    if moving is None:
        _LOGGER.debug(
            "move_piece: no piece on %s in current internal board; "
            "proceeding anyway — firmware will drive based on matrix diff",
            from_square,
        )
    # Overwrite destination (capture or move); python-chess handles either.
    if moving is not None:
        self._board.set_piece_at(dst_sq, moving)
    new_fen = self._board.board_fen()
    _LOGGER.debug(
        "move_piece: %s → %s, target FEN = %s", from_square, to_square, new_fen
    )

    ok = await self._phantom_execute_position(fen=new_fen, side="B")

    # Update all dashboard-visible state derived from self._board so the
    # dashboard (live_position FEN, piece_grid, last_move) stays in sync
    # with reality. Without this, subsequent move_piece calls operate on
    # a stale self._board, AND the dashboard's piece_grid attribute drifts
    # from the live_fen FEN state.
    self._state["live_fen"] = self._board.board_fen()
    self._state["last_move"] = f"{from_square}{to_square}"
    grid = self._build_phantom_matrix_from_fen(self._board.fen())
    self._state["piece_grid"] = grid
    self._state["piece_count"] = sum(1 for c in grid if c != ".")
    # Keep CLEAN: Match parser cache aligned with the new state.
    self._last_target_fen = self._board.board_fen()
    self.async_set_updated_data(dict(self._state))

    if not ok:
        _LOGGER.warning(
            "move_piece: BLE_MOVE_DONE timed out; firmware/board may be "
            "out of sync with integration's view"
        )


async def _phantom_send_ai_move(self, uci: str, piece: str = "E") -> None:
    """Send GameOPCode 2 (MOVEMENT) with M-format payload — the explicit
    AI-move-during-active-game path.

    ⚠️ CURRENTLY UNUSED + UNVERIFIED (2026-06-09 audit). The whole
    integration drives moves via the snapshot model
    (`_phantom_execute_position`); this opcode-2 path is never called.
    It is ALSO suspect: per EFRAIN_GAMEPLAY_DOC the MOVEMENT payload
    needs a move-index prefix (`MOVE_PREFIX = "M 1 "`, i.e.
    "M 1 e2-e4 E"), but the string built below omits the index. Do NOT
    re-wire this path without verifying the exact wire format on live
    hardware first; prefer `_phantom_execute_position`.

    The opcode-2 MOVEMENT write tells the firmware exactly which move
    to physically execute, overriding whatever its onboard AI would
    choose. Use during active games AFTER detecting a human move via
    the `\\x03M` notification; for arbitrary state setting (move_piece,
    start_game reset) use the snapshot-based `_phantom_execute_position`
    instead.

    uci is e.g. 'e2e4' or 'e4xd5' (capture). piece is the Phantom
    piece tag — the official iOS app uses 'E' as the trailing letter
    for engine moves.

    (Duplicate definition at line 1296 removed 2026-05-17 dead-code audit.)
    """
    GAME_CHANNEL = UUID_GAME
    # Detect capture via current python-chess state, then build M-string.
    try:
        move = chess.Move.from_uci(uci)
        capture = self._board.is_capture(move)
    except Exception:
        capture = False
    sep = "x" if capture else "-"
    from_sq = uci[:2]
    to_sq = uci[2:4]
    m_str = f"M {from_sq}{sep}{to_sq} {piece}"
    payload = bytes([0x02]) + m_str.encode("utf-8")
    _LOGGER.debug("Phantom movement: %r", m_str)
    await self._ble_write(GAME_CHANNEL, payload)


def _fw_at_least(self, target: tuple[int, ...]) -> bool:
    """True when the board's reported firmware_version >= ``target``.

    Parses the leading dotted-int run of ``firmware_version`` (e.g.
    "0.3.3" → (0, 3, 3)) and compares it tuple-wise. Unknown/garbage
    firmware → False (conservative: keep the historical 0.3.0 behaviour).
    """
    raw = (self._state.get("firmware_version") or "").strip()
    nums: list[int] = []
    for tok in raw.split("."):
        digits = ""
        for ch in tok:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        nums.append(int(digits))
    if not nums:
        return False
    return tuple(nums) >= target


async def _phantom_select_chess_play_mode(self) -> None:
    """Set the firmware to chess-play mode (mode 2) via UUID_SELECT_MODE."""
    await self._ble_write(UUID_SELECT_MODE, b"2")


async def async_phantom_start_game(
    self,
    fen: str = chess.STARTING_FEN,
    side: str = "W",
    wait_for_running_timeout_s: float = 30.0,
    side_opcode: str = "2",
) -> None:
    """Start a Phantom chess game using the snapshot protocol.

    Sequence (matches the official app's HCI capture 2026-06-29 on
    fw0.3.3):
      1. GAME_END (\\x01) → wait for firmware HOME mode (session init)
      2. SELECT_MODE 2 (chess-play mode) on fw>=0.3.2 — REQUIRED before
         GAME_START or the firmware 0x0D-rejects every UUID_GAME write
      3. GAME_START (opcode 0) with column-major matrix from FEN
      4. Wait for "Waiting Side", then SIDE (opcode 0x0A)
      5. Wait for BLE_MOVE_DONE if a diff exists, or for firmware to reach
         BLE Playing if no motor action is needed (sensors already match).

    History: 0.3.0 was driven by a SELECT_MODE-less "snapshot" bootstrap;
    fw0.3.2/0.3.3 broke it (every UUID_GAME write 0x0D-rejected). The
    SELECT_MODE 2 step is restored, version-gated, for 0.3.2+. If the
    physical board doesn't match the target FEN, the magnet drives the
    pieces to match before returning.

    Args:
        fen: target position FEN (board-only or full). Defaults to standard
             starting position.
        side: "W" or "B" — xouxou convention is the color that just moved.
              For a fresh start, "B" works (firmware just records the flag).
              Old default "W" is kept for caller compatibility.
        wait_for_running_timeout_s: max seconds to wait for BLE_MOVE_DONE.
    """
    if not self._ble_connected:
        raise RuntimeError("BLE not connected")
    side = side.upper()
    if side not in ("W", "B"):
        raise ValueError(f"side must be 'W' or 'B', got {side!r}")

    # Reset session-init flag so we always drop to HOME for a fresh game,
    # even if the integration thinks the session is already initialised.
    # A clean start-of-game GAME_END is cheap and avoids state ambiguity.
    self._phantom_session_initialized = False

    _LOGGER.debug(
        "Phantom start_game: snapshot protocol, target FEN = %s, side = %s",
        fen, side,
    )
    ok = await self._phantom_execute_position(
        fen=fen, side=side, timeout_s=wait_for_running_timeout_s,
        side_opcode=side_opcode, select_chess_mode=True,
    )
    if not ok:
        raise TimeoutError(
            "The chessboard did not confirm it was ready. "
            "Wait for the pieces to settle and try again."
        )
    _LOGGER.debug(
        "Phantom start_game: ACTIVATED. Firmware mode: %r",
        self._state.get("firmware_mode"),
    )


async def async_phantom_apply_ai_move(self, uci: str) -> bool:
    """Execute one target position; commit local moves only after confirmation.

    A transport error can arrive after the board accepted a command. Never
    retry an ambiguous physical operation automatically. Online games keep
    the stream-authoritative move; local games retain the last confirmed ply.
    """
    if self._physical_operation_lock.locked():
        raise RuntimeError("The board is still moving. Wait for it to finish.")
    if self._state.get("physical_operation") == "uncertain":
        raise RuntimeError("Reconcile the board position before making another move")
    if not self._ble_connected:
        raise RuntimeError("BLE not connected")
    mv = chess.Move.from_uci(uci)
    if mv not in self._board.legal_moves:
        raise ValueError(f"Move {uci} is not legal in the current position")
    session_board = self._board
    target = session_board.copy()
    speech = self._build_move_speech(mv) if self._should_announce_active_game() else ""
    self._set_last_ai_move(uci, mv=mv, pre_move_board=session_board.copy(stack=False))
    target.push(mv)
    online = bool(self._game_id)
    if online:
        session_board.push(mv)
    self._state["target_fen"] = target.fen()
    try:
        confirmed = await self._phantom_execute_position(
            fen=target.fen(), side="W" if self._our_color == chess.WHITE else "B",
            timeout_s=30.0, side_opcode="1",
        )
    except (Exception, asyncio.CancelledError):
        self._state["physical_operation"] = "uncertain"
        self._state["position_confirmed"] = False
        self.paused = True
        self._state["game_status"] = STATUS_PAUSED
        self._queue_checkpoint("uncertain")
        self.async_set_updated_data(dict(self._state))
        raise
    if not confirmed or self._board is not session_board:
        self._state["physical_operation"] = "uncertain"
        self._state["position_confirmed"] = False
        self.paused = True
        self._state["game_status"] = STATUS_PAUSED
        self._queue_checkpoint("uncertain")
        self.async_set_updated_data(dict(self._state))
        return False
    if not online:
        session_board.push(mv)
    self._state["target_fen"] = None
    self._state["live_fen"] = self._board.board_fen()
    self._state["last_move"] = uci
    grid = self._build_phantom_matrix_from_fen(self._board.fen())
    self._state["piece_grid"] = grid
    self._state["piece_count"] = sum(c != "." for c in grid)
    self._last_target_fen = self._board.board_fen()
    self.async_set_updated_data(dict(self._state))
    if speech:
        event = self._post_move_event_speech()
        self.hass.async_create_task(self._announce_via_tts((speech + ". " + event).strip(". ")))
    return True

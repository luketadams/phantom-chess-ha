"""Settle-window lifecycle + resign/back-to-modes/graveyard fixes.

Backs the four fixes in ``FINDINGS_SETTLE_FIX.md`` (live Lichess session
bLraP9m6, 2026-07-08). Each production behaviour change has a test that FAILS
on the pre-fix code — the falsifiability note per group cites the live episode.

Run (minimal-style env — no phacc needed)::

    pytest tests/test_settle_fix.py -p no:pytest_homeassistant_custom_component
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import chess

from custom_components.phantom_chess import const
from custom_components.phantom_chess import coordinator as coord_mod
from custom_components.phantom_chess.const import (
    PENDING_FRAME_MAX_AGE_SECONDS,
    SETTLE_MODE_TRIM_SECONDS,
    SETTLE_TIMEOUT_TRIM_SECONDS,
    STATUS_RESIGNED,
    UUID_GAME,
    UUID_SEND_MATRIX,
)
from custom_components.phantom_chess.matrix import build_matrix_from_fen

from .ble_mock import FakeBleakClient, drain_tasks, make_coordinator


# ─── helpers ────────────────────────────────────────────────────────────────


def _session_with_statuses(statuses: list[int], bodies: list[str] | None = None):
    """A session mock whose POST/GET return the given statuses in order."""
    cms = []
    for i, st in enumerate(statuses):
        resp = MagicMock(
            status=st,
            text=AsyncMock(return_value=(bodies[i] if bodies else "err-body")),
            json=AsyncMock(return_value={}),
        )
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        cms.append(cm)
    session = MagicMock()
    session.post = MagicMock(side_effect=cms)
    session.get = MagicMock(side_effect=cms)
    return session


def _stub_services(coord) -> None:
    coord.hass.services = MagicMock()
    coord.hass.services.async_call = AsyncMock()


def _create_calls(coord):
    return [
        c
        for c in coord.hass.services.async_call.call_args_list
        if c.args[:2] == ("persistent_notification", "create")
    ]


def _matrix_payload(piece_count: int) -> bytes:
    """A CLEAN matrix wire payload whose grid has exactly ``piece_count`` pieces."""
    board = chess.Board()
    # Remove pawns from a2, b2, c2, … until the target count is reached.
    to_remove = 32 - piece_count
    squares = [chess.A2, chess.B2, chess.C2, chess.D2, chess.E2, chess.F2]
    for sq in squares[:to_remove]:
        board.remove_piece_at(sq)
    grid = build_matrix_from_fen(board.fen())
    assert sum(1 for c in grid if c != ".") == piece_count
    bitmap = "0" * 100
    return f"CLEAN: Match.,{grid},{bitmap}".encode()


# ════════════════════════════════════════════════════════════════════════════
# Fix A1 — trim the settle window on an execute-position TIMEOUT.
#
# Falsifiability: on the old code the 600s window armed at GAME_START stays
# armed when the 0x0c BLE_MOVE_DONE never arrives, so a human frame landing
# after the drive is eaten for ~570s (the live c4-d5 case, 51s post-drive).
# ════════════════════════════════════════════════════════════════════════════


async def test_a1_execute_timeout_trims_settle_window():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._phantom_session_initialized = True  # skip the drop-to-HOME precondition
    coord._state["firmware_mode"] = "Waiting Side"  # break the Waiting-Side poll at once

    ok = await coord._phantom_execute_position(
        fen=chess.STARTING_FEN, side="W", timeout_s=0.05,
    )

    assert ok is False  # 0x0c never fired
    now = coord.hass.loop.time()
    # Trimmed to a short tail, NOT left at now+600 and NOT zeroed.
    assert coord._activation_settle_until <= now + SETTLE_TIMEOUT_TRIM_SECONDS + 0.5
    assert coord._activation_settle_until > now
    # The c4-d5 timing: a human frame ~15s later now falls OUTSIDE the window
    # (old code: inside the 600s window → eaten).
    assert coord._activation_settle_until < now + 15.0


# ════════════════════════════════════════════════════════════════════════════
# Fix A2 — trim the settle window when the firmware reaches Board Playing.
#
# Falsifiability: the g1-e2 case — firmware reports Board Playing, then a human
# frame arrives, but the 600s window (never cleared, since 0x0c was unreliable)
# suppressed it. The 2026-05-25 spurious e8-g8 (mid-activation, before any
# Board Playing transition) is the regression this must NOT reopen.
# ════════════════════════════════════════════════════════════════════════════


async def test_a2_board_playing_trims_settle_window():
    coord = make_coordinator()
    now = coord.hass.loop.time()
    coord._activation_settle_until = now + 600.0

    coord._apply_firmware_mode_state("Board Playing")

    now2 = coord.hass.loop.time()
    assert coord._activation_settle_until <= now2 + SETTLE_MODE_TRIM_SECONDS + 0.5
    assert coord._activation_settle_until > now2  # trimmed, NOT cleared to zero


async def test_a2_ble_playing_label_also_trims():
    coord = make_coordinator()
    now = coord.hass.loop.time()
    coord._activation_settle_until = now + 600.0
    coord._apply_firmware_mode_state("BLE Playing")
    assert coord._activation_settle_until <= coord.hass.loop.time() + SETTLE_MODE_TRIM_SECONDS + 0.5


async def test_a2_frame_just_after_transition_still_suppressed():
    # 0.5s after the transition the trimmed (~2s) window is still armed — the
    # e8-g8 regression guard. The frame is stashed, not applied.
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._activation_settle_until = coord.hass.loop.time() + SETTLE_MODE_TRIM_SECONDS

    coord._apply_move_frame("M 1 e2-e4", UUID_GAME, client, 0x03)
    await drain_tasks()

    assert list(coord._board.move_stack) == []  # suppressed
    assert coord._pending_settle_frame is not None  # stashed for replay


async def test_a2_frame_after_window_elapsed_applies():
    # 3s after the transition the 2s window has elapsed → the frame applies.
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._activation_settle_until = coord.hass.loop.time() - 1.0  # elapsed

    coord._apply_move_frame("M 1 e2-e4", UUID_GAME, client, 0x03)
    await drain_tasks()

    assert [m.uci() for m in coord._board.move_stack] == ["e2e4"]


# ════════════════════════════════════════════════════════════════════════════
# Fix A3 — a frame suppressed mid-settle is stashed and replayed exactly once.
# ════════════════════════════════════════════════════════════════════════════


async def test_a3_pending_frame_replays_on_move_done_exactly_once():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._activation_settle_until = coord.hass.loop.time() + 600.0

    # Frame arrives mid-settle → suppressed + stashed.
    coord._apply_move_frame("M 1 e2-e4", UUID_GAME, client, 0x03)
    await drain_tasks()
    assert list(coord._board.move_stack) == []
    assert coord._pending_settle_frame is not None

    # 0x0c arrives: window cleared, stashed frame replays through the full path.
    coord._activation_settle_until = 0.0
    coord._maybe_replay_pending_settle_frame(UUID_GAME, client, 0x0C)
    await drain_tasks()
    assert [m.uci() for m in coord._board.move_stack] == ["e2e4"]
    assert coord._pending_settle_frame is None

    # Idempotent: a second release is a no-op — still a SINGLE board push.
    coord._maybe_replay_pending_settle_frame(UUID_GAME, client, 0x0C)
    await drain_tasks()
    assert len(coord._board.move_stack) == 1


async def test_a3_stale_pending_discarded_not_applied():
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._pending_settle_frame = (
        "M 1 e2-e4",
        coord.hass.loop.time() - (PENDING_FRAME_MAX_AGE_SECONDS + 1.0),
    )
    coord._activation_settle_until = 0.0

    coord._maybe_replay_pending_settle_frame(UUID_GAME, client, 0x0C)
    await drain_tasks()

    assert list(coord._board.move_stack) == []  # too old — discarded
    assert coord._pending_settle_frame is None


async def test_a3_lazy_replay_at_top_of_next_frame():
    # 0x0c never comes, but a later human frame arrives after the window trims
    # to expiry: the stashed frame replays first, then the new frame applies.
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._activation_settle_until = coord.hass.loop.time() + 600.0

    coord._apply_move_frame("M 1 e2-e4", UUID_GAME, client, 0x03)  # stashed
    await drain_tasks()
    assert list(coord._board.move_stack) == []

    coord._activation_settle_until = coord.hass.loop.time() - 1.0  # window elapsed
    coord._apply_move_frame("M 1 e7-e5", UUID_GAME, client, 0x03)  # black reply
    await drain_tasks()

    assert [m.uci() for m in coord._board.move_stack] == ["e2e4", "e7e5"]
    assert coord._pending_settle_frame is None


async def test_a3_pending_held_while_window_still_armed():
    # A release attempt while the window is genuinely armed must NOT flush.
    client = FakeBleakClient()
    coord = make_coordinator(client=client)
    coord._activation_settle_until = coord.hass.loop.time() + 600.0
    coord._pending_settle_frame = ("M 1 e2-e4", coord.hass.loop.time())

    coord._maybe_replay_pending_settle_frame(UUID_GAME, client, 0x0C)
    await drain_tasks()

    assert list(coord._board.move_stack) == []
    assert coord._pending_settle_frame is not None  # still held


# ════════════════════════════════════════════════════════════════════════════
# Fix B — resign robustness (notification + one retry; success clears state).
# ════════════════════════════════════════════════════════════════════════════


async def test_b_resign_non200_notifies_and_retries_once():
    coord = make_coordinator()
    coord._game_id = "g1"
    _stub_services(coord)
    session = _session_with_statuses([500, 500])

    with patch.object(coord_mod, "async_get_clientsession", return_value=session), \
            patch.object(coord_mod, "_sleep", new=AsyncMock()):
        await coord.async_resign()

    assert session.post.call_count == 2  # original + ONE retry
    assert _create_calls(coord), "expected a resign-failed persistent notification"
    assert coord._game_id == "g1"  # still live — not cleared on failure


async def test_b_resign_retry_succeeds_on_second_attempt():
    coord = make_coordinator()
    coord._game_id = "g1"
    _stub_services(coord)
    session = _session_with_statuses([500, 200])

    with patch.object(coord_mod, "async_get_clientsession", return_value=session), \
            patch.object(coord_mod, "_sleep", new=AsyncMock()):
        await coord.async_resign()

    assert session.post.call_count == 2
    assert coord._game_id is None
    assert coord._state["game_status"] == STATUS_RESIGNED


async def test_b_resign_success_clears_state_even_with_dead_stream(
    mock_aiohttp_session_factory,
):
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._state["lichess_active"] = True
    coord._state["lichess_game_id"] = "g1"
    dead = coord.hass.loop.create_task(asyncio.Event().wait())  # never delivers terminal
    coord._lichess_task = dead
    _stub_services(coord)
    session = mock_aiohttp_session_factory(status=200)

    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_resign()
    await drain_tasks()

    assert coord._game_id is None
    assert coord._state["lichess_active"] is False
    assert coord._state["lichess_game_id"] is None
    assert coord._state["game_status"] == STATUS_RESIGNED
    assert dead.cancelled()  # stream task torn down, not left hanging


# ════════════════════════════════════════════════════════════════════════════
# Fix C — back_to_modes ends an active Lichess game (no zombie).
#
# Falsifiability: old back_to_modes never touched _game_id / lichess_active, so
# a mid-game tap left the server game running (tonight's zombie needing reload).
# ════════════════════════════════════════════════════════════════════════════


async def test_c_back_to_modes_tears_down_active_lichess_game(
    mock_aiohttp_session_factory,
):
    coord = make_coordinator()
    coord._game_id = "g1"
    coord._state["lichess_active"] = True
    coord._state["lichess_game_id"] = "g1"
    dead = coord.hass.loop.create_task(asyncio.Event().wait())
    coord._lichess_task = dead
    _stub_services(coord)
    coord.async_reset_position = AsyncMock()
    session = mock_aiohttp_session_factory(status=200)

    with patch.object(coord_mod, "async_get_clientsession", return_value=session):
        await coord.async_back_to_modes()
    await drain_tasks()

    assert coord._game_id is None  # FAILS on old code (zombie left running)
    assert coord._state["lichess_active"] is False
    assert coord._state["lichess_game_id"] is None
    assert dead.cancelled()  # stream task cancelled
    assert coord.setup_mode == const.DEFAULT_SETUP_MODE
    coord.async_reset_position.assert_awaited_once()  # single re-home fan-out
    session.post.assert_called_once()  # best-effort resign — single POST, no retry


# ════════════════════════════════════════════════════════════════════════════
# Fix D — graveyard shortfall notification after a re-home.
# ════════════════════════════════════════════════════════════════════════════


async def test_d_rehome_shortfall_notifies_with_count():
    client = FakeBleakClient(read_values={UUID_SEND_MATRIX: _matrix_payload(28)})
    coord = make_coordinator(client=client)
    _stub_services(coord)

    await coord._check_graveyard_shortfall()

    creates = _create_calls(coord)
    assert creates, "expected a graveyard-shortfall notification"
    msg = creates[0].args[2]["message"]
    assert "4 piece" in msg  # 32 - 28 = 4 in the tray


async def test_d_full_board_no_notification():
    client = FakeBleakClient(read_values={UUID_SEND_MATRIX: _matrix_payload(32)})
    coord = make_coordinator(client=client)
    _stub_services(coord)

    await coord._check_graveyard_shortfall()

    assert not _create_calls(coord)  # all 32 present — silent

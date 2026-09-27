"""Single-game sculpture playback (beta3).

`async_play_selected_sculpture` no longer enters the firmware's auto-looping
sculpture mode. Instead the integration drives ONE selected historic game
move-by-move via the same snapshot primitive AI-vs-AI uses (`_sculpture_loop`),
then stops. These tests bind the real `_sculpture_loop` to a lightweight stub
(no Home Assistant, no hardware) and also assert the bundled move catalog
(`sculpture_games.json`) is internally consistent and matches
`const.SCULPTURE_GAMES`.

Runs in the minimal CI env — only needs `chess`.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import types
from unittest.mock import AsyncMock, MagicMock

import chess
import chess.pgn
import pytest

import custom_components.phantom_chess.coordinator as coord_mod
from custom_components.phantom_chess.coordinator import PhantomChessCoordinator
from custom_components.phantom_chess.const import SCULPTURE_GAMES


# ── loop stub ────────────────────────────────────────────────────────────────

class _FakeLoop:
    def __init__(self) -> None:
        self._t = 0.0

    def time(self) -> float:
        self._t += 0.5
        return self._t


class _FakeHass:
    def __init__(self) -> None:
        self.loop = _FakeLoop()


def _make_stub() -> types.SimpleNamespace:
    stub = types.SimpleNamespace()
    stub.hass = _FakeHass()
    stub._board = chess.Board()
    stub._sculpture_active = True
    stub._ai_vs_ai_active = False  # sibling flag the shared reconnect helper reads
    stub._sculpture_move_delay = 0.0
    stub._our_color = chess.WHITE
    stub._local_game_active = True
    stub._ble_connected = True
    stub.selected_sculpture = "Test Game"
    stub._state = {}
    stub.async_set_updated_data = lambda *a, **k: None
    stub._record_and_analyze_local_move = lambda *a, **k: None
    stub._build_post_game_review = AsyncMock()
    stub._announce_via_tts = AsyncMock()

    def _create_task(coro, **k):
        # Close the coro so an un-awaited AsyncMock coroutine doesn't warn.
        # (Review/announce firing is asserted via their AsyncMock call_count.)
        try:
            coro.close()
        except Exception:
            pass
        return None

    stub.hass.async_create_task = _create_task
    stub._sculpture_active = True

    stub._sculpture_loop = types.MethodType(
        PhantomChessCoordinator._sculpture_loop, stub
    )
    stub._ai_vs_ai_await_reconnect = types.MethodType(
        PhantomChessCoordinator._ai_vs_ai_await_reconnect, stub
    )
    return stub


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(coord_mod.rt, "_sleep", _instant)


# Scholar's mate — 7 plies ending in checkmate.
SCHOLARS_MATE = ["e2e4", "e7e5", "f1c4", "b8c6", "d1h5", "g8f6", "h5f7"]


@pytest.mark.asyncio
async def test_full_game_plays_once_and_ends_in_checkmate():
    stub = _make_stub()
    applied: list[str] = []

    async def _apply(uci):
        stub._board.push(chess.Move.from_uci(uci))
        applied.append(uci)

    stub.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    stub._phantom_execute_position = AsyncMock(return_value=True)

    await stub._sculpture_loop(list(SCHOLARS_MATE))

    # Every ply played, exactly once, in order — one game, no loop.
    assert applied == SCHOLARS_MATE
    assert stub._board.is_checkmate()
    assert stub._sculpture_active is False
    assert stub._local_game_active is False
    assert stub._state["game_status"] == "checkmate"
    assert stub._state["last_game_result"] == "1-0 (checkmate)"
    assert stub._state["lichess_review_ready"] is True
    # Sentinel cleared so the dashboard yields to the review / picker.
    assert stub._state.get("lichess_game_id") is None
    # Post-game review was triggered once.
    assert stub._build_post_game_review.call_count == 1


@pytest.mark.asyncio
async def test_quiet_complete_game_reports_generic_result():
    """A game whose bundled line doesn't end in mate still completes + reviews."""
    stub = _make_stub()
    moves = ["e2e4", "e7e5", "g1f3", "b8c6"]  # 4 quiet plies

    async def _apply(uci):
        stub._board.push(chess.Move.from_uci(uci))

    stub.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    stub._phantom_execute_position = AsyncMock(return_value=True)

    await stub._sculpture_loop(moves)

    assert len(stub._board.move_stack) == 4
    assert stub._sculpture_active is False
    assert stub._state["game_status"] == "idle"
    assert stub._state["last_game_result"] == "Historic game complete"
    assert stub._state["lichess_review_ready"] is True
    assert stub._build_post_game_review.call_count == 1


@pytest.mark.asyncio
async def test_external_stop_halts_without_review():
    """Clearing _sculpture_active mid-game stops cleanly and builds no review."""
    stub = _make_stub()
    applied = {"n": 0}

    async def _apply(uci):
        stub._board.push(chess.Move.from_uci(uci))
        applied["n"] += 1
        if applied["n"] == 2:
            stub._sculpture_active = False  # emulate stop_local_game/back_to_modes

    stub.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    stub._phantom_execute_position = AsyncMock(return_value=True)

    await stub._sculpture_loop(list(SCHOLARS_MATE))

    assert applied["n"] == 2  # stopped after 2 plies, remaining not applied
    assert stub._sculpture_active is False
    assert stub._state["game_status"] == "idle"
    assert stub._build_post_game_review.call_count == 0  # no review on an interrupted game
    assert stub._state.get("lichess_review_ready") is not True


@pytest.mark.asyncio
async def test_illegal_move_stops_without_desync():
    """A move that isn't legal on the tracked board halts before driving it."""
    stub = _make_stub()
    applied: list[str] = []

    async def _apply(uci):
        stub._board.push(chess.Move.from_uci(uci))
        applied.append(uci)

    stub.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)
    stub._phantom_execute_position = AsyncMock(return_value=True)

    # Third entry is illegal (e2 is empty after 1.e4).
    await stub._sculpture_loop(["e2e4", "e7e5", "e2e4"])

    assert applied == ["e2e4", "e7e5"]
    assert stub._state["game_status"] == "idle"
    assert stub._build_post_game_review.call_count == 0


@pytest.mark.asyncio
async def test_transport_failure_stops_without_replaying_motion(monkeypatch):
    """An ambiguous command stops playback before any further physical write."""
    stub = _make_stub()
    stub._notify_wedge_circuit_breaker = MagicMock()
    stub._get_ai_move = AsyncMock(return_value="e2e4")
    stub.async_phantom_apply_ai_move = AsyncMock(side_effect=RuntimeError("BLE dropped"))
    stub._phantom_execute_position = AsyncMock()
    monkeypatch.setattr(coord_mod.rt, "_sleep", AsyncMock())
    await stub._sculpture_loop(["e2e4", "e7e5"])
    stub.async_phantom_apply_ai_move.assert_awaited_once_with("e2e4")
    stub._phantom_execute_position.assert_not_awaited()
    stub._notify_wedge_circuit_breaker.assert_called_once()
    assert len(stub._board.move_stack) == 0


# ── bundled data-file integrity ─────────────────────────────────────────────

def _load_catalog() -> dict:
    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components" / "phantom_chess" / "sculpture_games.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))["games"]


def test_catalog_keys_match_const_exactly():
    catalog = _load_catalog()
    assert set(catalog) == set(SCULPTURE_GAMES)
    assert len(catalog) == len(SCULPTURE_GAMES) == 18


def test_every_catalog_game_replays_legally():
    catalog = _load_catalog()
    for label, rec in catalog.items():
        assert rec["moves"], f"{label} has no moves"
        board = chess.Board()
        for uci in rec["moves"]:
            mv = chess.Move.from_uci(uci)
            assert mv in board.legal_moves, f"{label}: {uci} illegal at {board.fen()}"
            board.push(mv)


def test_catalog_records_have_player_names():
    catalog = _load_catalog()
    for label, rec in catalog.items():
        assert rec.get("white"), f"{label} missing white"
        assert rec.get("black"), f"{label} missing black"


def test_catalog_records_have_display_metadata():
    """C4: every game carries the full display-metadata set so the
    dashboard can render its card straight from the sculpture-select
    attributes (the old 55-line inline Jinja dict is deleted).

    Falsifiable: before C4 enriched sculpture_games.json, ``site`` and
    ``significance`` lived ONLY in the dashboard's inline dict, so this
    assertion fails on the pre-C4 catalog.
    """
    catalog = _load_catalog()
    for label, rec in catalog.items():
        for field in ("date", "eco", "result", "site", "significance"):
            assert rec.get(field), f"{label} missing {field}"

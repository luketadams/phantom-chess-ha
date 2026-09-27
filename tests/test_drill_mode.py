"""Endgame drills on the coordinator: start, full-strength engine, judging."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import chess
import pytest

from custom_components.phantom_chess.drills import DRILLS_BY_ID

from .ble_mock import drain_tasks, make_coordinator


def _coordinator(*, confirmed: bool = True, engine_move: str | None = None):
    c = make_coordinator()
    c._phantom_execute_position = AsyncMock(return_value=confirmed)
    c._announce_via_tts = AsyncMock()

    async def apply(uci: str) -> bool:
        c._board.push_uci(uci)
        return True

    c.async_phantom_apply_ai_move = AsyncMock(side_effect=apply)
    c._record_and_analyze_local_move = MagicMock()
    c._analysis_client = MagicMock(allow_cloud=False,
                                   best_move_for_ai_level=AsyncMock(return_value=engine_move))
    return c


def _spoken(c) -> list[str]:
    return [call.args[0] for call in c._announce_via_tts.await_args_list]


async def _turn(c) -> None:
    with patch("custom_components.phantom_chess.runtime._sleep", new=AsyncMock()):
        await c._local_ai_turn()
    await drain_tasks()


async def test_start_drill_sets_position_and_instructions() -> None:
    c = _coordinator()
    summary = await c.async_start_drill("queen_mate")
    await drain_tasks()
    drill = DRILLS_BY_ID["queen_mate"]
    assert c._phantom_execute_position.await_args.kwargs["fen"] == drill.fen
    assert c._board.fen() == drill.fen and c._our_color == chess.WHITE
    assert c._local_game_active and c._saved_game_id is None
    assert summary["status"] == "active" and c._state["drill"]["moves"] == 0
    assert _spoken(c) == [f"{drill.title}. {drill.instructions}"]


async def test_start_drill_rejects_unknown_and_unconfirmed() -> None:
    c = _coordinator(confirmed=False)
    with pytest.raises(ValueError):
        await c.async_start_drill("nope")
    with pytest.raises(TimeoutError):
        await c.async_start_drill("queen_mate")
    assert c._drill is None and not c._local_game_active


async def test_engine_defends_at_full_strength() -> None:
    c = _coordinator(engine_move="e5f5")
    await c.async_start_drill("queen_mate")
    c.ai_level = 1  # the user's usual level must not weaken the defence
    c._board.push_uci("e1e2")
    await _turn(c)
    level = c._analysis_client.best_move_for_ai_level.await_args.args[1]
    assert level == 8
    c.async_phantom_apply_ai_move.assert_awaited_once_with("e5f5")
    assert c._drill.status == "active" and c._state["drill"]["moves"] == 1


async def test_mating_move_completes_before_the_engine_thinks() -> None:
    c = _coordinator()
    await c.async_start_drill("ladder_mate")
    for uci in "h1h5 e5d4 a1a4 d4c3 h5h3 c3c2 a4b4 c2c1".split():
        c._board.push_uci(uci)
    c._board.push_uci("h3c3")  # mate
    await _turn(c)
    c._analysis_client.best_move_for_ai_level.assert_not_awaited()
    assert c._drill.status == "success" and c._drill.reason == "Checkmate."
    assert not c._local_game_active and c._state["local_game_active"] is False
    assert "Drill complete. Checkmate." in _spoken(c)


async def test_engine_promotion_fails_the_defence() -> None:
    c = _coordinator(engine_move="e7e8q")
    await c.async_start_drill("hold_the_draw")
    c._board = chess.Board("8/4P3/8/8/8/8/k7/4K3 b - - 0 1")  # a lost defence
    c._board.push_uci("a2b2")
    await _turn(c)
    assert c._drill.status == "failure" and c._drill.reason == "The pawn promoted."
    assert "Drill failed. The pawn promoted." in _spoken(c)


async def test_game_end_is_judged_by_the_drill() -> None:
    c = _coordinator()
    await c.async_start_drill("queen_mate")
    c._build_post_game_review = AsyncMock()
    c._board = chess.Board("7k/8/5K2/8/8/8/8/6Q1 w - - 0 1")
    c._board.push_uci("g1g6")  # stalemate
    c._finish_local_game()
    await drain_tasks()
    assert c._drill.status == "failure" and c._drill.reason.startswith("Stalemate")
    c._build_post_game_review.assert_not_awaited()
    assert c._state.get("last_game_result") is None


async def test_stop_and_next_start_clear_the_drill() -> None:
    c = _coordinator()
    await c.async_start_drill("queen_mate")
    c._ble_write = AsyncMock()
    c.async_checkpoint = AsyncMock()
    await c.async_stop_local_game()
    assert c._drill is None and "drill" not in c._state
    await c.async_start_drill("queen_mate")
    c._drill.status = "success"
    c._local_game_active = False
    c._assert_no_active_game()
    assert c._drill is None

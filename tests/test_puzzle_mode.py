"""Puzzle mode on the coordinator: start, judge, reply, take back, finish."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import chess
import pytest

from custom_components.phantom_chess.puzzles import Puzzle

from .ble_mock import drain_tasks, make_coordinator

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "lichess_puzzle_daily.json").read_text())
PUZZLE = Puzzle.from_lichess(FIXTURE)


def _coordinator(*, confirmed: bool = True):
    c = make_coordinator()
    c._fetch_puzzle = AsyncMock(return_value=FIXTURE)
    c._phantom_execute_position = AsyncMock(return_value=confirmed)
    c._announce_via_tts = AsyncMock()

    async def apply(uci: str) -> bool:
        c._board.push_uci(uci)
        return True

    c.async_phantom_apply_ai_move = AsyncMock(side_effect=apply)
    c._record_and_analyze_local_move = MagicMock()
    return c


def _spoken(c) -> list[str]:
    return [call.args[0] for call in c._announce_via_tts.await_args_list]


async def test_start_sets_board_to_puzzle_position() -> None:
    c = _coordinator()
    summary = await c.async_start_puzzle("daily")
    await drain_tasks()
    c._phantom_execute_position.assert_awaited_once()
    kwargs = c._phantom_execute_position.await_args.kwargs
    assert kwargs["fen"] == PUZZLE.start_fen and kwargs["side_opcode"] == "1"
    assert c._board.fen() == PUZZLE.start_fen
    assert c._our_color == chess.Board(PUZZLE.start_fen).turn
    assert c._local_game_active and not c.paused and c._saved_game_id is None
    assert c._state["puzzle"]["status"] == "active" and summary["id"] == PUZZLE.id
    assert c._state["last_move"] == PUZZLE.last_move
    assert _spoken(c) == [f"Puzzle rated {PUZZLE.rating}. White to move."]


async def test_unconfirmed_position_does_not_start() -> None:
    c = _coordinator(confirmed=False)
    with pytest.raises(TimeoutError):
        await c.async_start_puzzle("daily")
    assert c._puzzle is None and not c._local_game_active


async def test_cannot_start_over_a_running_game() -> None:
    c = _coordinator()
    c._local_game_active = True
    with pytest.raises(RuntimeError):
        await c.async_start_puzzle("daily")
    c._fetch_puzzle.assert_not_awaited()


async def test_correct_move_gets_scripted_reply() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    c._board.push_uci(PUZZLE.solution[0])  # the solver's physical move
    await c._puzzle_turn()
    c.async_phantom_apply_ai_move.assert_awaited_once_with(PUZZLE.solution[1])
    assert c._puzzle.index == 2 and c._state["puzzle"]["progress"] == 2
    assert c._board.turn == c._our_color


async def test_wrong_move_is_taken_back() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    c.async_takeback = AsyncMock()
    wrong = next(m for m in c._board.legal_moves if m.uci() != PUZZLE.solution[0])
    c._board.push(wrong)
    await c._puzzle_turn()
    await drain_tasks()
    c.async_takeback.assert_awaited_once_with(1)
    c.async_phantom_apply_ai_move.assert_not_awaited()
    assert c._puzzle.mistakes == 1 and c._state["puzzle"]["mistakes"] == 1
    assert "Not the move. Try again." in _spoken(c)


async def test_failed_takeback_pauses_with_guidance() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    c.async_takeback = AsyncMock(side_effect=RuntimeError("Board not connected"))
    c._board.push(next(m for m in c._board.legal_moves if m.uci() != PUZZLE.solution[0]))
    await c._puzzle_turn()
    assert c.paused and "by hand" in c._state["puzzle_error"]


async def test_final_move_solves_and_ends_game() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    for i in range(0, len(PUZZLE.solution) - 1, 2):
        c._board.push_uci(PUZZLE.solution[i])
        await c._puzzle_turn()
    c._board.push_uci(PUZZLE.solution[-1])
    await c._puzzle_turn()
    await drain_tasks()
    assert c._puzzle.status == "solved"
    assert not c._local_game_active and c._state["local_game_active"] is False
    assert c._state["puzzle"]["status"] == "solved"
    assert "Puzzle solved. Well done." in _spoken(c)


async def test_solved_after_mistakes_reports_tries() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    c._puzzle.mistakes = 2
    c._puzzle.index = len(PUZZLE.solution) - 1
    c._board = chess.Board(PUZZLE.start_fen)
    for uci in PUZZLE.solution[:-1]:
        c._board.push_uci(uci)
    c._board.push_uci(PUZZLE.solution[-1])
    await c._puzzle_turn()
    await drain_tasks()
    assert "Puzzle solved in 3 tries." in _spoken(c)


async def test_hint_names_the_square() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    assert await c.async_puzzle_hint() == {"square": PUZZLE.solution[0][:2]}
    await drain_tasks()
    assert c._state["puzzle_hint"] == PUZZLE.solution[0][:2]
    assert f"Look at the piece on {PUZZLE.solution[0][:2]}." in _spoken(c)


async def test_help_requires_an_active_puzzle_on_solver_turn() -> None:
    c = _coordinator()
    with pytest.raises(ValueError):
        await c.async_puzzle_hint()
    await c.async_start_puzzle("daily")
    c._board.push_uci(PUZZLE.solution[0])  # opponent to move
    with pytest.raises(RuntimeError):
        await c.async_puzzle_show_solution()


async def test_show_solution_plays_the_line() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    with patch("custom_components.phantom_chess.puzzle_mode.SOLUTION_STEP_DELAY_S", 0):
        summary = await c.async_puzzle_show_solution()
    played = [call.args[0] for call in c.async_phantom_apply_ai_move.await_args_list]
    assert played == list(PUZZLE.solution)
    assert summary["status"] == "revealed" and not c._local_game_active


async def test_show_solution_stops_when_board_fails() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    c.async_phantom_apply_ai_move = AsyncMock(return_value=False)
    with patch("custom_components.phantom_chess.puzzle_mode.SOLUTION_STEP_DELAY_S", 0):
        await c.async_puzzle_show_solution()
    assert c.async_phantom_apply_ai_move.await_count == 1
    assert "did not confirm" in c._state["puzzle_error"]


async def test_opponent_turn_routes_to_puzzle_not_engine() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    c._get_ai_move = AsyncMock(return_value="e2e4")
    c._board.push_uci(PUZZLE.solution[0])
    with patch("custom_components.phantom_chess.runtime._sleep", new=AsyncMock()):
        await c._local_ai_turn()
    c._get_ai_move.assert_not_awaited()
    c.async_phantom_apply_ai_move.assert_awaited_once_with(PUZZLE.solution[1])


async def test_game_over_during_puzzle_is_judged_not_finished() -> None:
    c = _coordinator()
    start = "6k1/5ppp/8/8/8/8/8/R3Q1K1 w - - 0 1"
    data = {"game": {"pgn": ""}, "puzzle": {"id": "m1", "rating": 900, "solution": ["a1a8"], "themes": []}}
    with patch("custom_components.phantom_chess.puzzle_mode.Puzzle.from_lichess",
               return_value=Puzzle("m1", 900, (), start, ("a1a8",))):
        c._fetch_puzzle = AsyncMock(return_value=data)
        await c.async_start_puzzle("daily")
    c._replace_local_game_task = AsyncMock()
    c._board.push_uci("e1e8")  # an alternative mate
    c._finish_local_game()
    await drain_tasks()
    c._replace_local_game_task.assert_awaited_once()
    assert c._local_game_active  # the puzzle turn, not the game finisher, ends it
    await c._puzzle_turn()
    assert c._puzzle.status == "solved" and not c._local_game_active


async def test_stop_and_new_start_clear_puzzle() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    c._ble_write = AsyncMock()
    c.async_checkpoint = AsyncMock()
    await c.async_stop_local_game()
    assert c._puzzle is None and "puzzle" not in c._state
    await c.async_start_puzzle("daily")
    c._complete_puzzle()
    c._assert_no_active_game()  # a finished puzzle is replaced by the next start
    assert c._puzzle is None


async def test_classification_speech_is_muted_during_puzzle() -> None:
    c = _coordinator()
    await c.async_start_puzzle("daily")
    c._announce_via_tts.reset_mock()
    await c._maybe_announce_classification("blunder", 400, "")
    c._announce_via_tts.assert_not_awaited()


# ── fetching ────────────────────────────────────────────────────────────


def _session(status: int = 200, payload: dict | None = None, error: Exception | None = None):
    resp = MagicMock(status=status, json=AsyncMock(return_value=payload or FIXTURE))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp, side_effect=error)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


async def _fetch(session, *args):
    c = make_coordinator()
    with patch("homeassistant.helpers.aiohttp_client.async_get_clientsession", return_value=session):
        return await c._fetch_puzzle(*args)


async def test_fetch_daily_and_random_parameters() -> None:
    pytest.importorskip("pytest_homeassistant_custom_component")
    session = _session()
    assert await _fetch(session, "daily", None, None) == FIXTURE
    assert session.get.call_args.args[0].endswith("/api/puzzle/daily")
    assert session.get.call_args.kwargs["params"] == {}
    await _fetch(session, "random", "harder", "mateIn2")
    assert session.get.call_args.args[0].endswith("/api/puzzle/next")
    assert session.get.call_args.kwargs["params"] == {"difficulty": "harder", "angle": "mateIn2"}


@pytest.mark.parametrize("status,theme,exc,text", [
    (429, None, RuntimeError, "rate-limiting"),
    (404, "noSuchTheme", ValueError, "noSuchTheme"),
    (500, None, RuntimeError, "HTTP 500"),
])
async def test_fetch_http_errors_are_readable(status, theme, exc, text) -> None:
    pytest.importorskip("pytest_homeassistant_custom_component")
    with pytest.raises(exc, match=text):
        await _fetch(_session(status), "random", None, theme)


async def test_fetch_network_failure_and_bad_arguments() -> None:
    pytest.importorskip("pytest_homeassistant_custom_component")
    import aiohttp
    with pytest.raises(RuntimeError, match="Could not reach Lichess"):
        await _fetch(_session(error=aiohttp.ClientConnectionError("down")), "daily", None, None)
    with pytest.raises(ValueError):
        await _fetch(_session(), "weekly", None, None)
    with pytest.raises(ValueError):
        await _fetch(_session(), "random", "impossible", None)


async def test_unplayable_puzzle_is_reported() -> None:
    c = _coordinator()
    c._fetch_puzzle = AsyncMock(return_value={"game": {"pgn": "e4"}, "puzzle": {}})
    with pytest.raises(RuntimeError, match="can't be played"):
        await c.async_start_puzzle("daily")
    c._phantom_execute_position.assert_not_awaited()


async def test_engine_move_has_no_cloud_fallback_when_analysis_is_local() -> None:
    c = make_coordinator()
    c._analysis_client = MagicMock(allow_cloud=False,
                                   best_move_for_ai_level=AsyncMock(return_value=None))
    with patch("custom_components.phantom_chess.runtime.async_get_clientsession") as session:
        assert await c._get_ai_move(chess.Board()) is None
    session.assert_not_called()

"""Endgame drill catalogue and judging (pure logic; runs without Home Assistant)."""
from __future__ import annotations

import chess
import pytest

from custom_components.phantom_chess.drills import DRILLS, DRILLS_BY_ID, Drill, catalogue, judge, solver_moves

# Full-strength lines recorded with Stockfish 18 (both sides engine-played).
LADDER_LINE = "h1h5 e5d4 a1a4 d4c3 h5h3 c3c2 a4b4 c2c1 h3c3".split()
QUEEN_LINE = "e1e2 e5f5 d1d5 f5g6 e2f3 g6h7 d5g5 h7h8 f3e4 h8h7 e4f5 h7h8 f5g6 h8g8 g5d8".split()


def _play(drill: Drill, ucis: list[str]) -> list:
    board = chess.Board(drill.fen)
    verdicts = []
    for uci in ucis:
        board.push_uci(uci)
        verdicts.append(judge(drill, board))
    return verdicts


def test_catalogue_is_consistent() -> None:
    assert len(DRILLS) == len(DRILLS_BY_ID) == len(catalogue())
    for drill in DRILLS:
        board = chess.Board(drill.fen)
        assert board.is_valid() and not board.is_game_over(), drill.id
        assert drill.move_limit > 0 and drill.instructions
        assert catalogue()[DRILLS.index(drill)]["solver"] in ("white", "black")
    assert DRILLS_BY_ID["hold_the_draw"].solver == chess.BLACK


@pytest.mark.parametrize("drill_id,line", [("ladder_mate", LADDER_LINE), ("queen_mate", QUEEN_LINE)])
def test_mating_line_succeeds_on_the_mating_move(drill_id: str, line: list[str]) -> None:
    verdicts = _play(DRILLS_BY_ID[drill_id], line)
    assert verdicts[-1] == "success"
    assert all(v is None for v in verdicts[:-1])


def test_stalemate_fails_a_mate_drill() -> None:
    drill = Drill("t", "t", "checkmate", "7k/8/5K2/8/8/8/8/6Q1 w - - 0 1", 5, "beginner", "x")
    assert _play(drill, ["g1g6"]) == ["failure"]


def test_move_limit_fails_a_mate_drill_after_the_reply() -> None:
    drill = Drill("t", "t", "checkmate", DRILLS_BY_ID["queen_mate"].fen, 1, "beginner", "x")
    assert _play(drill, ["e1e2", "e5f5"]) == [None, "failure"]


def test_promotion_succeeds() -> None:
    drill = Drill("t", "t", "promote", "8/4P3/8/8/8/8/k7/4K3 w - - 0 1", 5, "beginner", "x")
    assert _play(drill, ["e7e8q"]) == ["success"]
    assert _play(drill, ["e7e8r"]) == ["success"]  # a winning underpromotion counts
    # A knight cannot force mate: promoting to one draws, which fails the drill.
    assert _play(drill, ["e7e8n"]) == ["failure"]


def test_hold_the_draw_outcomes() -> None:
    drill = DRILLS_BY_ID["hold_the_draw"]
    # Losing the key squares lets the pawn through.
    defender_blunder = Drill("t", "t", "draw", "8/4P3/8/8/8/8/k7/4K3 b - - 0 1", 30, "x", "x")
    assert _play(defender_blunder, ["a2b2", "e7e8q"]) == [None, "failure"]
    # Taking the pawn leaves bare kings: drawn.
    capture = Drill("t", "t", "draw", "8/8/8/8/8/8/3kP3/7K b - - 0 1", 30, "x", "x")
    assert _play(capture, ["d2e2"]) == ["success"]
    # Surviving the limit succeeds once the opponent has replied.
    short = Drill("t", "t", "draw", drill.fen, 1, "x", "x")
    assert _play(short, ["e8e7", "e4d5"]) == [None, "success"]


def test_solver_move_count() -> None:
    drill = DRILLS_BY_ID["ladder_mate"]
    board = chess.Board(drill.fen)
    assert solver_moves(drill, board) == 0
    board.push_uci("h1h5")
    assert solver_moves(drill, board) == 1
    board.push_uci("e5d4")
    assert solver_moves(drill, board) == 1

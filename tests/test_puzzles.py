"""Puzzle parsing and judging (pure logic; runs without Home Assistant)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import chess
import pytest

from custom_components.phantom_chess.puzzles import Puzzle, PuzzleError, PuzzleSession

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"lichess_puzzle_{name}.json").read_text())


@pytest.mark.parametrize("name", ["daily", "next"])
def test_start_position_is_end_of_pgn(name: str) -> None:
    data = _load(name)
    puzzle = Puzzle.from_lichess(data)
    board = chess.Board(puzzle.start_fen)
    # The PGN has initialPly + 1 plies and the solver is to move.
    assert len(data["game"]["pgn"].split()) == data["puzzle"]["initialPly"] + 1
    assert chess.Move.from_uci(puzzle.solution[0]) in board.legal_moves
    assert puzzle.solver_white == (board.turn == chess.WHITE)
    if data["puzzle"].get("fen"):
        assert board.board_fen() == data["puzzle"]["fen"].split()[0]
    if data["puzzle"].get("lastMove"):
        assert puzzle.last_move == data["puzzle"]["lastMove"]


def test_full_line_solves() -> None:
    puzzle = Puzzle.from_lichess(_load("daily"))
    session = PuzzleSession(puzzle)
    board = chess.Board(puzzle.start_fen)
    verdicts = []
    while session.status == "active":
        move = chess.Move.from_uci(session.expected)
        verdicts.append(session.judge(board, move))
        board.push(move)
        if session.status == "active":
            board.push_uci(session.reply())
    assert verdicts[-1] == "solved"
    assert set(verdicts[:-1]) <= {"correct"}
    assert session.moves == list(puzzle.solution)
    assert session.summary()["progress"] == len(puzzle.solution)


def test_wrong_move_counts_and_keeps_position() -> None:
    puzzle = Puzzle.from_lichess(_load("next"))
    session = PuzzleSession(puzzle)
    board = chess.Board(puzzle.start_fen)
    wrong = next(m for m in board.legal_moves if m.uci() != session.expected and not _mates(board, m))
    assert session.judge(board, wrong) == "wrong"
    assert session.mistakes == 1 and session.index == 0 and session.status == "active"
    assert session.judge(board, chess.Move.from_uci(session.expected)) == "correct"


def test_any_checkmate_solves() -> None:
    # Back rank: the stored line is Ra8#, but Qe8# mates too.
    start = "6k1/5ppp/8/8/8/8/8/R3Q1K1 w - - 0 1"
    board = chess.Board(start)
    assert _mates(board, chess.Move.from_uci("a1a8")) and _mates(board, chess.Move.from_uci("e1e8"))
    puzzle = Puzzle("t1", 1500, ("mateIn1",), start, ("a1a8",))
    session = PuzzleSession(puzzle)
    assert session.judge(board, chess.Move.from_uci("e1e8")) == "solved"
    assert session.status == "solved"


def test_hint_reveal_and_summary() -> None:
    puzzle = Puzzle.from_lichess(_load("daily"))
    session = PuzzleSession(puzzle)
    assert session.hint() == puzzle.solution[0][:2]
    assert session.reveal() == puzzle.solution[0]
    assert session.status == "revealed" and session.expected is None and session.hint() is None
    summary = session.summary()
    assert summary["hints"] == 1 and summary["url"].endswith(puzzle.id)
    with pytest.raises(PuzzleError):
        session.reply()
    with pytest.raises(PuzzleError):
        session.judge(chess.Board(puzzle.start_fen), chess.Move.from_uci(puzzle.solution[0]))


@pytest.mark.parametrize("mutate", [
    lambda d: d["puzzle"].pop("solution"),
    lambda d: d["puzzle"].__setitem__("solution", []),
    lambda d: d["puzzle"].__setitem__("solution", ["a1a1"]),
    lambda d: d["puzzle"].__setitem__("solution", d["puzzle"]["solution"][:2]),
    lambda d: d["game"].__setitem__("pgn", "e4 e4"),
    lambda d: d["puzzle"].__setitem__("rating", "high"),
    lambda d: d["puzzle"].__setitem__("solution", ["zz"]),
])
def test_malformed_responses_are_rejected(mutate) -> None:
    data = copy.deepcopy(_load("daily"))
    mutate(data)
    with pytest.raises(PuzzleError):
        Puzzle.from_lichess(data)


def _mates(board: chess.Board, move: chess.Move) -> bool:
    after = board.copy()
    after.push(move)
    return after.is_checkmate()

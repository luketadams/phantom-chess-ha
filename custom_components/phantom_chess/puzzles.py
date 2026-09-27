"""Lichess puzzles played on the physical board.

Pure logic, no Home Assistant imports: parsing the Lichess puzzle API
response and judging the solver's moves. The coordinator fetches the
puzzle, drives the board to the start position and plays the scripted
replies.

Lichess API contract (``GET /api/puzzle/daily`` and ``/api/puzzle/next``):
``game.pgn`` ends at the puzzle's start position (``initialPly + 1``
plies); the side to move there is the solver; ``puzzle.solution`` is the
UCI line alternating solver, opponent, solver … and always ends on a solver
move. ``puzzle.fen`` is present on some responses only, so the PGN replay is
authoritative. Like Lichess, any move that gives checkmate solves the
puzzle, even if it differs from the stored line.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import chess

LICHESS_PUZZLE_URL = "https://lichess.org/training/{id}"
DIFFICULTIES = ("easiest", "easier", "normal", "harder", "hardest")
Verdict = Literal["correct", "solved", "wrong"]


class PuzzleError(ValueError):
    """The puzzle response is malformed or its line is not legal."""


@dataclass(frozen=True)
class Puzzle:
    """A validated puzzle: start position plus a legal solution line."""

    id: str
    rating: int
    themes: tuple[str, ...]
    start_fen: str
    solution: tuple[str, ...]
    last_move: str | None = None

    @property
    def solver_white(self) -> bool:
        return chess.Board(self.start_fen).turn == chess.WHITE

    @property
    def url(self) -> str:
        return LICHESS_PUZZLE_URL.format(id=self.id)

    @classmethod
    def from_lichess(cls, data: dict[str, Any]) -> Puzzle:
        try:
            puzzle, game = data["puzzle"], data["game"]
            pid, rating = str(puzzle["id"]), int(puzzle["rating"])
            solution = tuple(str(u) for u in puzzle["solution"])
            themes = tuple(str(t) for t in puzzle.get("themes", ()))
            sans = str(game["pgn"]).split()
        except (KeyError, TypeError, ValueError) as err:
            raise PuzzleError(f"Unexpected puzzle response: {err}") from err
        if not solution:
            raise PuzzleError("The puzzle has no solution")
        board = chess.Board()
        last_move: str | None = None
        try:
            for san in sans:
                move = board.parse_san(san)
                board.push(move)
                last_move = move.uci()
        except ValueError as err:
            raise PuzzleError(f"The puzzle's game record is not legal: {err}") from err
        _validate_line(board, solution)
        return cls(pid, rating, themes, board.fen(), solution, last_move)


def _validate_line(start: chess.Board, solution: tuple[str, ...]) -> None:
    if len(solution) % 2 == 0:
        raise PuzzleError("The solution must end on the solver's move")
    board = start.copy()
    for uci in solution:
        try:
            move = chess.Move.from_uci(uci)
        except ValueError as err:
            raise PuzzleError(f"Invalid solution move {uci!r}") from err
        if move not in board.legal_moves:
            raise PuzzleError(f"Solution move {uci} is illegal in {board.fen()}")
        board.push(move)


@dataclass
class PuzzleSession:
    """Progress through one puzzle.

    ``index`` points at the next solver move in ``puzzle.solution``. The
    session never touches the board; the caller reports each solver move
    with :meth:`judge` and plays :meth:`reply` when it is the opponent's turn.
    """

    puzzle: Puzzle
    index: int = 0
    mistakes: int = 0
    hints: int = 0
    status: Literal["active", "solved", "revealed"] = "active"
    moves: list[str] = field(default_factory=list)

    @property
    def expected(self) -> str | None:
        if self.status != "active" or self.index >= len(self.puzzle.solution):
            return None
        return self.puzzle.solution[self.index]

    def judge(self, board_before: chess.Board, move: chess.Move) -> Verdict:
        """Judge a solver move played from ``board_before``.

        A move is correct if it matches the line; any checkmate also solves
        the puzzle. A correct final move solves it. Wrong moves count as
        mistakes; the caller takes them back.
        """
        if self.status != "active":
            raise PuzzleError("The puzzle is already finished")
        after = board_before.copy()
        after.push(move)
        if after.is_checkmate() or move.uci() == self.expected:
            self.moves.append(move.uci())
            self.index += 1
            if after.is_checkmate() or self.index >= len(self.puzzle.solution):
                self.status = "solved"
                return "solved"
            return "correct"
        self.mistakes += 1
        return "wrong"

    def reply(self) -> str:
        """The opponent's scripted answer; advances past it."""
        if self.status != "active" or self.index >= len(self.puzzle.solution):
            raise PuzzleError("No opponent reply is due")
        uci = self.puzzle.solution[self.index]
        self.moves.append(uci)
        self.index += 1
        return uci

    def hint(self) -> str | None:
        """The square of the piece to move next, e.g. ``"d6"``."""
        expected = self.expected
        if expected is None:
            return None
        self.hints += 1
        return expected[:2]

    def reveal(self) -> str | None:
        """Give up on this move: return it and count it as revealed."""
        expected = self.expected
        if expected is not None:
            self.status = "revealed"
        return expected

    def summary(self) -> dict[str, Any]:
        p = self.puzzle
        return {
            "id": p.id, "rating": p.rating, "themes": list(p.themes), "url": p.url,
            "solver": "white" if p.solver_white else "black", "status": self.status,
            "progress": self.index, "length": len(p.solution),
            "mistakes": self.mistakes, "hints": self.hints, "last_move": p.last_move,
        }

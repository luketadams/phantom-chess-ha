"""Endgame drills: a position, a goal, and full-strength resistance.

Pure logic, no Home Assistant imports. Each drill starts with the solver to
move; the local engine plays the other side at maximum strength. A drill
succeeds or fails by the rules below, judged after every move.

Positions were checked with Stockfish 18 (depth 25–30, September 2026):
ladder mate #8, queen mate #7, rook mate #18, the pawn ending wins by
promotion, and the defensive pawn ending is a draw (0.00) with the defender
to move. Move limits leave room above the best line.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import chess

Goal = Literal["checkmate", "promote", "draw"]
Result = Literal["success", "failure"]


@dataclass(frozen=True)
class Drill:
    id: str
    title: str
    goal: Goal
    fen: str
    move_limit: int
    level: str  # beginner / intermediate
    instructions: str

    @property
    def solver(self) -> chess.Color:
        return chess.Board(self.fen).turn

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "goal": self.goal, "level": self.level,
            "move_limit": self.move_limit, "instructions": self.instructions,
            "solver": "white" if self.solver == chess.WHITE else "black",
        }


DRILLS: tuple[Drill, ...] = (
    Drill("ladder_mate", "Two-rook ladder", "checkmate", "8/8/8/4k3/8/8/8/R3K2R w - - 0 1", 15,
          "beginner", "Checkmate the lone king with your two rooks, driving it to the edge one rank at a time."),
    Drill("queen_mate", "Queen and king mate", "checkmate", "8/8/8/4k3/8/8/8/3QK3 w - - 0 1", 15,
          "beginner", "Checkmate with queen and king. Box the king in, then bring your king up. Avoid stalemate."),
    Drill("rook_mate", "Rook and king mate", "checkmate", "8/8/8/4k3/8/8/8/R3K3 w - - 0 1", 35,
          "intermediate", "Checkmate with rook and king. Use your king to take the opposition and the rook to cut off ranks."),
    Drill("pawn_promotion", "King and pawn: promote", "promote", "4k3/8/4K3/4P3/8/8/8/8 w - - 0 1", 15,
          "intermediate", "Promote the pawn against the defending king. Keep your king in front of the pawn."),
    Drill("hold_the_draw", "King and pawn: hold the draw", "draw", "4k3/8/8/4P3/4K3/8/8/8 b - - 0 1", 30,
          "intermediate", "Defend with Black. Stop the pawn from promoting for 30 moves, or reach a draw."),
)
DRILLS_BY_ID = {d.id: d for d in DRILLS}


def catalogue() -> list[dict[str, Any]]:
    return [d.summary() for d in DRILLS]


def solver_moves(drill: Drill, board: chess.Board) -> int:
    """Moves the solver has made (the solver always moves first)."""
    return (len(board.move_stack) + 1) // 2


def judge(drill: Drill, board: chess.Board) -> Result | None:
    """Decide the drill from ``board`` (played from ``drill.fen``); None = continue."""
    solver = drill.solver
    outcome = board.outcome(claim_draw=True)
    last = board.move_stack[-1] if board.move_stack else None
    last_mover = (not board.turn) if last is not None else None
    moves = solver_moves(drill, board)
    if drill.goal == "draw":
        if outcome is not None:
            return "success" if outcome.winner is None else ("success" if outcome.winner == solver else "failure")
        if last is not None and last.promotion and last_mover != solver:
            return "failure"
        # Survived the limit: the opponent has just replied to the last move.
        return "success" if moves >= drill.move_limit and board.turn == solver else None
    if outcome is not None:
        return "success" if drill.goal == "checkmate" and outcome.winner == solver else "failure"
    if drill.goal == "promote" and last is not None and last.promotion and last_mover == solver:
        return "success"
    if moves >= drill.move_limit and board.turn == solver:
        return "failure"
    return None

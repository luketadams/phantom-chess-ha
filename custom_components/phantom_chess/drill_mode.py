"""Endgame drill mode on the physical board.

A drill runs as a local game from the drill's position with the engine at
full strength. The drill is judged (drills.judge) after the solver's move,
after the engine's reply, and when the game ends; the first decisive result
ends it. Like puzzles, attempts are not saved to the game library.

No Home Assistant imports at module level (minimal test environment).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import chess

from .drills import DRILLS_BY_ID, Drill, judge, solver_moves

if TYPE_CHECKING:
    import asyncio

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DRILL_ENGINE_LEVEL = 8  # full strength: the defence (or attack) is the point


@dataclass
class DrillSession:
    drill: Drill
    status: Literal["active", "success", "failure"] = "active"
    reason: str | None = None

    def summary(self, board: chess.Board) -> dict[str, Any]:
        return {**self.drill.summary(), "status": self.status, "reason": self.reason,
                "moves": solver_moves(self.drill, board)}


def _reason(drill: Drill, board: chess.Board, result: str) -> str:
    if result == "success":
        if board.is_checkmate():
            return "Checkmate."
        if drill.goal == "promote":
            return "The pawn promoted."
        if board.is_game_over(claim_draw=True):
            return "Drawn."
        return f"You held for {drill.move_limit} moves."
    if board.is_stalemate():
        return "Stalemate: the king had no legal move but was not in check."
    if board.is_checkmate():
        return "You were checkmated."
    if board.is_insufficient_material():
        return "Not enough material left to win."
    if board.is_game_over(claim_draw=True):
        return "The game was drawn."
    last = board.move_stack[-1] if board.move_stack else None
    if drill.goal == "draw" and last is not None and last.promotion:
        return "The pawn promoted."
    return f"The {drill.move_limit}-move limit was reached."


class DrillModeMixin:
    """Mixed into PhantomChessCoordinator; relies on its local-game machinery."""

    hass: HomeAssistant
    _drill: DrillSession | None
    _state: dict[str, Any]
    _board: chess.Board
    _analysis_board: chess.Board
    _our_color: chess.Color | None
    _saved_game_id: str | None
    _game_id: str | None
    _last_target_fen: str | None
    _play_revision: int
    _processed_moves: int
    _local_game_active: bool
    _phantom_session_initialized: bool
    paused: bool
    player_color: str

    if TYPE_CHECKING:
        # Declared on PhantomChessCoordinator (coordinator.py and the session
        # modules). Stubs only, so the calls below type-check without ignores
        # and without shadowing the real bound methods at runtime.
        _local_start_lock: asyncio.Lock

        def async_set_updated_data(self, data: dict[str, Any]) -> None: ...
        def _assert_no_active_game(self) -> None: ...
        async def _phantom_execute_position(
            self, fen: str, side: str = "B", timeout_s: float = 30.0,
            side_opcode: str = "2", select_chess_mode: bool = False,
        ) -> bool: ...
        def _build_phantom_matrix_from_fen(self, fen: str) -> str: ...
        async def _announce_via_tts(self, message: str) -> None: ...

    def _drill_active(self) -> bool:
        drill = getattr(self, "_drill", None)
        return drill is not None and drill.status == "active"

    async def async_start_drill(self, drill_id: str) -> dict[str, Any]:
        drill = DRILLS_BY_ID.get(drill_id)
        if drill is None:
            raise ValueError(f"Unknown drill {drill_id!r}")
        async with self._local_start_lock:
            self._assert_no_active_game()
            board = chess.Board(drill.fen)
            color = board.turn
            self._phantom_session_initialized = False
            confirmed = await self._phantom_execute_position(
                fen=drill.fen, side="W" if color == chess.WHITE else "B",
                timeout_s=60.0, side_opcode="1", select_chess_mode=True,
            )
            if not confirmed:
                raise TimeoutError("The board did not confirm the drill position. Check the pieces and try again.")
            self._drill = DrillSession(drill)
            self._board = board
            self._analysis_board = board.copy()
            self._our_color = color
            self.player_color = "white" if color == chess.WHITE else "black"
            self._saved_game_id = None
            self._game_id = None
            self._processed_moves = 0
            self._local_game_active = True
            self.paused = False
            self._play_revision += 1
            grid = self._build_phantom_matrix_from_fen(drill.fen)
            self._state.update({
                "local_game_active": True, "lichess_active": False, "lichess_game_id": "local",
                "game_status": "playing", "lichess_review_ready": False,
                "live_fen": board.board_fen(), "last_move": None, "move_history_moves": [],
                "eval_cp": None, "eval_mate": None, "best_move_san": None, "threat_san": None,
                "last_game_result": None, "lichess_white_name": "White", "lichess_black_name": "Black",
                "piece_grid": grid, "piece_count": sum(c != "." for c in grid),
            })
            self._last_target_fen = board.board_fen()
            self._publish_drill()
            self._drill_speak(f"{drill.title}. {drill.instructions}")
            return self._drill.summary(board)

    def _check_drill(self) -> bool:
        """Judge the current position; end the drill if decided. True if ended."""
        session = getattr(self, "_drill", None)
        if session is None or session.status != "active":
            return False
        result = judge(session.drill, self._board)
        if result is None:
            self._publish_drill()
            return False
        session.status = result
        session.reason = _reason(session.drill, self._board, result)
        self._local_game_active = False
        self._state.update({"local_game_active": False, "lichess_game_id": None, "game_status": "idle"})
        self._publish_drill()
        self._drill_speak(("Drill complete. " if result == "success" else "Drill failed. ") + session.reason)
        return True

    def _clear_drill(self) -> None:
        self._drill = None
        self._state.pop("drill", None)

    def _publish_drill(self) -> None:
        if self._drill is not None:
            self._state["drill"] = self._drill.summary(self._board)
        self.async_set_updated_data(dict(self._state))

    def _drill_speak(self, message: str) -> None:
        self.hass.async_create_task(
            self._announce_via_tts(message), name="phantom_chess_drill_speech",
        )

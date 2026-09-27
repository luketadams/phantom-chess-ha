"""Puzzle mode: Lichess puzzles on the physical board.

A puzzle runs as a local game with a scripted opponent. After each solver
move the opponent's turn judges it (see :class:`puzzles.PuzzleSession`):

- correct → the board plays the stored reply;
- wrong → the move is taken back physically and the solver tries again;
- solved (end of line, or any checkmate) → the puzzle ends.

All judging happens on the opponent's turn so the hardware-sensitive
Bluetooth move path stays unchanged: a solver move is recorded exactly like
any local move, and a wrong one is undone with the existing takeback.

Kept free of Home Assistant imports at module level so the minimal test
environment can load it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

import chess

from .puzzles import DIFFICULTIES, Puzzle, PuzzleError, PuzzleSession

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DAILY_URL = "https://lichess.org/api/puzzle/daily"
NEXT_URL = "https://lichess.org/api/puzzle/next"
FETCH_TIMEOUT_S = 15
SOLUTION_STEP_DELAY_S = 1.5
_COLOR_NAME = {chess.WHITE: "White", chess.BLACK: "Black"}


class PuzzleModeMixin:
    """Mixed into PhantomChessCoordinator; relies on its local-game machinery."""

    hass: HomeAssistant
    _puzzle: PuzzleSession | None
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
        async def async_takeback(self, count: int = 1) -> None: ...
        async def async_phantom_apply_ai_move(self, uci: str) -> bool: ...
        def _record_and_analyze_local_move(
            self, move: chess.Move, mover_is_white: bool
        ) -> None: ...

    # ── fetching ────────────────────────────────────────────────────────

    async def _fetch_puzzle(self, source: str, difficulty: str | None, theme: str | None) -> dict[str, Any]:
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        params: dict[str, str] = {}
        if source == "daily":
            url = DAILY_URL
        elif source == "random":
            if difficulty:
                if difficulty not in DIFFICULTIES:
                    raise ValueError(f"Unknown difficulty {difficulty!r}")
                params["difficulty"] = difficulty
            if theme:
                params["angle"] = theme
            url = NEXT_URL
        else:
            raise ValueError(f"Unknown puzzle source {source!r}")
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url, params=params, headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S),
            ) as resp:
                if resp.status == 429:
                    raise RuntimeError("Lichess is rate-limiting puzzle requests. Try again in a minute.")
                if resp.status == 404 and theme:
                    raise ValueError(f"Lichess has no puzzles for the theme {theme!r}")
                if resp.status != 200:
                    raise RuntimeError(f"Lichess did not return a puzzle (HTTP {resp.status}).")
                return cast(dict[str, Any], await resp.json())
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise RuntimeError(f"Could not reach Lichess for a puzzle: {err}") from err

    # ── starting ────────────────────────────────────────────────────────

    async def async_start_puzzle(
        self, source: str = "daily", difficulty: str | None = None, theme: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a puzzle, set the board to its start position and begin."""
        async with self._local_start_lock:
            self._assert_no_active_game()
            try:
                puzzle = Puzzle.from_lichess(await self._fetch_puzzle(source, difficulty, theme))
            except PuzzleError as err:
                raise RuntimeError(f"Lichess returned a puzzle that can't be played: {err}") from err
            board = chess.Board(puzzle.start_fen)
            color = board.turn  # the solver is always to move
            self._phantom_session_initialized = False
            confirmed = await self._phantom_execute_position(
                fen=puzzle.start_fen, side="W" if color == chess.WHITE else "B",
                timeout_s=60.0, side_opcode="1", select_chess_mode=True,
            )
            if not confirmed:
                raise TimeoutError("The board did not confirm the puzzle position. Check the pieces and try again.")
            self._puzzle = PuzzleSession(puzzle)
            self._board = board
            self._analysis_board = board.copy()
            self._our_color = color
            self.player_color = "white" if color == chess.WHITE else "black"
            self._saved_game_id = None  # attempts are not saved to the library
            self._game_id = None
            self._processed_moves = 0
            self._local_game_active = True
            self.paused = False
            self._play_revision += 1
            grid = self._build_phantom_matrix_from_fen(puzzle.start_fen)
            self._state.update({
                "local_game_active": True, "lichess_active": False, "lichess_game_id": "local",
                "game_status": "playing", "lichess_review_ready": False,
                "live_fen": board.board_fen(), "last_move": puzzle.last_move,
                "move_history_moves": [], "eval_cp": None, "eval_mate": None,
                "best_move_san": None, "threat_san": None, "last_game_result": None,
                "lichess_white_name": "White", "lichess_black_name": "Black",
                "piece_grid": grid, "piece_count": sum(c != "." for c in grid),
                "puzzle_hint": None, "puzzle_error": None,
            })
            self._last_target_fen = board.board_fen()
            self._publish_puzzle()
            self._speak(f"Puzzle rated {puzzle.rating}. {_COLOR_NAME[color]} to move.")
            return self._puzzle.summary()

    # ── the opponent's turn ─────────────────────────────────────────────

    async def _puzzle_turn(self) -> None:
        """Judge the solver's last move and answer it."""
        session = self._puzzle
        board = self._board
        if session is None or session.status != "active" or not board.move_stack:
            return
        if board.turn == self._our_color:
            return  # nothing new to judge (e.g. right after a takeback)
        last = board.peek()
        before = board.copy()
        before.pop()
        verdict = session.judge(before, last)
        if verdict == "solved":
            self._complete_puzzle()
            return
        if verdict == "wrong":
            self._publish_puzzle()
            self._speak("Not the move. Try again.")
            try:
                await self.async_takeback(1)
            except Exception as err:  # noqa: BLE001 — surfaced on the dashboard
                _LOGGER.warning("Puzzle takeback failed: %s", err)
                self._state["puzzle_error"] = (
                    "The wrong move could not be taken back. Put the piece back by hand, then continue."
                )
                self.paused = True
                self._state["game_status"] = "paused"
                self._publish_puzzle()
            return
        await self._play_puzzle_move(session.reply())
        self._state["puzzle_hint"] = None
        self._publish_puzzle()

    async def _play_puzzle_move(self, uci: str) -> bool:
        mover_is_white = self._board.turn == chess.WHITE
        try:
            delivered = await self.async_phantom_apply_ai_move(uci)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Puzzle move %s failed: %s", uci, err)
            delivered = False
        if not delivered:
            self._local_game_active = False
            self.paused = True
            self._state.update({
                "local_game_active": False, "game_status": "paused",
                "puzzle_error": f"The board did not confirm the move {uci}. The puzzle has stopped.",
            })
            self._publish_puzzle()
            return False
        self._record_and_analyze_local_move(
            chess.Move.from_uci(uci), mover_is_white,
        )
        return True

    # ── help ────────────────────────────────────────────────────────────

    def _active_puzzle(self) -> PuzzleSession:
        session = self._puzzle
        if session is None or session.status != "active" or not self._local_game_active:
            raise ValueError("There is no puzzle in progress")
        if self._board.turn != self._our_color:
            raise RuntimeError("Wait for the board to finish its move")
        return session

    async def async_puzzle_hint(self) -> dict[str, Any]:
        """Name the square of the piece to move next."""
        square = self._active_puzzle().hint()
        self._state["puzzle_hint"] = square
        self._publish_puzzle()
        if square:
            self._speak(f"Look at the piece on {square}.")
        return {"square": square}

    async def async_puzzle_show_solution(self) -> dict[str, Any]:
        """Play the rest of the line on the board, then end the puzzle."""
        session = self._active_puzzle()
        remaining = list(session.puzzle.solution[session.index:])
        session.reveal()
        self._publish_puzzle()
        self._speak("Here is the solution.")
        for step, uci in enumerate(remaining):
            if step:
                await asyncio.sleep(SOLUTION_STEP_DELAY_S)
            if not self._local_game_active or not await self._play_puzzle_move(uci):
                break
        self._complete_puzzle()
        return session.summary()

    # ── finishing ───────────────────────────────────────────────────────

    def _complete_puzzle(self) -> None:
        session = self._puzzle
        if session is None:
            return
        self._local_game_active = False
        self._state.update({
            "local_game_active": False, "lichess_game_id": None,
            "game_status": "idle", "puzzle_hint": None,
        })
        self._publish_puzzle()
        if session.status == "solved":
            if session.mistakes == 0 and session.hints == 0:
                self._speak("Puzzle solved. Well done.")
            else:
                tries = session.mistakes + 1
                self._speak(f"Puzzle solved in {tries} tries.")

    def _clear_puzzle(self) -> None:
        self._puzzle = None
        self._state.pop("puzzle", None)
        self._state["puzzle_hint"] = None
        self._state["puzzle_error"] = None

    def _publish_puzzle(self) -> None:
        if self._puzzle is not None:
            self._state["puzzle"] = self._puzzle.summary()
        self.async_set_updated_data(dict(self._state))

    def _speak(self, message: str) -> None:
        self.hass.async_create_task(
            self._announce_via_tts(message), name="phantom_chess_puzzle_speech",
        )

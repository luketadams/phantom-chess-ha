"""Local-session persistence and explicit physical recovery, separate from BLE parsing."""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

import chess

from .issues import sync_engine_issue
from .game_library import GameLibrary, SavedGame

_LOGGER = logging.getLogger(__name__)


class LocalSessionMixin:
    """Coordinator-facing journal adapter. Recovery never runs on startup."""

    # The coordinator supplies runtime state and physical/analysis operations.
    hass: Any
    _state: dict[str, Any]
    _library: GameLibrary | None
    _saved_game_id: str | None
    _saved_revision: int
    _journal_tasks: set[asyncio.Task]
    _board: chess.Board
    _our_color: chess.Color | None
    ai_level: int
    paused: bool
    _local_game_active: bool
    _local_start_lock: asyncio.Lock
    _physical_operation_lock: asyncio.Lock
    _analysis_board: chess.Board
    _processed_moves: int
    _game_id: str | None
    _phantom_session_initialized: bool
    _last_target_fen: str | None
    player_color: str

    def _publish_engine_state(self, value: dict) -> None:
        self._state["engine_health"] = value
        sync_engine_issue(self.hass, value)  # type: ignore[attr-defined]
        self.async_set_updated_data(dict(self._state))  # type: ignore[attr-defined]

    async def async_check_engine(self) -> dict:
        if (self._local_game_active or self._game_id or getattr(self, "_two_player_active", False)
                or getattr(self, "_ai_vs_ai_active", False) or getattr(self, "_sculpture_active", False)):
            raise RuntimeError("End the current game before checking the engine")
        client = getattr(self, "_analysis_client", None)
        engine = getattr(client, "_stockfish", None)
        if engine is None:
            raise RuntimeError("Local analysis is unavailable")
        try:
            async with asyncio.timeout(150):
                async with engine._eval_lock:
                    engine._available = True  # User-requested recovery after an earlier failure.
                    ready = await engine.ensure_engine()
                    if ready is not None:
                        try:
                            await ready.ping()
                        except Exception:
                            await engine.shutdown()
                            engine._available = True
                            ready = await engine.ensure_engine()
                            if ready is not None:
                                await ready.ping()
            value = {**engine.engine_state, "version": ready.id.get("name") if ready else None}
        except TimeoutError:
            value = {"status": "error", "error": "Engine check timed out. Try again when the connection is stable."}
        except Exception as err:
            value = {"status": "error", "error": f"Engine check failed: {err}"}
        if value.get("status") == "ready":
            self._state["engine_error"] = None
        self._publish_engine_state(value)
        return value

    def _publish_review_state(self, value: dict) -> None:
        self._state["game_reviews"] = value
        self.async_set_updated_data(dict(self._state))  # type: ignore[attr-defined]

    def _refresh_library_state(self) -> None:
        library = getattr(self, "_library", None)
        if library is None:
            return
        self._state["saved_games"] = library.list()
        self._state["recovery_game_id"] = library.recovery_id
        self._state["recovery_available"] = bool(library.recovery_id and not self._local_game_active)
        self._state["library_error"] = (
            "Some saved games could not be read. The library is protected against overwriting them."
            if library.invalid_records else None
        )
        self.async_set_updated_data(dict(self._state))  # type: ignore[attr-defined]

    def _capture_checkpoint(self, status: str | None = None) -> SavedGame | None:
        if getattr(self, "_library", None) is None or not getattr(self, "_saved_game_id", None):
            return None
        self._saved_revision += 1
        if status is None:
            status = "uncertain" if self._state.get("physical_operation") == "uncertain" else "paused" if self.paused else "playing"
        return SavedGame.capture(
            self._board.copy(), game_id=self._saved_game_id, revision=self._saved_revision,
            player_color="white" if self._our_color == chess.WHITE else "black",
            ai_level=int(self.ai_level), status=status,
            headers={
                "Event": "Phantom local game", "White": self._state.get("lichess_white_name") or "White",
                "Black": self._state.get("lichess_black_name") or "Black",
                "Result": self._board.result() if self._board.is_game_over() else "*",
            },
        )

    async def _write_checkpoint(self, snapshot: SavedGame) -> None:
        assert self._library is not None
        try:
            await self._library.put(snapshot)
            self._state["journal_error"] = None
        except (ValueError, OSError) as err:
            self._state["journal_error"] = f"Game could not be saved: {err}"
            self.paused = True
            self._state["game_status"] = "paused"
            _LOGGER.error("Phantom checkpoint failed: %s", err)
            raise
        finally:
            self._refresh_library_state()

    async def async_checkpoint(self, status: str | None = None) -> None:
        snapshot = self._capture_checkpoint(status)
        if snapshot is not None:
            await self._write_checkpoint(snapshot)

    def _queue_checkpoint(self, status: str | None = None) -> None:
        snapshot = self._capture_checkpoint(status)
        if snapshot is None:
            return
        task = self.hass.async_create_task(self._write_checkpoint(snapshot))
        self._journal_tasks.add(task)

        def done(completed: asyncio.Task) -> None:
            self._journal_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()  # Error is already surfaced in state/logs.

        task.add_done_callback(done)

    async def _flush_journal(self) -> None:
        tasks = list(getattr(self, "_journal_tasks", ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _begin_saved_session(self) -> None:
        if getattr(self, "_library", None) is None:
            return
        await self._flush_journal()
        self._saved_game_id = uuid4().hex
        self._saved_revision = 0
        await self.async_checkpoint("playing")

    async def async_save_game(self) -> dict[str, Any]:
        if getattr(self, "_library", None) is None:
            raise RuntimeError("The saved-game library is unavailable")
        if not self._local_game_active:
            raise ValueError("There is no active local game to save")
        if self._physical_operation_lock.locked():
            raise RuntimeError("Wait for the board to finish moving before saving")
        await self.async_set_pause(True)  # type: ignore[attr-defined]
        await self.async_checkpoint("paused")
        return {"game_id": self._saved_game_id, "saved": True}

    async def async_resume_game(self, game_id: str | None = None) -> None:
        library = getattr(self, "_library", None)
        if library is None:
            raise RuntimeError("The saved-game library is unavailable")
        async with self._local_start_lock:
            if self._local_game_active and (game_id is None or game_id == self._saved_game_id):
                return
            # Explicit recovery is allowed to reconcile an uncertain position.
            # Other starts still require a confirmed idle board.
            uncertain = self._state.get("physical_operation") == "uncertain"
            if uncertain:
                self._state["physical_operation"] = "idle"
            try:
                self._assert_no_active_game()  # type: ignore[attr-defined]
            finally:
                if uncertain:
                    self._state["physical_operation"] = "uncertain"
            selected = game_id or library.recovery_id
            if selected is None:
                raise ValueError("There is no unfinished game to resume")
            saved = library.get(selected)
            board = saved.board()
            if saved.status == "finished" or board.is_game_over():
                raise ValueError("This game is finished; open it in Review instead")
            color = chess.WHITE if saved.player_color == "white" else chess.BLACK
            self._phantom_session_initialized = False
            confirmed = await self._phantom_execute_position(  # type: ignore[attr-defined]
                fen=board.fen(), side="W" if color else "B", timeout_s=60.0,
                side_opcode="1" if board.turn == color else "2", select_chess_mode=True,
            )
            if not confirmed:
                raise TimeoutError("The board did not confirm the saved position. The game remains saved")
            self._board = board
            self._analysis_board = board.copy()
            self._our_color = color
            self.player_color = saved.player_color
            self.ai_level = saved.ai_level
            self._saved_game_id, self._saved_revision = saved.game_id, saved.revision
            self._game_id = None
            self._processed_moves = len(saved.moves)
            self._local_game_active = True
            self.paused = False
            history = []
            replay = board.root()
            for ply, move in enumerate(board.move_stack):
                history.append({"uci": move.uci(), "san": replay.san(move),
                                "move_num": replay.fullmove_number, "side": "white" if replay.turn else "black",
                                "classification": "unknown", "cpl": 0, "ply": ply + 1,
                                "motif": "", "color": "#888888", "glyph": ""})
                replay.push(move)
            self._state.update({
                "local_game_active": True, "lichess_active": False, "lichess_game_id": "local",
                "game_status": "playing", "lichess_review_ready": False,
                "live_fen": board.board_fen(), "last_move": saved.moves[-1] if saved.moves else None,
                "move_history_moves": history, "eval_cp": None, "eval_mate": None,
                "best_move_san": None, "threat_san": None, "last_game_result": None,
                "lichess_white_name": saved.headers.get("White", "White"),
                "lichess_black_name": saved.headers.get("Black", "Black"),
            })
            self._last_target_fen = board.board_fen()
            grid = self._build_phantom_matrix_from_fen(board.fen())  # type: ignore[attr-defined]
            self._state["piece_grid"] = grid
            self._state["piece_count"] = sum(c != "." for c in grid)
            await self.async_checkpoint("playing")
            if board.turn != color:
                await self._replace_local_game_task(name="phantom_resumed_ai")  # type: ignore[attr-defined]

    async def async_game_library(self, action: str, **data: Any) -> dict[str, Any]:
        library = getattr(self, "_library", None)
        if library is None:
            raise RuntimeError("The saved-game library is unavailable")
        if action == "list":
            return {"games": library.list(data.get("query", "")), "recovery_id": library.recovery_id}
        if action == "import":
            game = SavedGame.from_pgn(data.get("pgn", ""))
            await library.put(game, recovery=False)
            self._refresh_library_state()
            return game.summary()
        game_id = data.get("game_id") or self._saved_game_id or library.recovery_id
        if not game_id:
            raise ValueError("Choose a saved game")
        game = library.get(game_id)
        if action in ("analyze", "reanalyze", "review", "cancel_review"):
            reviews = getattr(self, "_reviews", None)
            if reviews is None:
                raise RuntimeError("Game analysis is unavailable")
            if action in ("analyze", "reanalyze"):
                return reviews.start(game_id, force=action == "reanalyze")
            if action == "cancel_review":
                if reviews.current_id != game_id:
                    raise ValueError("This game has no running analysis")
                await reviews.cancel()
            return reviews.report(game_id)
        if action == "export":
            board = chess.Board(game.initial_fen)
            positions, sans = [board.fen()], []
            for uci in game.moves:
                move = chess.Move.from_uci(uci)
                sans.append(board.san(move))
                board.push(move)
                positions.append(board.fen())
            return {"pgn": game.pgn(), "game_id": game_id, "moves": list(game.moves),
                    "initial_fen": game.initial_fen, "positions": positions, "sans": sans}
        if action == "practice":
            ply = data.get("ply", 0)
            if not isinstance(ply, int) or not 0 <= ply <= len(game.moves):
                raise ValueError("Choose a position within this game")
            board = chess.Board(game.initial_fen)
            for uci in game.moves[:ply]:
                board.push_uci(uci)
            practice = SavedGame.capture(
                chess.Board(board.fen()), status="paused",
                player_color="white" if board.turn else "black", ai_level=int(self.ai_level),
                headers={"Event": "Practice position", "White": "White", "Black": "Black", "Result": "*"},
            )
            await library.put(practice, recovery=False)
            self._refresh_library_state()
            return practice.summary()
        if action == "delete":
            reviews = getattr(self, "_reviews", None)
            if reviews is not None and reviews.current_id == game_id:
                await reviews.cancel()
            if self._local_game_active and game_id == self._saved_game_id:
                raise ValueError("End the active game before deleting its saved copy")
            await library.delete(game_id)
            if reviews is not None:
                await reviews.discard(game_id)
            self._refresh_library_state()
            return {"deleted": game_id}
        raise ValueError("Unknown game-library action")

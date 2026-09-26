"""Validated, versioned chess sessions persisted through Home Assistant Store."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import io
from typing import Any
from uuid import uuid4

import chess
import chess.pgn

MAX_GAMES = 200
MAX_PLIES = 4096
MAX_PGN_BYTES = 256_000
STATUSES = {"starting", "playing", "paused", "uncertain", "finished"}


@dataclass(frozen=True)
class SavedGame:
    """Immutable checkpoint; a revision cannot overwrite a later checkpoint."""

    game_id: str
    revision: int
    initial_fen: str
    moves: tuple[str, ...]
    player_color: str
    ai_level: int
    status: str
    updated: str
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def capture(
        cls, board: chess.Board, *, game_id: str | None = None,
        revision: int = 0, player_color: str = "white", ai_level: int = 3,
        status: str = "paused", headers: dict[str, str] | None = None,
    ) -> SavedGame:
        return cls.decode({
            "game_id": game_id or uuid4().hex, "revision": revision,
            "initial_fen": board.root().fen(),
            "moves": [m.uci() for m in board.move_stack],
            "player_color": player_color, "ai_level": ai_level, "status": status,
            "updated": datetime.now(timezone.utc).isoformat(), "headers": headers or {},
        })

    @classmethod
    def decode(cls, data: Any) -> SavedGame:
        if not isinstance(data, dict):
            raise ValueError("Saved game must be an object")
        game_id = data.get("game_id")
        revision = data.get("revision")
        moves = data.get("moves")
        level = data.get("ai_level")
        headers = data.get("headers", {})
        if not isinstance(game_id, str) or not game_id.isalnum() or len(game_id) > 64:
            raise ValueError("Invalid saved-game ID")
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid saved-game revision")
        if type(level) is not int or not 1 <= level <= 8:
            raise ValueError("Invalid saved difficulty")
        if data.get("player_color") not in ("white", "black") or data.get("status") not in STATUSES:
            raise ValueError("Invalid saved-game settings")
        if not isinstance(moves, (list, tuple)) or len(moves) > MAX_PLIES:
            raise ValueError("Invalid saved move list")
        if not isinstance(headers, dict) or len(headers) > 20 or any(
            not isinstance(k, str) or not isinstance(v, str) or len(k) > 40 or len(v) > 200
            for k, v in headers.items()
        ):
            raise ValueError("Invalid game headers")
        updated = data.get("updated")
        if not isinstance(updated, str):
            raise ValueError("Invalid checkpoint timestamp")
        stamp = datetime.fromisoformat(updated)
        if stamp.tzinfo is None:
            raise ValueError("Checkpoint timestamp must include a timezone")
        initial_fen = data.get("initial_fen")
        if not isinstance(initial_fen, str):
            raise ValueError("Missing starting position")
        game = cls(game_id, revision, initial_fen, tuple(moves), data["player_color"],
                   level, data["status"], stamp.astimezone(timezone.utc).isoformat(), dict(headers))
        game.board()  # Replay every move before accepting a checkpoint.
        return game

    def board(self) -> chess.Board:
        board = chess.Board(self.initial_fen)
        if not board.is_valid():
            raise ValueError("Saved starting position is not a valid chess position")
        for uci in self.moves:
            if not isinstance(uci, str):
                raise ValueError("Invalid saved move")
            board.push_uci(uci)
        return board

    def encode(self) -> dict[str, Any]:
        data = asdict(self)
        data["moves"] = list(self.moves)
        return data

    def summary(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id, "status": self.status,
            "updated": self.updated, "plies": len(self.moves),
            "white": self.headers.get("White", "White"),
            "black": self.headers.get("Black", "Black"),
            "result": self.headers.get("Result", "*"),
            "player_color": self.player_color, "ai_level": self.ai_level,
        }

    def pgn(self) -> str:
        game = chess.pgn.Game.from_board(self.board())
        for key in ("Event", "Site", "Date", "White", "Black", "Result"):
            if key in self.headers:
                game.headers[key] = self.headers[key]
        return str(game.accept(chess.pgn.StringExporter(headers=True, variations=False, comments=False)))

    @classmethod
    def from_pgn(cls, text: str) -> SavedGame:
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_PGN_BYTES:
            raise ValueError("Import one PGN game of at most 256 KB")
        source = io.StringIO(text)
        game = chess.pgn.read_game(source)
        if game is None or game.errors:
            raise ValueError("The PGN contains an invalid game")
        if chess.pgn.read_game(source) is not None:
            raise ValueError("Import one game at a time")
        board = game.end().board()
        headers = {key: game.headers[key] for key in ("Event", "Date", "White", "Black", "Result") if key in game.headers}
        return cls.capture(board, headers=headers,
                           status="finished" if board.is_game_over() or headers.get("Result", "*") != "*" else "paused")


class GameLibrary:
    """Serialize atomic saves and preserve existing data if persistence fails."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._lock = asyncio.Lock()
        self._games: dict[str, SavedGame] = {}
        self.recovery_id: str | None = None
        self.invalid_records = 0

    async def load(self) -> None:
        data = await self._store.async_load()
        if data is None:
            return
        if not isinstance(data, dict) or not isinstance(data.get("games"), list):
            raise ValueError("The saved-game library is unreadable; preserve the storage file for recovery")
        games: dict[str, SavedGame] = {}
        for raw in data["games"]:
            try:
                game = SavedGame.decode(raw)
            except (ValueError, TypeError, KeyError):
                self.invalid_records += 1
                continue
            games[game.game_id] = game
        self._games = games
        recovery = data.get("recovery_id")
        self.recovery_id = recovery if isinstance(recovery, str) and recovery in games and games[recovery].status != "finished" else None

    def get(self, game_id: str) -> SavedGame:
        try:
            return self._games[game_id]
        except KeyError as err:
            raise ValueError("Saved game not found") from err

    def list(self, query: str = "") -> list[dict[str, Any]]:
        query = query.casefold().strip()
        return [g.summary() for g in sorted(self._games.values(), key=lambda g: g.updated, reverse=True)
                if not query or query in " ".join(g.headers.values()).casefold() or query in g.game_id]

    async def put(self, game: SavedGame, *, recovery: bool = True) -> None:
        async with self._lock:
            existing = self._games.get(game.game_id)
            if existing is not None and existing.revision > game.revision:
                return
            if existing is None and len(self._games) >= MAX_GAMES:
                raise ValueError("Game library is full. Export and delete a saved game before adding another")
            games = {**self._games, game.game_id: game}
            recovery_id = self.recovery_id
            if recovery:
                if game.status != "finished":
                    recovery_id = game.game_id
                elif recovery_id == game.game_id:
                    recovery_id = None
            await self._persist(games, recovery_id)
            self._games, self.recovery_id = games, recovery_id

    async def delete(self, game_id: str) -> None:
        async with self._lock:
            self.get(game_id)
            games = {k: v for k, v in self._games.items() if k != game_id}
            recovery_id = None if self.recovery_id == game_id else self.recovery_id
            await self._persist(games, recovery_id)
            self._games, self.recovery_id = games, recovery_id

    async def _persist(self, games: dict[str, SavedGame], recovery_id: str | None) -> None:
        # Refuse to overwrite a library containing records we could not decode.
        # This protects potentially recoverable data instead of silently losing it.
        if self.invalid_records:
            raise ValueError("Some saved games could not be read. Repair the library before changing it")
        await self._store.async_save({"games": [g.encode() for g in games.values()], "recovery_id": recovery_id})

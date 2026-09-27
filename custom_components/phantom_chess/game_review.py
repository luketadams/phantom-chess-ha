"""Restart-safe, bounded local analysis. This module never controls the board."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from typing import Any, Callable

import chess

from .game_library import GameLibrary, SavedGame
from .move_quality import chance_loss, quality_grade, winning_chances

REVIEW_VERSION = 1
MAX_REVIEW_PLIES = 400
GRADES = {"best": "Best", "excellent": "Excellent", "good": "Good", "inaccuracy": "Inaccuracy", "mistake": "Mistake", "blunder": "Blunder"}


def fingerprint(game: SavedGame) -> str:
    """A metadata-only save must not discard analysis; a changed line must."""
    return hashlib.sha256(json.dumps([REVIEW_VERSION, game.initial_fen, game.moves]).encode()).hexdigest()


def positions(game: SavedGame, limit: int | None = None) -> list[chess.Board]:
    board = chess.Board(game.initial_fen)
    result = [board.copy()]
    for uci in game.moves[:limit]:
        board.push_uci(uci)
        result.append(board.copy())
    return result


def terminal_evaluation(board: chess.Board) -> dict[str, Any] | None:
    # claimable draws are not automatically final. Repetition is handled by
    # the full saved move history when appropriate, not a FEN-only inference.
    outcome = board.outcome(claim_draw=False)
    if outcome is None:
        return None
    winner = "draw" if outcome.winner is None else "white" if outcome.winner else "black"
    return {"cp": 0 if winner == "draw" else 10000 if winner == "white" else -10000,
            "mate": None, "depth": None, "pv": [], "source": "rules", "terminal": winner}


def evaluation(board: chess.Board, ev: Any) -> dict[str, Any]:
    """Accept real scores and legal PVs only; never turn failure into equality."""
    if ev is None or (type(ev.cp) is not int and type(ev.mate) is not int):
        raise ValueError("Local analysis is unavailable. Check the engine and retry.")
    line = (ev.raw or {}).get("pv", []) or ([ev.best_uci] if ev.best_uci else [])
    replay = board.copy(stack=False)
    pv = []
    for uci in line[:8]:
        try:
            move = chess.Move.from_uci(uci)
        except (ValueError, TypeError):
            break
        if move not in replay.legal_moves:
            break
        pv.append(uci)
        replay.push(move)
    return {"cp": ev.cp, "mate": ev.mate, "depth": ev.depth, "pv": pv,
            "source": "stockfish-local", "terminal": None}


def win_percent(ev: dict[str, Any]) -> float:
    value = winning_chances(ev["cp"], ev["mate"])
    if value is None:
        raise ValueError("Analysis score is unavailable")
    return value


def score_label(ev: dict[str, Any]) -> str:
    if ev.get("terminal"):
        return "Draw" if ev["terminal"] == "draw" else f"Checkmate: {ev['terminal'].title()} wins"
    if ev["mate"] is not None:
        return f"{'White' if ev['mate'] > 0 else 'Black'} mates in {abs(ev['mate'])}"
    return f"{ev['cp'] / 100:+.1f}"


def move_feedback(board: chess.Board, uci: str, before: dict[str, Any], after: dict[str, Any], ply: int) -> dict[str, Any]:
    move = chess.Move.from_uci(uci)
    white = board.turn == chess.WHITE
    loss = chance_loss(win_percent(before), win_percent(after), white)
    best = bool(before["pv"] and uci == before["pv"][0])
    grade = quality_grade(loss, best)
    variation = board.copy(stack=False)
    sans = []
    for candidate in before["pv"]:
        next_move = chess.Move.from_uci(candidate)
        sans.append(variation.san(next_move))
        variation.push(next_move)
    san = board.san(move)
    reason = (f"This move reduced the engine's estimated winning chances by {loss:.1f} percentage points."
              if loss >= 5 else "This move preserves the position's engine evaluation."
              if not best else "You found the engine's preferred move.")
    if sans and not best:
        reason += f" Compare {sans[0]} and the suggested continuation."
    return {"ply": ply, "move_num": board.fullmove_number, "side": "white" if white else "black",
            "san": san, "uci": uci, "classification": grade, "label": GRADES[grade],
            "chance_loss": round(loss, 2), "before": score_label(before), "after": score_label(after),
            "best_san": sans[0] if sans else None, "variation": sans, "coaching": reason}


class ReviewManager:
    """One resumable job per board; Store commits precede durable completion."""

    def __init__(self, store: Any, library: GameLibrary, evaluate: Callable[..., Any],
                 busy: Callable[[], bool], publish: Callable[[dict[str, Any]], None]) -> None:
        self.store, self.library, self.evaluate = store, library, evaluate
        self.busy, self.publish = busy, publish
        self.records: dict[str, dict[str, Any]] = {}
        self.task: asyncio.Task[None] | None = None
        self.current_id: str | None = None
        self.error: str | None = None

    async def load(self) -> None:
        try:
            data = await self.store.async_load()
        except (OSError, ValueError):
            self.error = "Analysis storage is unavailable; saved games use separate storage."
            self._publish()
            return
        if data is None:
            return
        try:
            if not isinstance(data, dict) or data.get("version") != REVIEW_VERSION:
                raise ValueError("Unsupported analysis cache")
            saved_ids = {game["game_id"] for game in self.library.list()}
            for game_id, record in data["reviews"].items():
                if game_id not in saved_ids:
                    continue  # A deleted game's cache is not corrupt game data.
                try:
                    game = self.library.get(game_id)
                    if len(game.moves) > MAX_REVIEW_PLIES:
                        continue
                    boards = positions(game)
                    if record["fingerprint"] != fingerprint(game):
                        continue
                    rows = record["evaluations"]
                    if not isinstance(rows, list) or len(rows) > len(boards) or len(rows) > MAX_REVIEW_PLIES + 1:
                        raise ValueError("Invalid analysis length")
                    for board, row in zip(boards, rows, strict=False):
                        if not isinstance(row, dict) or not isinstance(row.get("pv"), list) or len(row["pv"]) > 8:
                            raise ValueError("Invalid analysis line")
                        required = {"cp", "mate", "depth", "pv", "source", "terminal"}
                        if not required <= row.keys():
                            raise ValueError("Incomplete analysis record")
                        cp, mate = row["cp"], row["mate"]
                        if not ((type(cp) is int and abs(cp) <= 100000 and mate is None)
                                or (cp is None and type(mate) is int and 0 < abs(mate) <= 10000)):
                            raise ValueError("Invalid analysis score")
                        if row.get("depth") is not None and type(row["depth"]) is not int:
                            raise ValueError("Invalid analysis depth")
                        if row.get("source") not in ("rules", "stockfish-local") or row.get("terminal") not in (None, "white", "black", "draw"):
                            raise ValueError("Invalid analysis provenance")
                        if row["source"] == "rules" and row != terminal_evaluation(board):
                            raise ValueError("Terminal evaluation does not match the game")
                        if row["source"] == "stockfish-local" and row["terminal"] is not None:
                            raise ValueError("Engine record cannot assert a terminal result")
                        for uci in row["pv"]:
                            board.push_uci(uci)
                    self.records[game_id] = {"fingerprint": record["fingerprint"], "evaluations": rows,
                                             "status": "complete" if len(rows) == len(boards) else "paused", "error": None}
                except (KeyError, ValueError, TypeError):
                    self.error = "Some cached analysis could not be read. Reanalyze those games; saved moves are intact."
        except (KeyError, ValueError, TypeError, AttributeError):
            self.error = "Cached analysis could not be read. Reanalyze games; saved moves are intact."
        self._publish()

    def _record(self, game: SavedGame) -> dict[str, Any]:
        record = self.records.get(game.game_id)
        if record is None or record["fingerprint"] != fingerprint(game):
            return {"fingerprint": fingerprint(game), "evaluations": [], "status": "not_started", "error": None}
        return record

    def summary(self, game_id: str) -> dict[str, Any]:
        game = self.library.get(game_id)
        record = self._record(game)
        return {"game_id": game_id, "status": record["status"], "analyzed": max(0, len(record["evaluations"]) - 1),
                "total": len(game.moves), "error": record["error"]}

    def _publish(self) -> None:
        summaries = [self.summary(g["game_id"]) for g in self.library.list() if g["game_id"] in self.records]
        self.publish({"games": summaries, "error": self.error})

    def report(self, game_id: str) -> dict[str, Any]:
        game = self.library.get(game_id)
        record = self._record(game)
        rows = record["evaluations"]
        boards = positions(game, max(0, len(rows) - 1))
        moves = [move_feedback(boards[i], uci, rows[i], rows[i+1], i+1)
                 for i, uci in enumerate(game.moves[:max(0, len(rows)-1)])]
        return {**self.summary(game_id), "version": REVIEW_VERSION, "moves": moves,
                "evaluations": deepcopy(rows), "method": "Local Stockfish · depth up to 18 · 1.5 seconds per position. Chance-loss grades use the Lichess curve and Phantom thresholds, not Chess.com ratings."}

    async def _save(self) -> None:
        # Analysis is expendable cache; saved games have their own protected Store.
        valid = {g["game_id"] for g in self.library.list()}
        self.records = {k: v for k, v in self.records.items() if k in valid}
        await self.store.async_save({"version": REVIEW_VERSION, "reviews": deepcopy(self.records)})
        self.error = None  # The replacement cache is now readable and durable.

    def start(self, game_id: str, *, force: bool = False) -> dict[str, Any]:
        game = self.library.get(game_id)
        if len(game.moves) > MAX_REVIEW_PLIES:
            raise ValueError(f"Review currently supports games up to {MAX_REVIEW_PLIES} half-moves")
        if self.task is not None and not self.task.done():
            if self.current_id == game_id:
                return self.summary(game_id)
            raise ValueError("Another game is being analyzed. Cancel it before starting this review.")
        if self.busy():
            raise ValueError("End the current game before starting a review")
        record = self._record(game) if not force else {"fingerprint": fingerprint(game), "evaluations": [], "status": "not_started", "error": None}
        if record["status"] == "complete":
            return self.summary(game_id)
        self.records[game_id] = record
        record.update(status="running", error=None)
        self.current_id = game_id
        self.task = asyncio.create_task(self._run(game, record), name="phantom_game_review")
        self._publish()
        return self.summary(game_id)

    async def _run(self, game: SavedGame, record: dict[str, Any]) -> None:
        try:
            async with asyncio.timeout(1800):
                for board in positions(game)[len(record["evaluations"]):]:
                    if self.busy() or fingerprint(self.library.get(game.game_id)) != record["fingerprint"]:
                        record["status"] = "paused"
                        break
                    result = terminal_evaluation(board)
                    if result is None:
                        async with asyncio.timeout(150 if not record["evaluations"] else 15):
                            result = evaluation(board, await self.evaluate(board.fen()))
                    record["evaluations"].append(result)
                    if len(record["evaluations"]) % 5 == 0:
                        await self._save()
                    self._publish()
                    await asyncio.sleep(0)  # Let game-start/cancel requests run between positions.
                else:
                    record["status"] = "complete"
        except asyncio.CancelledError:
            record["status"] = "paused"
        except Exception as err:
            record.update(status="failed", error=str(err) or "Analysis timed out; retry to continue.")
        finally:
            try:
                await self._save()
            except Exception:
                record.update(status="failed", error="Analysis could not be saved. Check storage and retry.")
            self._publish()

    async def discard(self, game_id: str) -> None:
        self.records.pop(game_id, None)
        try:
            await self._save()
        except Exception:
            self.error = "The game was deleted, but cached analysis cleanup failed. Check storage."
        self._publish()

    async def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                # Cancellation can precede the coroutine's first instruction.
                record = self.records.get(self.current_id or "")
                if record is not None:
                    record["status"] = "paused"
                await self._save()
                self._publish()

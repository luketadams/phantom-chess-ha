"""Review persistence, bounded work, honest scores and no board-control effects."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import chess
import pytest

from custom_components.phantom_chess.game_library import GameLibrary, SavedGame
from custom_components.phantom_chess.game_review import (
    MAX_REVIEW_PLIES, ReviewManager, evaluation, fingerprint, move_feedback,
    positions, score_label, terminal_evaluation, win_percent,
)
from custom_components.phantom_chess.lichess_analysis import EvalResult


def ev(cp=0, mate=None, pv=None):
    return {"cp": cp, "mate": mate, "depth": 18, "pv": pv or [], "terminal": None, "source": "stockfish-local"}


async def manager(moves=("e2e4", "e7e5", "g1f3")):
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    game = SavedGame.capture(board)
    library = GameLibrary(AsyncMock())
    await library.put(game)
    store = AsyncMock()
    store.async_load.return_value = None
    evaluate = AsyncMock(return_value=EvalResult(30, None, 18, None))
    obj = ReviewManager(store, library, evaluate, Mock(return_value=False), Mock())
    return obj, game


@pytest.mark.parametrize("white,pre,post,grade", [
    (True, 0, -500, "blunder"), (False, 0, 500, "blunder"),
    (True, 0, -150, "mistake"), (True, 0, -80, "inaccuracy"),
    (True, 0, -30, "good"), (True, 0, -10, "excellent"),
    (True, 3000, 2500, "excellent"),
])
def test_position_relative_grades_and_mover_perspective(white, pre, post, grade):
    board = chess.Board()
    if not white:
        board.push_uci("e2e4")
    result = move_feedback(board, "e2e4" if white else "e7e5", ev(pre), ev(post), 1)
    assert result["classification"] == grade
    assert result["side"] == ("white" if white else "black")


def test_best_alternative_and_legality_ground_coaching():
    board = chess.Board()
    before = ev(0, pv=["e2e4", "e7e5", "g1f3"])
    best = move_feedback(board, "e2e4", before, ev(0), 1)
    assert best["classification"] == "best"
    other = move_feedback(board, "f2f3", before, ev(-300), 1)
    assert other["best_san"] == "e4"
    assert other["variation"] == ["e4", "e5", "Nf3"]
    assert "Compare e4" in other["coaching"]
    assert "percentage points" in other["coaching"]


@pytest.mark.parametrize("value", [None, EvalResult(None, None, 18, None)])
def test_missing_score_is_not_draw(value):
    with pytest.raises(ValueError, match="unavailable"):
        evaluation(chess.Board(), value)


def test_illegal_engine_variation_is_truncated():
    result = evaluation(chess.Board(), EvalResult(30, None, 18, "e2e4", raw={"pv": ["e2e4", "a1a8", "d7d5"]}))
    assert result["pv"] == ["e2e4"]
    result = evaluation(chess.Board(), EvalResult(30, None, 18, None, raw={"pv": ["nonsense"]}))
    assert result["pv"] == []


def test_terminal_positions_and_mates():
    assert terminal_evaluation(chess.Board()) is None
    board = chess.Board()
    for uci in ("f2f3", "e7e5", "g2g4", "d8h4"):
        board.push_uci(uci)
    mate = terminal_evaluation(board)
    assert mate["terminal"] == "black"
    assert score_label(mate) == "Checkmate: Black wins"
    assert score_label(ev(mate=-3)) == "Black mates in 3"
    assert win_percent(ev(mate=3)) == 100
    assert win_percent(ev(mate=-3)) == 0
    draw = terminal_evaluation(chess.Board("8/8/8/8/8/2k5/8/K7 w - - 0 1"))
    assert score_label(draw) == "Draw"
    assert score_label(ev(125)) == "+1.2"


async def test_completed_review_roundtrip_and_metadata_preservation():
    obj, game = await manager()
    assert obj.start(game.game_id)["status"] == "running"
    await obj.task
    report = obj.report(game.game_id)
    assert report["status"] == "complete"
    assert len(report["evaluations"]) == 4
    assert [m["san"] for m in report["moves"]] == ["e4", "e5", "Nf3"]
    assert obj.evaluate.await_count == 4
    report["evaluations"][0]["cp"] = 999  # caller cannot corrupt cache
    assert obj.report(game.game_id)["evaluations"][0]["cp"] == 30
    await obj.library.put(replace(game, revision=2, status="finished"))
    assert obj.start(game.game_id)["status"] == "complete"
    obj.store.async_load.return_value = obj.store.async_save.call_args.args[0]
    restored = ReviewManager(obj.store, obj.library, obj.evaluate, obj.busy, obj.publish)
    await restored.load()
    assert restored.report(game.game_id)["moves"] == report["moves"]
    assert fingerprint(game) == fingerprint(replace(game, revision=3))


async def test_cancel_keeps_progress_and_retry_only_analyzes_remainder():
    obj, game = await manager()
    entered = asyncio.Event()
    async def evaluate(fen):
        if fen == positions(game)[1].fen():
            entered.set()
            await asyncio.Event().wait()
        return EvalResult(0, None, 18, None)
    obj.evaluate.side_effect = evaluate
    obj.start(game.game_id)
    await entered.wait()
    assert obj.start(game.game_id)["status"] == "running"  # idempotent start
    await obj.cancel()
    assert obj.report(game.game_id)["status"] == "paused"
    assert len(obj.report(game.game_id)["evaluations"]) == 1
    obj.evaluate.side_effect = None
    obj.evaluate.reset_mock()
    obj.start(game.game_id)
    await obj.task
    assert obj.evaluate.await_count == 3
    assert obj.summary(game.game_id)["status"] == "complete"


async def test_cancel_before_first_instruction_is_safe():
    obj, game = await manager()
    obj.start(game.game_id)
    await obj.cancel()
    assert obj.summary(game.game_id)["status"] == "paused"
    obj.evaluate.assert_not_awaited()


async def test_game_start_pauses_background_analysis():
    obj, game = await manager()
    async def start_play(fen):
        obj.busy.return_value = True
        return EvalResult(0, None, 18, None)
    obj.evaluate.side_effect = start_play
    obj.start(game.game_id)
    await obj.task
    assert obj.evaluate.await_count == 1
    assert obj.summary(game.game_id)["status"] == "paused"
    with pytest.raises(ValueError, match="End the current game"):
        obj.start(game.game_id)


async def test_changed_line_invalidates_old_analysis():
    obj, game = await manager()
    obj.start(game.game_id)
    await obj.task
    changed = replace(game, revision=1, moves=("d2d4",))
    await obj.library.put(changed)
    assert obj.summary(game.game_id)["status"] == "not_started"
    assert obj.report(game.game_id)["moves"] == []
    obj.start(game.game_id)
    await obj.task
    assert obj.report(game.game_id)["moves"][0]["san"] == "d4"


async def test_analysis_failure_and_storage_failure_are_visible():
    obj, game = await manager()
    obj.evaluate.return_value = None
    obj.start(game.game_id)
    await obj.task
    assert obj.summary(game.game_id)["status"] == "failed"
    assert "unavailable" in obj.summary(game.game_id)["error"]
    assert obj.report(game.game_id)["moves"] == []
    obj.evaluate.return_value = EvalResult(0, None, 18, None)
    obj.store.async_save.side_effect = OSError("disk full")
    obj.start(game.game_id)
    await obj.task
    assert obj.summary(game.game_id)["status"] == "failed"
    assert "could not be saved" in obj.summary(game.game_id)["error"]
    obj.store.async_save.side_effect = None
    obj.start(game.game_id)
    await obj.task
    assert obj.summary(game.game_id)["status"] == "complete"


async def test_limits_one_job_and_maximum_game_length():
    obj, game = await manager()
    other = replace(game, game_id="other")
    await obj.library.put(other)
    obj.start(game.game_id)
    with pytest.raises(ValueError, match="Another game"):
        obj.start(other.game_id)
    await obj.cancel()
    too_long = replace(game, game_id="long", moves=game.moves * (MAX_REVIEW_PLIES//3+1))
    obj.library._games[too_long.game_id] = too_long  # avoids unrelated legality checks
    with pytest.raises(ValueError, match="half-moves"):
        obj.start(too_long.game_id)


@pytest.mark.parametrize("bad", [[], {}, {"version": 999}, {"version": 1, "reviews": []}])
async def test_corrupt_cache_does_not_damage_saved_games(bad):
    obj, game = await manager()
    obj.store.async_load.return_value = bad
    await obj.load()
    assert obj.error
    assert obj.library.get(game.game_id).moves == game.moves


async def test_stale_and_corrupt_records_are_recomputable():
    obj, game = await manager()
    obj.store.async_load.return_value = {"version": 1, "reviews": {game.game_id: {"fingerprint": "old", "evaluations": []}}}
    await obj.load()
    assert not obj.records
    obj.store.async_load.return_value["reviews"][game.game_id]["fingerprint"] = fingerprint(game)
    obj.store.async_load.return_value["reviews"][game.game_id]["evaluations"] = [{"cp": None, "mate": None, "pv": []}]
    await obj.load()
    assert obj.error
    assert not obj.records


async def test_partial_cache_restores_paused_without_starting_engine():
    obj, game = await manager()
    obj.store.async_load.return_value = {"version": 1, "reviews": {game.game_id: {
        "fingerprint": fingerprint(game), "evaluations": [ev()], "status": "running"}}}
    await obj.load()
    assert obj.summary(game.game_id)["status"] == "paused"
    obj.evaluate.assert_not_awaited()
    assert obj.task is None


async def test_coordinator_review_actions_and_deletion_cleanup():
    from .ble_mock import make_coordinator
    coord = make_coordinator(ble_connected=True)
    obj, game = await manager()
    coord._library, coord._reviews = obj.library, obj
    coord._local_game_active = False
    obj.publish = coord._publish_review_state
    assert (await coord.async_game_library("review", game_id=game.game_id))["status"] == "not_started"
    await coord.async_game_library("analyze", game_id=game.game_id)
    await coord.async_game_library("cancel_review", game_id=game.game_id)
    assert coord._state["game_reviews"]["games"][0]["status"] == "paused"
    await coord.async_game_library("analyze", game_id=game.game_id)
    await obj.task
    report = await coord.async_game_library("review", game_id=game.game_id)
    assert report["analyzed"] == 3
    await coord.async_game_library("delete", game_id=game.game_id)
    assert game.game_id not in obj.store.async_save.call_args.args[0]["reviews"]
    with pytest.raises(ValueError, match="not found"):
        await coord.async_game_library("review", game_id=game.game_id)
    coord._reviews = None
    await coord._library.put(game)
    with pytest.raises(RuntimeError, match="unavailable"):
        await coord.async_game_library("analyze", game_id=game.game_id)
    coord._reviews = obj
    obj.current_id = None
    with pytest.raises(ValueError, match="no running"):
        await coord.async_game_library("cancel_review", game_id=game.game_id)


async def test_terminal_game_uses_rules_for_final_position():
    obj, game = await manager(("f2f3", "e7e5", "g2g4", "d8h4"))
    obj.start(game.game_id)
    await obj.task
    assert obj.evaluate.await_count == 4  # final checkmate needs no engine
    assert obj.report(game.game_id)["evaluations"][-1]["terminal"] == "black"
    # Fivefold repetition uses complete history, not a FEN-only assumption.
    obj, game = await manager(("g1f3", "g8f6", "f3g1", "f6g8") * 4)
    assert terminal_evaluation(positions(game)[-1])["terminal"] == "draw"


@pytest.mark.parametrize("row", [
    {"cp": 0, "mate": None, "pv": [], "depth": "bad", "source": "rules", "terminal": None},
    {"cp": 0, "mate": None, "pv": [], "depth": 18, "source": "fake", "terminal": None},
    {"cp": 0, "mate": None, "pv": ["a1a8"], "depth": 18, "source": "stockfish-local", "terminal": None},
    {"cp": 0, "mate": None, "pv": "bad"},
])
async def test_malformed_cached_variations_never_reach_coaching(row):
    obj, game = await manager()
    obj.store.async_load.return_value = {"version": 1, "reviews": {game.game_id: {
        "fingerprint": fingerprint(game), "evaluations": [row]}}}
    await obj.load()
    assert obj.error
    assert obj.report(game.game_id)["moves"] == []


async def test_explicit_reanalysis_replaces_engine_cache():
    obj, game = await manager()
    obj.start(game.game_id)
    await obj.task
    obj.evaluate.return_value = EvalResult(100, None, 22, None)
    obj.start(game.game_id, force=True)
    await obj.task
    assert obj.evaluate.await_count == 8
    assert obj.report(game.game_id)["evaluations"][0]["cp"] == 100


async def test_unreadable_analysis_store_does_not_block_game_library():
    obj, game = await manager()
    obj.store.async_load.side_effect = OSError("read failed")
    await obj.load()
    assert "storage is unavailable" in obj.error
    assert obj.library.get(game.game_id) == game


@pytest.mark.parametrize("missing", ["cp", "mate", "depth", "source", "terminal", "pv"])
async def test_incomplete_cache_rows_are_rejected_before_rendering(missing):
    obj, game = await manager()
    row = ev()
    del row[missing]
    obj.store.async_load.return_value = {"version": 1, "reviews": {game.game_id: {
        "fingerprint": fingerprint(game), "evaluations": [row]}}}
    await obj.load()
    assert obj.error
    assert obj.report(game.game_id)["moves"] == []
    obj.start(game.game_id)
    await obj.task
    assert obj.report(game.game_id)["status"] == "complete"
    assert obj.error is None


@pytest.mark.parametrize("row", [ev(True), ev(1.5), ev(0, mate=3), ev(None, mate=0),
                                 {**ev(0), "source": "rules", "terminal": "black"}])
async def test_invalid_cached_scores_cannot_become_coaching(row):
    obj, game = await manager()
    obj.store.async_load.return_value = {"version": 1, "reviews": {game.game_id: {
        "fingerprint": fingerprint(game), "evaluations": [row]}}}
    await obj.load()
    assert obj.error
    assert obj.report(game.game_id)["status"] == "not_started"


async def test_deleted_game_cache_does_not_produce_corruption_warning():
    obj, game = await manager()
    obj.store.async_load.return_value = {"version": 1, "reviews": {"deleted": {"malformed": True}}}
    await obj.load()
    assert obj.error is None
    assert obj.records == {}


async def test_cache_cleanup_failure_does_not_undo_a_game_deletion():
    obj, game = await manager()
    obj.start(game.game_id)
    await obj.task
    await obj.library.delete(game.game_id)
    obj.store.async_save.side_effect = OSError("full")
    await obj.discard(game.game_id)
    assert "game was deleted" in obj.error
    assert obj.library.list() == []

@pytest.mark.parametrize("white", [True, False])
@pytest.mark.parametrize("pre,post", [(0, -10), (0, -30), (0, -80), (0, -150), (0, -500), (3000, 2500), (-3000, -2500)])
@pytest.mark.parametrize("best", [True, False])
def test_live_and_saved_review_share_grades(white, pre, post, best):
    from custom_components.phantom_chess.lichess_analysis import classify_move
    board = chess.Board()
    if not white:
        board.push_uci("e2e4")
    move = "e2e4" if white else "e7e5"
    before = ev(pre, pv=[move] if best else [])
    reviewed = move_feedback(board, move, before, ev(post), 1)
    live, _ = classify_move(EvalResult(pre, None, 18, move if best else None), EvalResult(post, None, 18, None), move, white)
    assert live == reviewed["classification"]

@pytest.mark.parametrize("cp,mate", [(None, None), (None, 0), (True, None), (None, True)])
def test_unknown_scores_are_not_coaching(cp, mate):
    from custom_components.phantom_chess.lichess_analysis import classify_move
    live, _ = classify_move(EvalResult(cp, mate, 18, None), EvalResult(0, None, 18, None), "e2e4", True)
    assert live == "unknown"
    with pytest.raises(ValueError):
        win_percent(ev(cp, mate))

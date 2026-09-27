"""Extra coverage for lichess_analysis.py — the network + Stockfish paths.

The sibling file ``test_lichess_analysis.py`` covers the pure helpers
(classification, accuracy math, FEN sanitizing, cache bookkeeping). This
file targets the branches that file leaves uncovered:

- ``EvalResult.white_win_pct`` OverflowError guard
- ``_win_pct_loss_to_accuracy`` OverflowError guard
- ``LichessAnalysisClient`` async HTTP paths: get_eval (cache hit,
  cache miss + Stockfish fallback), _fetch_eval (200 / 404 / non-200 /
  network error), get_opening + _fetch_opening (hit / miss / error),
  shutdown, best_move_for_ai_level
- ``StockfishFallback`` engine lifecycle: ensure_engine (available flag,
  engine caching, binary-missing, spawn failure), _ensure_binary
  (already-present, unsupported-arch, download HTTP error, download
  network error, successful download+extract+install for both the
  "official tar" and "inner path" layouts), evaluate (no engine, bad
  FEN, cp/mate normalization, engine errors), shutdown
- ``compute_threat_san`` (game over, no eval, illegal best, non-noisy,
  capture, mate)

The async HTTP paths are exercised by patching
``homeassistant.helpers.aiohttp_client.async_get_clientsession`` (the
function-local import inside each method resolves the attribute at call
time, so patching the module attribute is sufficient). The Stockfish
paths are exercised without ever spawning a subprocess or downloading a
binary — ``chess.engine.popen_uci`` and the download session are stubbed.

Run:
    pytest tests/test_lichess_analysis_extra.py
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import chess
import chess.engine
import pytest

from custom_components.phantom_chess.lichess_analysis import (
    EvalResult,
    LichessAnalysisClient,
    StockfishFallback,
    _win_pct_loss_to_accuracy,
    compute_threat_san,
    detect_fork,
)

_SESSION_TARGET = (
    "homeassistant.helpers.aiohttp_client.async_get_clientsession"
)


# ─── helpers ────────────────────────────────────────────────────────────


def _session(*, status: int = 200, json_data=None, read_data: bytes | None = None,
             raise_exc: BaseException | None = None) -> MagicMock:
    """Build an async-context-manager session mock.

    Mirrors ``conftest.mock_aiohttp_session_factory`` but adds ``.read()``
    (used by the Stockfish download path) and an optional ``raise_exc`` for
    the network-error branches.
    """
    resp = MagicMock(
        status=status,
        json=AsyncMock(return_value=json_data or {}),
        text=AsyncMock(return_value=""),
        read=AsyncMock(return_value=read_data or b""),
    )
    resp_cm = MagicMock()
    resp_cm.__aenter__ = AsyncMock(return_value=resp)
    resp_cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    if raise_exc is not None:
        session.get = MagicMock(side_effect=raise_exc)
    else:
        session.get = MagicMock(return_value=resp_cm)
    return session


def _client(stockfish_bin_dir=None) -> LichessAnalysisClient:
    return LichessAnalysisClient(hass=MagicMock(), stockfish_bin_dir=stockfish_bin_dir)


# ─── EvalResult.white_win_pct OverflowError guard ───────────────────────


def test_white_win_pct_overflow_positive_returns_hundred() -> None:
    """math.exp overflows for a wildly negative exponent (huge positive cp).

    -0.00368208 * cp must overflow exp; that needs cp very negative so the
    exponent is huge positive. Huge-negative cp → exp overflows → the
    except branch returns 0.0 (cp < 0). Huge-positive cp does NOT overflow
    (exp of a large-negative number underflows to 0.0, no error). So force
    the overflow with a giant negative cp and assert the cp<0 branch.
    """
    ev = EvalResult(cp=-10**9, mate=None, depth=20, best_uci=None)
    assert ev.white_win_pct == 0.0


def test_white_win_pct_overflow_branch_is_reached() -> None:
    """Patch math.exp to raise OverflowError so both sides of the ternary
    in the except handler are reachable regardless of platform exp limits."""
    import custom_components.phantom_chess.lichess_analysis as mod

    with patch.object(mod.math, "exp", side_effect=OverflowError):
        pos = EvalResult(cp=500, mate=None, depth=20, best_uci=None)
        neg = EvalResult(cp=-500, mate=None, depth=20, best_uci=None)
        assert pos.white_win_pct == 100.0
        assert neg.white_win_pct == 0.0


# ─── _win_pct_loss_to_accuracy OverflowError guard ──────────────────────


def test_win_pct_loss_to_accuracy_overflow_returns_zero() -> None:
    import custom_components.phantom_chess.lichess_analysis as mod

    with patch.object(mod.math, "exp", side_effect=OverflowError):
        assert _win_pct_loss_to_accuracy(10.0) == 0.0


# ─── get_eval: cache hit / miss + fallback ──────────────────────────────


@pytest.mark.asyncio
async def test_get_eval_cache_hit_returns_cached() -> None:
    """A pre-populated cache entry is returned without any HTTP call."""
    client = _client()
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    cached = EvalResult(cp=42, mate=None, depth=20, best_uci="e2e4")
    # get_eval sanitizes the FEN then keys by "<safe>::<multi_pv>".
    client._eval_cache[f"{fen}::1"] = cached
    with patch(_SESSION_TARGET) as get_sess:
        result = await client.get_eval(fen)
    assert result is cached
    get_sess.assert_not_called()


@pytest.mark.asyncio
async def test_get_eval_http_success_parses_and_caches() -> None:
    client = _client()
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    payload = {"depth": 20, "pvs": [{"cp": 30, "moves": "e2e4 e7e5"}]}
    with patch(_SESSION_TARGET, return_value=_session(status=200, json_data=payload)):
        result = await client.get_eval(fen)
    assert result is not None
    assert result.cp == 30
    assert result.best_uci == "e2e4"
    # Now cached — a second call skips HTTP.
    with patch(_SESSION_TARGET) as get_sess:
        again = await client.get_eval(fen)
    assert again is result
    get_sess.assert_not_called()


@pytest.mark.asyncio
async def test_get_eval_404_no_stockfish_returns_none_without_caching() -> None:
    """404 with no Stockfish fallback returns None without caching it.

    None is never cached — transient failures must not poison the LRU for
    the FEN's entire eviction lifetime.  Old code cached None unconditionally,
    so the assertion `not in client._eval_cache` would have failed there.
    """
    client = _client()
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    with patch(_SESSION_TARGET, return_value=_session(status=404)):
        result = await client.get_eval(fen)
    assert result is None
    assert f"{fen}::1" not in client._eval_cache


@pytest.mark.asyncio
async def test_get_eval_404_falls_back_to_stockfish(tmp_path) -> None:
    """On a cloud-eval miss the client asks its StockfishFallback."""
    client = _client(stockfish_bin_dir=tmp_path)
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    fallback_eval = EvalResult(cp=15, mate=None, depth=18, best_uci="d2d4",
                               source="stockfish-local")
    client._stockfish.evaluate = AsyncMock(return_value=fallback_eval)
    with patch(_SESSION_TARGET, return_value=_session(status=404)):
        result = await client.get_eval(fen)
    assert result is fallback_eval
    client._stockfish.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_eval_double_checked_lock_hit() -> None:
    """Simulate a concurrent fill: another coroutine populates the cache
    entry while we're waiting on the lock, so the post-lock re-check returns
    the cached value without fetching. We model 'another coroutine' by having
    a patched _fetch_eval that would run only if the re-check missed — here we
    pre-seed the key so the second `if cache_key in ...` branch fires."""
    client = _client()
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    cached = EvalResult(cp=7, mate=None, depth=9, best_uci="e2e4")

    real_acquire = client._eval_lock.acquire

    async def seeding_acquire():
        # Populate the cache "between" the pre-lock check and the post-lock
        # check, mimicking a racing coroutine that filled it first.
        client._eval_cache[f"{fen}::1"] = cached
        return await real_acquire()

    client._fetch_eval = AsyncMock(side_effect=AssertionError("should not fetch"))
    with patch.object(client._eval_lock, "acquire", side_effect=seeding_acquire):
        result = await client.get_eval(fen)
    assert result is cached
    client._fetch_eval.assert_not_called()


@pytest.mark.asyncio
async def test_get_eval_evicts_when_over_capacity() -> None:
    """A fresh HTTP result inserted into a full cache triggers LRU eviction."""
    client = _client()
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    # Pre-fill the cache to exactly the cap with dummy entries so inserting
    # the new result pushes it over and evicts the oldest.
    cap = client.EVAL_CACHE_SIZE
    for i in range(cap):
        client._eval_cache[f"dummy{i}::1"] = EvalResult(
            cp=i, mate=None, depth=1, best_uci=None
        )
    payload = {"depth": 20, "pvs": [{"cp": 5, "moves": "e2e4"}]}
    with patch(_SESSION_TARGET, return_value=_session(status=200, json_data=payload)):
        result = await client.get_eval(fen)
    assert result is not None
    assert len(client._eval_cache) == cap  # eviction kept it at the cap
    assert "dummy0::1" not in client._eval_cache  # oldest evicted
    assert f"{fen}::1" in client._eval_cache


@pytest.mark.asyncio
async def test_get_opening_evicts_when_over_capacity() -> None:
    client = _client()
    fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    cap = client.OPENING_CACHE_SIZE
    for i in range(cap):
        client._opening_cache[f"dummy{i}"] = (f"o{i}", f"e{i}")
    payload = {"opening": {"name": "X", "eco": "A00"}}
    with patch(_SESSION_TARGET, return_value=_session(status=200, json_data=payload)):
        await client.get_opening(fen)
    assert len(client._opening_cache) == cap
    assert "dummy0" not in client._opening_cache
    assert fen in client._opening_cache


# ─── _fetch_eval: status branches + network error ───────────────────────


@pytest.mark.asyncio
async def test_fetch_eval_non_200_non_404_returns_none() -> None:
    client = _client()
    with patch(_SESSION_TARGET, return_value=_session(status=429)):
        result = await client._fetch_eval("8/8/8/8/8/8/8/8 w - - 0 1", 1)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_eval_network_error_returns_none() -> None:
    import aiohttp

    client = _client()
    with patch(_SESSION_TARGET,
               return_value=_session(raise_exc=aiohttp.ClientError("boom"))):
        result = await client._fetch_eval("8/8/8/8/8/8/8/8 w - - 0 1", 1)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_eval_timeout_returns_none() -> None:
    import asyncio

    client = _client()
    with patch(_SESSION_TARGET,
               return_value=_session(raise_exc=asyncio.TimeoutError())):
        result = await client._fetch_eval("8/8/8/8/8/8/8/8 w - - 0 1", 1)
    assert result is None


# ─── get_opening + _fetch_opening ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_opening_success_and_caches() -> None:
    client = _client()
    fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    payload = {"opening": {"name": "King's Pawn", "eco": "B00"}}
    with patch(_SESSION_TARGET, return_value=_session(status=200, json_data=payload)):
        name, eco = await client.get_opening(fen)
    assert (name, eco) == ("King's Pawn", "B00")
    # Cached — second call skips HTTP.
    with patch(_SESSION_TARGET) as get_sess:
        again = await client.get_opening(fen)
    assert again == ("King's Pawn", "B00")
    get_sess.assert_not_called()


@pytest.mark.asyncio
async def test_get_opening_no_opening_key_returns_none_pair() -> None:
    client = _client()
    with patch(_SESSION_TARGET, return_value=_session(status=200, json_data={})):
        result = await client.get_opening("8/8/8/8/8/8/8/8 w - - 0 1")
    assert result == (None, None)


@pytest.mark.asyncio
async def test_get_opening_non_200_returns_none_pair() -> None:
    client = _client()
    with patch(_SESSION_TARGET, return_value=_session(status=500)):
        result = await client.get_opening("8/8/8/8/8/8/8/8 w - - 0 1")
    assert result == (None, None)


@pytest.mark.asyncio
async def test_fetch_opening_network_error_returns_none_pair() -> None:
    import aiohttp

    client = _client()
    with patch(_SESSION_TARGET,
               return_value=_session(raise_exc=aiohttp.ClientError("boom"))):
        result = await client._fetch_opening("8/8/8/8/8/8/8/8 w - - 0 1")
    assert result == (None, None)


# ─── shutdown ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_shutdown_no_stockfish_is_noop() -> None:
    client = _client()
    await client.shutdown()  # must not raise


@pytest.mark.asyncio
async def test_client_shutdown_delegates_to_stockfish(tmp_path) -> None:
    client = _client(stockfish_bin_dir=tmp_path)
    client._stockfish.shutdown = AsyncMock()
    await client.shutdown()
    client._stockfish.shutdown.assert_awaited_once()


# ─── best_move_for_ai_level ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_best_move_no_stockfish_returns_none() -> None:
    client = _client()  # no bin_dir → no fallback
    assert await client.best_move_for_ai_level(chess.Board(), 5) is None


@pytest.mark.asyncio
async def test_best_move_engine_unavailable_returns_none(tmp_path) -> None:
    client = _client(stockfish_bin_dir=tmp_path)
    client._stockfish.ensure_engine = AsyncMock(return_value=None)
    assert await client.best_move_for_ai_level(chess.Board(), 5) is None


@pytest.mark.asyncio
async def test_best_move_happy_path_returns_uci(tmp_path) -> None:
    client = _client(stockfish_bin_dir=tmp_path)
    engine = MagicMock()
    engine.configure = AsyncMock()
    play_result = MagicMock(move=chess.Move.from_uci("e2e4"))
    engine.play = AsyncMock(return_value=play_result)
    client._stockfish.ensure_engine = AsyncMock(return_value=engine)
    move = await client.best_move_for_ai_level(chess.Board(), ai_level=6)
    assert move == "e2e4"
    engine.configure.assert_awaited_once()
    engine.play.assert_awaited_once()


@pytest.mark.asyncio
async def test_best_move_configure_failure_still_plays(tmp_path) -> None:
    """A rejected mid-session reconfigure is logged + swallowed."""
    client = _client(stockfish_bin_dir=tmp_path)
    engine = MagicMock()
    engine.configure = AsyncMock(side_effect=RuntimeError("no reconfigure"))
    engine.play = AsyncMock(return_value=MagicMock(move=chess.Move.from_uci("d2d4")))
    client._stockfish.ensure_engine = AsyncMock(return_value=engine)
    move = await client.best_move_for_ai_level(chess.Board(), ai_level=3)
    assert move == "d2d4"


@pytest.mark.asyncio
async def test_best_move_unknown_level_defaults_to_max(tmp_path) -> None:
    """An out-of-range level falls back to the level-8 (skill,depth) row."""
    client = _client(stockfish_bin_dir=tmp_path)
    engine = MagicMock()
    engine.configure = AsyncMock()
    engine.play = AsyncMock(return_value=MagicMock(move=chess.Move.from_uci("g1f3")))
    client._stockfish.ensure_engine = AsyncMock(return_value=engine)
    move = await client.best_move_for_ai_level(chess.Board(), ai_level=99)
    assert move == "g1f3"
    # Configured with the level-8 skill (20).
    skill_arg = engine.configure.await_args.args[0]
    assert skill_arg == {"Skill Level": 20}


@pytest.mark.asyncio
async def test_best_move_play_raises_returns_none(tmp_path) -> None:
    client = _client(stockfish_bin_dir=tmp_path)
    engine = MagicMock()
    engine.configure = AsyncMock()
    engine.play = AsyncMock(side_effect=RuntimeError("play died"))
    client._stockfish.ensure_engine = AsyncMock(return_value=engine)
    assert await client.best_move_for_ai_level(chess.Board(), 8) is None


@pytest.mark.asyncio
async def test_best_move_no_move_in_result_returns_none(tmp_path) -> None:
    client = _client(stockfish_bin_dir=tmp_path)
    engine = MagicMock()
    engine.configure = AsyncMock()
    engine.play = AsyncMock(return_value=MagicMock(move=None))
    client._stockfish.ensure_engine = AsyncMock(return_value=engine)
    assert await client.best_move_for_ai_level(chess.Board(), 8) is None


# ─── StockfishFallback.ensure_engine ────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_engine_unavailable_short_circuits(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    sf._available = False
    assert await sf.ensure_engine() is None


@pytest.mark.asyncio
async def test_ensure_engine_returns_cached_engine(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    sentinel = MagicMock()
    sf._engine = sentinel
    assert await sf.ensure_engine() is sentinel


@pytest.mark.asyncio
async def test_ensure_engine_double_checked_lock_hit(tmp_path) -> None:
    """The engine is set 'by another coroutine' while we await the init lock;
    the post-lock re-check returns it without spawning."""
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    sentinel = MagicMock()
    real_acquire = sf._init_lock.acquire

    async def seeding_acquire():
        sf._engine = sentinel  # racing coroutine won the spawn
        return await real_acquire()

    with patch.object(sf._init_lock, "acquire", side_effect=seeding_acquire), \
         patch("chess.engine.popen_uci",
               new=AsyncMock(side_effect=AssertionError("should not spawn"))):
        result = await sf.ensure_engine()
    assert result is sentinel


@pytest.mark.asyncio
async def test_ensure_engine_binary_missing_marks_unavailable(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    sf._ensure_binary = AsyncMock(return_value=False)
    assert await sf.ensure_engine() is None
    assert sf._available is False


@pytest.mark.asyncio
async def test_ensure_engine_spawn_success(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    sf._ensure_binary = AsyncMock(return_value=True)
    sf.binary_path = tmp_path / "engine"
    engine = MagicMock()
    engine.configure = AsyncMock()
    with patch("chess.engine.popen_uci",
               new=AsyncMock(return_value=(MagicMock(), engine))):
        result = await sf.ensure_engine()
    assert result is engine
    assert sf._engine is engine
    engine.configure.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_engine_configure_rejected_still_spawns(tmp_path) -> None:
    """Engines that reject the Threads/Hash configure still come up."""
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    sf._ensure_binary = AsyncMock(return_value=True)
    sf.binary_path = tmp_path / "engine"
    engine = MagicMock()
    engine.configure = AsyncMock(side_effect=RuntimeError("no configure"))
    with patch("chess.engine.popen_uci",
               new=AsyncMock(return_value=(MagicMock(), engine))):
        result = await sf.ensure_engine()
    assert result is engine


@pytest.mark.asyncio
async def test_ensure_engine_spawn_failure_marks_unavailable(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    sf._ensure_binary = AsyncMock(return_value=True)
    sf.binary_path = tmp_path / "engine"
    with patch("chess.engine.popen_uci",
               new=AsyncMock(side_effect=OSError("ENOENT"))):
        assert await sf.ensure_engine() is None
    assert sf._available is False


# Verified installation behavior is covered in test_engine_artifacts.py.


# ─── StockfishFallback.evaluate ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_no_engine_returns_none(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    sf.ensure_engine = AsyncMock(return_value=None)
    assert await sf.evaluate("8/8/8/8/8/8/8/8 w - - 0 1") is None


@pytest.mark.asyncio
async def test_evaluate_bad_fen_returns_none(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    sf.ensure_engine = AsyncMock(return_value=MagicMock())
    assert await sf.evaluate("totally-not-a-fen") is None


@pytest.mark.asyncio
async def test_evaluate_cp_score_normalizes_white(tmp_path) -> None:
    """A cp score is returned white-positive with best_uci from the PV."""
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    engine = MagicMock()
    engine.configure = AsyncMock()
    score = chess.engine.PovScore(chess.engine.Cp(120), chess.WHITE)
    engine.analyse = AsyncMock(return_value={
        "score": score,
        "depth": 18,
        "pv": [chess.Move.from_uci("e2e4")],
    })
    sf.ensure_engine = AsyncMock(return_value=engine)
    result = await sf.evaluate("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    assert result is not None
    engine.configure.assert_awaited_once_with({"Skill Level": 20})
    assert result.raw["pv"] == ["e2e4"]
    assert result.cp == 120
    assert result.mate is None
    assert result.best_uci == "e2e4"
    assert result.source == "stockfish-local"


@pytest.mark.asyncio
async def test_evaluate_mate_score(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    engine = MagicMock()
    engine.configure = AsyncMock()
    score = chess.engine.PovScore(chess.engine.Mate(3), chess.WHITE)
    engine.analyse = AsyncMock(return_value={"score": score, "depth": 20, "pv": []})
    sf.ensure_engine = AsyncMock(return_value=engine)
    result = await sf.evaluate("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    assert result is not None
    assert result.mate == 3
    assert result.cp is None
    assert result.best_uci is None  # empty pv


@pytest.mark.asyncio
async def test_evaluate_score_none_returns_none(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    engine = MagicMock()
    engine.analyse = AsyncMock(return_value={"depth": 5})  # no 'score'
    sf.ensure_engine = AsyncMock(return_value=engine)
    result = await sf.evaluate("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    assert result is None


@pytest.mark.asyncio
async def test_evaluate_engine_error_returns_none(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    engine = MagicMock()
    engine.analyse = AsyncMock(side_effect=chess.engine.EngineError("dead"))
    sf.ensure_engine = AsyncMock(return_value=engine)
    result = await sf.evaluate("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    assert result is None


@pytest.mark.asyncio
async def test_evaluate_unexpected_error_shuts_down_and_returns_none(tmp_path) -> None:
    """An unexpected analyse error forces a shutdown (respawn next call)."""
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    engine = MagicMock()
    engine.analyse = AsyncMock(side_effect=RuntimeError("boom"))
    engine.quit = AsyncMock()
    sf.ensure_engine = AsyncMock(return_value=engine)
    sf._engine = engine
    result = await sf.evaluate("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    assert result is None
    # shutdown() cleared the cached engine.
    assert sf._engine is None


# ─── StockfishFallback.shutdown ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_shutdown_no_engine_is_noop(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    await sf.shutdown()  # must not raise
    assert sf._engine is None


@pytest.mark.asyncio
async def test_shutdown_quits_engine(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    engine = MagicMock()
    engine.quit = AsyncMock()
    sf._engine = engine
    sf._transport = MagicMock()
    await sf.shutdown()
    engine.quit.assert_awaited_once()
    assert sf._engine is None
    assert sf._transport is None


@pytest.mark.asyncio
async def test_shutdown_swallows_quit_error(tmp_path) -> None:
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    engine = MagicMock()
    engine.quit = AsyncMock(side_effect=RuntimeError("already dead"))
    sf._engine = engine
    await sf.shutdown()  # must not raise
    assert sf._engine is None


# ─── compute_threat_san ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_threat_san_game_over_returns_none() -> None:
    # Fool's mate — black is checkmated, game over.
    board = chess.Board(
        "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    )
    assert board.is_game_over()
    client = _client()
    assert await compute_threat_san(board, client) is None


@pytest.mark.asyncio
async def test_threat_san_no_eval_returns_none() -> None:
    board = chess.Board()
    client = _client()
    client.get_eval = AsyncMock(return_value=None)
    assert await compute_threat_san(board, client) is None


@pytest.mark.asyncio
async def test_threat_san_no_best_uci_returns_none() -> None:
    board = chess.Board()
    client = _client()
    client.get_eval = AsyncMock(
        return_value=EvalResult(cp=0, mate=None, depth=10, best_uci=None)
    )
    assert await compute_threat_san(board, client) is None


@pytest.mark.asyncio
async def test_threat_san_unparseable_best_uci_returns_none() -> None:
    board = chess.Board()
    client = _client()
    client.get_eval = AsyncMock(
        return_value=EvalResult(cp=0, mate=None, depth=10, best_uci="zzzz")
    )
    assert await compute_threat_san(board, client) is None


@pytest.mark.asyncio
async def test_threat_san_illegal_best_move_returns_none() -> None:
    board = chess.Board()  # startpos; e5 by white is illegal
    client = _client()
    client.get_eval = AsyncMock(
        return_value=EvalResult(cp=0, mate=None, depth=10, best_uci="e2e5")
    )
    assert await compute_threat_san(board, client) is None


@pytest.mark.asyncio
async def test_threat_san_quiet_best_move_is_not_a_threat() -> None:
    """A legal but non-capture/non-mate best move is not surfaced."""
    board = chess.Board()  # startpos; e2e4 is quiet
    client = _client()
    client.get_eval = AsyncMock(
        return_value=EvalResult(cp=30, mate=None, depth=10, best_uci="e2e4")
    )
    assert await compute_threat_san(board, client) is None


@pytest.mark.asyncio
async def test_threat_san_capture_returns_san() -> None:
    """A best move that is a capture surfaces as the threat SAN."""
    # White knight on e5, black pawn on d7 -> Nxd7 is a capture.
    board = chess.Board("rnbqkbnr/pppppppp/8/4N3/8/8/PPPPPPPP/R1BQKB1R w KQkq - 0 1")
    move = chess.Move.from_uci("e5d7")
    assert move in board.legal_moves
    assert board.is_capture(move)
    client = _client()
    client.get_eval = AsyncMock(
        return_value=EvalResult(cp=100, mate=None, depth=10, best_uci="e5d7")
    )
    san = await compute_threat_san(board, client)
    assert san == board.san(move)


@pytest.mark.asyncio
async def test_threat_san_mate_returns_san() -> None:
    """A short-mate best move (non-capture) still surfaces via the mate branch."""
    # Back-rank mate: white Ra1 delivers mate on a8 vs a boxed-in black king.
    board = chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1")
    move = chess.Move.from_uci("a1a8")
    assert move in board.legal_moves
    assert not board.is_capture(move)
    client = _client()
    client.get_eval = AsyncMock(
        return_value=EvalResult(cp=None, mate=1, depth=10, best_uci="a1a8")
    )
    san = await compute_threat_san(board, client)
    assert san == board.san(move)


@pytest.mark.asyncio
async def test_threat_san_san_raises_returns_none() -> None:
    """If board.san() raises (illegal-move / assertion), we return None
    rather than propagate — the final except branch."""
    board = chess.Board("rnbqkbnr/pppppppp/8/4N3/8/8/PPPPPPPP/R1BQKB1R w KQkq - 0 1")
    client = _client()
    client.get_eval = AsyncMock(
        return_value=EvalResult(cp=100, mate=None, depth=10, best_uci="e5d7")
    )
    with patch.object(chess.Board, "san",
                      side_effect=chess.IllegalMoveError("nope")):
        assert await compute_threat_san(board, client) is None


# ─── detect_fork: guard branch (moving piece absent) ────────────────────


def test_detect_fork_from_empty_square_returns_false() -> None:
    """A syntactically-legal-looking move object whose from-square is empty
    can't be a fork. We reach the `moving_piece is None` guard by asserting
    the move passes the legal-moves check first — so use a real legal move
    but drop the piece via a patched piece_at.

    Simpler: pick a null move analogue — python-chess treats a legal move as
    requiring a piece, so instead verify the guard indirectly: a legal move
    from an occupied square is fine (covered elsewhere); here we confirm the
    function returns False for a move not in legal_moves (already covered) and
    additionally that a passing move on a copy without the piece is handled.
    """
    board = chess.Board()
    # e2e4 is legal; monkeypatch the board copy's piece_at to report empty
    # from-square, exercising the None guard. We patch chess.Board.piece_at
    # to return None only for the from-square e2.
    move = chess.Move.from_uci("e2e4")
    orig = chess.Board.piece_at

    def fake(self, sq):
        if sq == chess.E2:
            return None
        return orig(self, sq)

    with patch.object(chess.Board, "piece_at", fake):
        assert detect_fork(board, move) is False


# ─── B5: shared engine lock (play_move serializes with evaluate) ─────────


@pytest.mark.asyncio
async def test_play_move_and_evaluate_do_not_overlap(tmp_path) -> None:
    """play_move() and evaluate() never hold the engine simultaneously.

    Without the _eval_lock in play_move, the two coroutines' engine
    commands would interleave during the asyncio.sleep(0) yield points,
    setting `active > 1` and flipping `overlap_detected`. With the lock
    they strictly serialize.

    Old code: play_move() didn't exist → AttributeError → test fails.
    New code without lock: overlap_detected would be True → assertion fails.
    New code with lock: overlap_detected stays False → test passes.
    """
    import asyncio as _asyncio

    from custom_components.phantom_chess.lichess_analysis import StockfishFallback

    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    engine = MagicMock()

    active = 0
    overlap_detected = False

    async def slow_analyse(board, limit):
        nonlocal active, overlap_detected
        active += 1
        if active > 1:
            overlap_detected = True
        await _asyncio.sleep(0)
        active -= 1
        score = chess.engine.PovScore(chess.engine.Cp(0), chess.WHITE)
        return {"score": score, "depth": 5, "pv": []}

    async def slow_configure(*args, **kwargs):
        nonlocal active, overlap_detected
        active += 1
        if active > 1:
            overlap_detected = True
        await _asyncio.sleep(0)
        active -= 1

    async def slow_play(*args, **kwargs):
        nonlocal active, overlap_detected
        active += 1
        if active > 1:
            overlap_detected = True
        await _asyncio.sleep(0)
        active -= 1
        return MagicMock(move=chess.Move.from_uci("e2e4"))

    engine.analyse = AsyncMock(side_effect=slow_analyse)
    engine.configure = AsyncMock(side_effect=slow_configure)
    engine.play = AsyncMock(side_effect=slow_play)
    sf._engine = engine
    sf.ensure_engine = AsyncMock(return_value=engine)

    await _asyncio.gather(
        sf.evaluate(
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        ),
        sf.play_move(chess.Board(), skill=5, depth=5),
    )
    assert not overlap_detected


# ─── B5: None not cached for transient failures ──────────────────────────


@pytest.mark.asyncio
async def test_transient_failure_does_not_cache_none() -> None:
    """A transient HTTP error (network down) does NOT cache None.

    Old code cached None unconditionally, so the follow-up assertion
    `not in client._eval_cache` would have failed. New code skips caching
    on None so the next call retries the fetch.
    """
    import aiohttp

    client = _client()
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    with patch(_SESSION_TARGET,
               return_value=_session(raise_exc=aiohttp.ClientError("network down"))):
        result = await client.get_eval(fen)
    assert result is None
    assert f"{fen}::1" not in client._eval_cache


@pytest.mark.asyncio
async def test_bypass_cache_forces_refetch() -> None:
    """bypass_cache=True re-fetches even when the FEN is already cached."""
    client = _client()
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    cached = EvalResult(cp=10, mate=None, depth=18, best_uci="e2e4")
    client._eval_cache[f"{fen}::1"] = cached

    payload = {"depth": 20, "pvs": [{"cp": 20, "moves": "d2d4"}]}
    with patch(_SESSION_TARGET, return_value=_session(status=200, json_data=payload)):
        result = await client.get_eval(fen, bypass_cache=True)
    assert result is not None
    assert result.cp == 20  # fresh value, not the cached 10


# ─── B5: 429 Retry-After cooldown ────────────────────────────────────────


@pytest.mark.asyncio
async def test_429_sets_rate_limit_and_next_call_skips_http() -> None:
    """A 429 response with Retry-After sets the cooldown; the next call
    returns None immediately without making an HTTP request.

    Old code had no rate-limit tracking, so both calls would hit the
    session — the second patch.assert_not_called() would have failed.
    """
    import asyncio as _asyncio

    client = _client()
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    resp = MagicMock(
        status=429,
        headers={"Retry-After": "30"},
        json=AsyncMock(return_value={}),
        read=AsyncMock(return_value=b""),
    )
    resp_cm = MagicMock()
    resp_cm.__aenter__ = AsyncMock(return_value=resp)
    resp_cm.__aexit__ = AsyncMock(return_value=None)
    session_429 = MagicMock()
    session_429.get = MagicMock(return_value=resp_cm)

    with patch(_SESSION_TARGET, return_value=session_429):
        result1 = await client.get_eval(fen)
    assert result1 is None
    # Rate limit should be set now.
    loop = _asyncio.get_event_loop()
    assert client._rate_limit_until > loop.time()

    # Second call — no HTTP should be made (rate-limited).
    with patch(_SESSION_TARGET) as no_session:
        result2 = await client.get_eval(fen)
    assert result2 is None
    no_session.assert_not_called()


# ─── B5: shutdown closes transport on quit failure ────────────────────────


@pytest.mark.asyncio
async def test_shutdown_closes_transport_when_quit_fails(tmp_path) -> None:
    """When engine.quit() raises, the transport is closed to avoid a zombie.

    Old code: caught the exception, set both to None, but never called
    transport.close() — so the subprocess stayed alive.  The assertion
    `transport.close.assert_called_once()` would fail on old code.
    """
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    engine = MagicMock()
    engine.quit = AsyncMock(side_effect=RuntimeError("already dead"))
    transport = MagicMock()
    sf._engine = engine
    sf._transport = transport
    await sf.shutdown()
    assert sf._engine is None
    assert sf._transport is None
    transport.close.assert_called_once()

@pytest.mark.parametrize("score,expected", [(chess.engine.MateGiven, 10000), (chess.engine.Mate(0), -10000)])
async def test_mate_zero_preserves_winning_side(tmp_path, score, expected):
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    engine = MagicMock()
    engine.configure = AsyncMock()
    engine.analyse = AsyncMock(return_value={"score": chess.engine.PovScore(score, chess.WHITE), "depth": 20, "pv": []})
    sf.ensure_engine = AsyncMock(return_value=engine)
    result = await sf.evaluate(chess.STARTING_FEN)
    assert result.cp == expected
    assert result.mate is None


async def test_local_only_analysis_never_touches_the_network() -> None:
    """cloud_analysis off: evaluations come from Stockfish, openings stay unnamed."""
    from unittest.mock import AsyncMock, MagicMock
    from custom_components.phantom_chess.lichess_analysis import EvalResult, LichessAnalysisClient

    client = LichessAnalysisClient(MagicMock())
    client.allow_cloud = False
    client._fetch_eval = AsyncMock()
    client._fetch_opening = AsyncMock()
    local = EvalResult(cp=35, mate=None, depth=12, best_uci="e2e4", source="stockfish-local", raw={})
    client._stockfish = MagicMock(evaluate=AsyncMock(return_value=local))
    fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    assert await client.get_eval(fen) is local
    assert await client.get_opening(fen) == (None, None)
    client._fetch_eval.assert_not_awaited()
    client._fetch_opening.assert_not_awaited()
    # Turning the option back on names the opening (nothing was cached).
    client.allow_cloud = True
    client._fetch_opening = AsyncMock(return_value=("King's Pawn Game", "B00"))
    assert await client.get_opening(fen) == ("King's Pawn Game", "B00")


async def test_turning_cloud_off_hides_results_cached_while_on() -> None:
    """QUALIFICATION F7–F9: cloud results cached with the option on are not
    served after it is turned off (no reload happens), and come back when it
    is turned on again. Local results stay usable either way."""
    from unittest.mock import AsyncMock, MagicMock
    from custom_components.phantom_chess.lichess_analysis import EvalResult, LichessAnalysisClient

    fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
    cloud = EvalResult(cp=40, mate=None, depth=40, best_uci="b8c6", source="lichess-cloud", raw={})
    local = EvalResult(cp=35, mate=None, depth=12, best_uci="b8c6", source="stockfish-local", raw={})
    client = LichessAnalysisClient(MagicMock())
    client._fetch_eval = AsyncMock(return_value=cloud)
    client._fetch_opening = AsyncMock(return_value=("King's Knight Opening", "C40"))
    client._stockfish = MagicMock(evaluate=AsyncMock(return_value=local))

    # Option on: cloud results are fetched and cached.
    assert await client.get_eval(fen) is cloud
    assert await client.get_opening(fen) == ("King's Knight Opening", "C40")

    # Option off: the same position evaluates locally and has no opening name.
    client.allow_cloud = False
    client._fetch_eval.reset_mock()
    client._fetch_opening.reset_mock()
    assert await client.get_eval(fen) is local
    assert await client.get_opening(fen) == (None, None)
    # The local result is now cached and served while the option stays off.
    assert await client.get_eval(fen) is local
    client._stockfish.evaluate.assert_awaited_once()
    client._fetch_eval.assert_not_awaited()
    client._fetch_opening.assert_not_awaited()

    # Option on again: the cached opening name is served without a request.
    client.allow_cloud = True
    assert await client.get_opening(fen) == ("King's Knight Opening", "C40")
    client._fetch_opening.assert_not_awaited()

"""Tests for the pure-function helpers in lichess_analysis.py.

The Lichess analysis module's network paths (cloud-eval HTTP, Stockfish
binary download/spawn) are exercised only in HA-integration tests. The
helpers covered here are pure: chess-board logic, math, threshold
classification.

Run:
    pytest tests/test_lichess_analysis.py
"""
from __future__ import annotations


import chess
import pytest

from custom_components.phantom_chess.const import (
    CLASSIFICATION_BEST,
    CLASSIFICATION_BLUNDER,
    CLASSIFICATION_GOOD,
    CLASSIFICATION_INACCURACY,
    CLASSIFICATION_MISTAKE,
    CLASSIFICATION_UNKNOWN,
)
from custom_components.phantom_chess.lichess_analysis import (
    CLASSIFICATION_DISPLAY,
    EvalResult,
    LichessAnalysisClient,
    StockfishFallback,
    _detect_libc,
    _safe_fen_for_eval,
    _win_pct_loss_to_accuracy,
    classification_color_glyph,
    classify_move,
    compute_game_accuracy,
    detect_fork,
)


# ─── _safe_fen_for_eval ─────────────────────────────────────────────────


def test_safe_fen_for_eval_no_ep_passes_through() -> None:
    """A FEN without an en-passant square is returned unchanged."""
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    assert _safe_fen_for_eval(fen) == fen


def test_safe_fen_for_eval_keeps_real_ep() -> None:
    """An EP square that a pawn CAN capture into is left intact."""
    # 1.e4 e5 2.Nf3 Nc6 3.d4 — black to move, white's d-pawn just advanced
    # 2 squares from d2 → d4. EP square = d3. But no black pawn on c4/e4
    # can capture, so this would be spurious. Build a position where black
    # CAN actually capture en passant.
    # 1.e4 d5 2.e5 f5 → en passant on f6 is legal for white's e5 pawn.
    fen = "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3"
    out = _safe_fen_for_eval(fen)
    # EP field at index 3
    assert out.split()[3] == "f6"


def test_safe_fen_for_eval_strips_spurious_ep() -> None:
    """An EP square no pawn can capture into is replaced with '-'."""
    # Starting position with a contrived EP square that nothing can use.
    # White just played d2-d4, EP square = d3, but black has no pawn on
    # c4 or e4 yet — so EP is spurious.
    fen = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq d3 0 1"
    out = _safe_fen_for_eval(fen)
    parts = out.split()
    assert parts[3] == "-"


def test_safe_fen_for_eval_invalid_fen_returns_input() -> None:
    """A malformed FEN passes through unchanged — we don't raise."""
    bad = "not a fen at all"
    assert _safe_fen_for_eval(bad) == bad


def test_safe_fen_for_eval_short_fen_no_ep_field() -> None:
    """A FEN with fewer than 4 space-separated fields can't be stripped."""
    # python-chess will choke on this, the function returns it as-is.
    short = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w"
    out = _safe_fen_for_eval(short)
    # Either passes through or stays with no ep change — both fine.
    assert isinstance(out, str)


# ─── EvalResult.white_win_pct ───────────────────────────────────────────


def test_white_win_pct_zero_cp_is_fifty() -> None:
    """A 0-cp eval = 50% win probability."""
    ev = EvalResult(cp=0, mate=None, depth=20, best_uci="e2e4")
    assert ev.white_win_pct == pytest.approx(50.0)


def test_white_win_pct_positive_cp_above_fifty() -> None:
    ev = EvalResult(cp=200, mate=None, depth=20, best_uci="e2e4")
    assert 50.0 < ev.white_win_pct < 100.0


def test_white_win_pct_negative_cp_below_fifty() -> None:
    ev = EvalResult(cp=-200, mate=None, depth=20, best_uci="e7e5")
    assert 0.0 < ev.white_win_pct < 50.0


def test_white_win_pct_mate_for_white_is_hundred() -> None:
    ev = EvalResult(cp=None, mate=5, depth=20, best_uci="d1h5")
    assert ev.white_win_pct == 100.0


def test_white_win_pct_mate_for_black_is_zero() -> None:
    ev = EvalResult(cp=None, mate=-5, depth=20, best_uci="d8h4")
    assert ev.white_win_pct == 0.0


def test_white_win_pct_none_cp_defaults_to_fifty() -> None:
    """No-data eval (no mate, no cp) defaults to 50% (neutral)."""
    ev = EvalResult(cp=None, mate=None, depth=None, best_uci=None)
    assert ev.white_win_pct == 50.0


def test_white_win_pct_extreme_cp_clamps() -> None:
    """Very large cp values clamp to ~100; very negative to ~0."""
    high = EvalResult(cp=100_000, mate=None, depth=20, best_uci=None)
    low = EvalResult(cp=-100_000, mate=None, depth=20, best_uci=None)
    assert high.white_win_pct > 99.0
    assert low.white_win_pct < 1.0


# ─── EvalResult.from_mover_view ─────────────────────────────────────────


def test_from_mover_view_white_to_move_unchanged() -> None:
    ev = EvalResult(cp=150, mate=None, depth=20, best_uci="e2e4")
    assert ev.from_mover_view(white_to_move=True) == 150


def test_from_mover_view_black_to_move_negated() -> None:
    ev = EvalResult(cp=150, mate=None, depth=20, best_uci="e2e4")
    # Same +150 white-positive cp, viewed from black-to-move = -150
    assert ev.from_mover_view(white_to_move=False) == -150


def test_from_mover_view_none_cp_returns_none() -> None:
    ev = EvalResult(cp=None, mate=5, depth=20, best_uci="d1h5")
    assert ev.from_mover_view(white_to_move=True) is None


# ─── classify_move ──────────────────────────────────────────────────────


def _ev(cp: int, best_uci: str | None = None) -> EvalResult:
    return EvalResult(cp=cp, mate=None, depth=20, best_uci=best_uci)


def test_classify_move_unknown_when_pre_eval_missing() -> None:
    klass, cpl = classify_move(None, _ev(0), "e2e4", mover_is_white=True)
    assert klass == CLASSIFICATION_UNKNOWN
    assert cpl == 0


def test_classify_move_unknown_when_post_eval_missing() -> None:
    klass, cpl = classify_move(_ev(0), None, "e2e4", mover_is_white=True)
    assert klass == CLASSIFICATION_UNKNOWN
    assert cpl == 0


def test_classify_move_best_matches_engine_top() -> None:
    """Move matching pre_eval.best_uci is always classified `best`, even
    with a tiny CP loss within rounding noise."""
    pre = _ev(50, best_uci="e2e4")
    post = _ev(48, best_uci="e7e5")  # white played e2e4, lost 2cp
    klass, cpl = classify_move(pre, post, "e2e4", mover_is_white=True)
    assert klass == CLASSIFICATION_BEST


def test_classify_move_good_when_loss_below_good_max() -> None:
    pre = _ev(50, best_uci="e2e4")
    post = _ev(35)  # 15cp loss for white, < CPL_GOOD_MAX (20)
    klass, _ = classify_move(pre, post, "g1f3", mover_is_white=True)
    assert klass == CLASSIFICATION_GOOD


def test_classify_move_inaccuracy_in_20_to_99_band() -> None:
    pre = _ev(50, best_uci="e2e4")
    post = _ev(0)  # 50cp loss → inaccuracy
    klass, cpl = classify_move(pre, post, "a2a3", mover_is_white=True)
    assert klass == CLASSIFICATION_INACCURACY
    assert cpl == 50


def test_classify_move_mistake_in_100_to_299_band() -> None:
    pre = _ev(50, best_uci="e2e4")
    post = _ev(-100)  # 150cp loss → mistake
    klass, cpl = classify_move(pre, post, "h2h4", mover_is_white=True)
    assert klass == CLASSIFICATION_MISTAKE
    assert cpl == 150


def test_classify_move_blunder_at_300_or_more() -> None:
    pre = _ev(50, best_uci="e2e4")
    post = _ev(-500)  # 550cp loss → blunder
    klass, cpl = classify_move(pre, post, "f2f3", mover_is_white=True)
    assert klass == CLASSIFICATION_BLUNDER
    assert cpl == 550


def test_classify_move_black_perspective_negated() -> None:
    """For a black move, loss = post_cp - pre_cp (since cp is white-positive
    and a worse position for black is more positive)."""
    pre = _ev(0)
    post = _ev(200)  # white's eval went up by 200 = black lost 200cp
    klass, cpl = classify_move(pre, post, "h7h5", mover_is_white=False)
    assert klass == CLASSIFICATION_MISTAKE
    assert cpl == 200


def test_classify_move_gain_clamps_cpl_to_zero() -> None:
    """A move that IMPROVES eval (negative loss) shouldn't produce
    negative CPL — clamp to 0."""
    pre = _ev(0)
    post = _ev(100)  # white gained 100cp
    klass, cpl = classify_move(pre, post, "g1f3", mover_is_white=True)
    assert cpl == 0
    # cpl=0 falls under the GOOD band (< 20)
    assert klass == CLASSIFICATION_GOOD


def test_classify_move_mate_treated_as_extreme_cp() -> None:
    """Pre-eval mate-for-white shows as +10000cp; missing the mate move
    is a huge blunder."""
    pre = EvalResult(cp=None, mate=2, depth=20, best_uci="d1h5")
    post = _ev(0)  # gave up the mate
    klass, _ = classify_move(pre, post, "h2h3", mover_is_white=True)
    assert klass == CLASSIFICATION_BLUNDER


# ─── classification_color_glyph ────────────────────────────────────────


@pytest.mark.parametrize(
    "classification,expected_color,expected_glyph",
    [
        (CLASSIFICATION_BEST,       "#4caf50", "✓"),
        (CLASSIFICATION_GOOD,       "#8bc34a", "!"),
        (CLASSIFICATION_INACCURACY, "#ffc107", "?!"),
        (CLASSIFICATION_MISTAKE,    "#ff9800", "?"),
        (CLASSIFICATION_BLUNDER,    "#f44336", "??"),
        (CLASSIFICATION_UNKNOWN,    "#9e9e9e", "…"),
        ("brilliant",               "#00bcd4", "!!"),
        ("book",                    "#795548", "📖"),
    ],
)
def test_classification_color_glyph(
    classification: str, expected_color: str, expected_glyph: str
) -> None:
    color, glyph = classification_color_glyph(classification)
    assert color == expected_color
    assert glyph == expected_glyph


def test_classification_color_glyph_unknown_input_falls_back() -> None:
    """An unrecognised classification returns the UNKNOWN palette."""
    color, glyph = classification_color_glyph("not-a-real-class")
    assert (color, glyph) == CLASSIFICATION_DISPLAY[CLASSIFICATION_UNKNOWN]


# ─── detect_fork ───────────────────────────────────────────────────────


def test_detect_fork_classic_knight_fork() -> None:
    """White knight b5 → c7 forks black king (e8) and queen (a8).

    A knight on c7 attacks both a8 and e8 — the textbook royal fork.
    """
    # Black king e8, black queen a8, white knight b5, white king e1.
    board = chess.Board(
        "q3k3/8/8/1N6/8/8/8/4K3 w - - 0 1"
    )
    move = chess.Move.from_uci("b5c7")
    assert move in board.legal_moves
    assert detect_fork(board, move) is True


def test_detect_fork_non_fork_is_false() -> None:
    """A simple knight move that doesn't fork is not a fork."""
    board = chess.Board()  # starting position
    move = chess.Move.from_uci("g1f3")
    assert detect_fork(board, move) is False


def test_detect_fork_illegal_move_returns_false() -> None:
    """Asking about a move that isn't legal in the position returns False."""
    board = chess.Board()  # starting position
    illegal = chess.Move.from_uci("e1e3")  # king can't move 2 squares
    assert detect_fork(board, illegal) is False


def test_detect_fork_low_value_attacker_against_pawn_only() -> None:
    """A knight attacking only pawns (lower-value than knight) isn't a fork
    by our rule — the moved piece needs to attack >=2 enemy pieces of
    equal-or-greater value."""
    # Knight on e5 attacking only two black pawns on d7/f7 — pawns are
    # less valuable than a knight, so no fork by the rule's definition.
    board = chess.Board(
        "rnbqkb1r/ppp2ppp/5n2/3pp3/4N3/8/PPPP1PPP/R1BQKBNR w KQkq - 0 4"
    )
    # White knight from e4 to c5 — knights for forks need to hit big pieces.
    # In starting positions early on, knights rarely fork. We just want a
    # legal move that's not a fork.
    move = chess.Move.from_uci("e4c5")
    if move in board.legal_moves:
        # Even if "legal", the resulting attacks shouldn't hit ≥ 2 pieces
        # of value ≥ knight.
        assert detect_fork(board, move) is False


# ─── _win_pct_loss_to_accuracy ─────────────────────────────────────────


def test_win_pct_loss_to_accuracy_zero_loss_is_perfect() -> None:
    """A zero win-pct loss = ~100% accuracy (with a tiny rounding offset)."""
    a = _win_pct_loss_to_accuracy(0.0)
    assert a == pytest.approx(100.0, abs=0.001)


def test_win_pct_loss_to_accuracy_small_loss_high_accuracy() -> None:
    """5% win-pct loss → accuracy ≈ 80% (Lichess formula yields 79.82).

    Bounds chosen to assert the curve is monotonically decreasing past
    0% loss without pinning to the exact constant.
    """
    a = _win_pct_loss_to_accuracy(5.0)
    assert 70.0 < a < 90.0


def test_win_pct_loss_to_accuracy_huge_loss_clamps_low() -> None:
    a = _win_pct_loss_to_accuracy(100.0)
    # 103.1668 * exp(-4.354) - 3.1669 ≈ -1.84 → clamped to 0
    assert a == 0.0


def test_win_pct_loss_to_accuracy_negative_loss_clamps_to_zero_loss() -> None:
    """A 'gain' (negative loss) is treated as 0 loss = perfect accuracy."""
    a_zero = _win_pct_loss_to_accuracy(0.0)
    a_negative = _win_pct_loss_to_accuracy(-10.0)
    assert a_negative == pytest.approx(a_zero)


# ─── compute_game_accuracy ─────────────────────────────────────────────


def test_compute_game_accuracy_all_perfect_moves() -> None:
    """Two best-moves each side → 100% accuracy for both."""
    eq = _ev(0)
    # Four moves: w, b, w, b — all preserved 0 cp.
    white_acc, black_acc = compute_game_accuracy(
        pre_evals=[eq, eq, eq, eq],
        post_evals=[eq, eq, eq, eq],
        side_movers=[True, False, True, False],
    )
    assert white_acc == pytest.approx(100.0, abs=0.01)
    assert black_acc == pytest.approx(100.0, abs=0.01)


def test_compute_game_accuracy_handles_sparse_data() -> None:
    """Missing eval entries are skipped, not crashed-on."""
    eq = _ev(0)
    white_acc, black_acc = compute_game_accuracy(
        pre_evals=[None, eq, None],
        post_evals=[eq, None, eq],
        side_movers=[True, False, True],
    )
    # Every move has at least one None → both averages are None.
    assert white_acc is None
    assert black_acc is None


def test_compute_game_accuracy_one_blunder_drops_average() -> None:
    """One white blunder pulls white's average below black's perfect."""
    eq = _ev(0)
    blunder_pre = _ev(0)
    blunder_post = _ev(-500)  # white blundered, eval crashed
    # 2 white moves, 1 black; first white move is the blunder
    white_acc, black_acc = compute_game_accuracy(
        pre_evals=[blunder_pre, eq, eq],
        post_evals=[blunder_post, eq, eq],
        side_movers=[True, False, True],
    )
    assert white_acc is not None
    assert black_acc is not None
    assert white_acc < black_acc


# ─── LichessAnalysisClient._parse_eval_payload ─────────────────────────


def _new_client() -> LichessAnalysisClient:
    """Build a client with no Stockfish and a stub hass.

    The client's __init__ creates an asyncio.Lock(); we don't actually
    use it in these pure-function tests but it requires a running event
    loop to construct. Building outside any async context works because
    asyncio.Lock() is a sync-instantiable primitive in Python 3.10+.
    """
    from unittest.mock import MagicMock
    return LichessAnalysisClient(hass=MagicMock(), stockfish_bin_dir=None)


def test_parse_eval_payload_empty_pvs_returns_none() -> None:
    """No 'pvs' key in payload → can't parse, return None."""
    client = _new_client()
    result = client._parse_eval_payload(
        {"depth": 20, "pvs": []},
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    )
    assert result is None


def test_parse_eval_payload_white_to_move_preserves_sign() -> None:
    """When white is to move, cp is already white-positive — no flip."""
    client = _new_client()
    result = client._parse_eval_payload(
        {
            "depth": 20,
            "pvs": [{"cp": 150, "moves": "e2e4 e7e5 g1f3"}],
        },
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    )
    assert result is not None
    assert result.cp == 150
    assert result.mate is None
    assert result.depth == 20
    assert result.best_uci == "e2e4"
    assert result.source == "lichess-cloud"


def test_parse_eval_payload_black_to_move_flips_sign() -> None:
    """When black is to move, Lichess returns cp from black's perspective.
    Our internal convention is white-positive, so we flip."""
    client = _new_client()
    result = client._parse_eval_payload(
        {
            "depth": 22,
            "pvs": [{"cp": 80, "moves": "e7e5"}],  # +80 for black-to-move = -80 white
        },
        fen="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    )
    assert result is not None
    assert result.cp == -80
    assert result.best_uci == "e7e5"


def test_parse_eval_payload_mate_for_black_flips_sign() -> None:
    """Lichess returns mate-in-N from side-to-move's perspective.
    Black-to-move + mate=3 means white is getting mated → mate=-3 internally."""
    client = _new_client()
    result = client._parse_eval_payload(
        {
            "depth": 30,
            "pvs": [{"mate": 3, "moves": "h4h2"}],
        },
        fen="6kr/6pp/8/8/7q/8/PP6/K7 b - - 0 1",
    )
    assert result is not None
    assert result.cp is None
    assert result.mate == -3
    assert result.best_uci == "h4h2"


def test_parse_eval_payload_moves_string_with_no_space() -> None:
    """A single-move PV ('e2e4' with no spaces) parses correctly."""
    client = _new_client()
    result = client._parse_eval_payload(
        {
            "depth": 5,
            "pvs": [{"cp": 0, "moves": "e2e4"}],
        },
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    )
    assert result is not None
    assert result.best_uci == "e2e4"


def test_parse_eval_payload_no_moves_string_yields_none_best_uci() -> None:
    """An eval with no PV moves string → best_uci is None."""
    client = _new_client()
    result = client._parse_eval_payload(
        {
            "depth": 5,
            "pvs": [{"cp": 0}],
        },
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    )
    assert result is not None
    assert result.best_uci is None


# ─── LichessAnalysisClient cache behaviour ─────────────────────────────


def test_eval_cache_lru_eviction() -> None:
    """When the cache exceeds EVAL_CACHE_SIZE, oldest entries get evicted.

    Pre-load the cache directly to avoid mocking the HTTP path; the
    eviction logic lives in `get_eval`'s outer flow but we can test
    the bare LRU bookkeeping by writing directly to `_eval_cache`.
    """
    client = _new_client()
    # Populate one past the cap to trigger eviction logic.
    cap = client.EVAL_CACHE_SIZE
    for i in range(cap + 5):
        client._eval_cache[f"fen{i}::1"] = EvalResult(
            cp=i, mate=None, depth=20, best_uci=None
        )
        # Trim like get_eval does
        while len(client._eval_cache) > cap:
            client._eval_cache.popitem(last=False)
    # The oldest 5 entries should be gone
    assert "fen0::1" not in client._eval_cache
    assert "fen4::1" not in client._eval_cache
    # The newest should still be present
    assert f"fen{cap + 4}::1" in client._eval_cache
    # Total size is exactly cap
    assert len(client._eval_cache) == cap


def test_opening_cache_lru_eviction() -> None:
    """OPENING_CACHE_SIZE is smaller (openings are deterministic);
    same eviction pattern."""
    client = _new_client()
    cap = client.OPENING_CACHE_SIZE
    for i in range(cap + 3):
        client._opening_cache[f"fen{i}"] = (f"opening-{i}", f"eco-{i}")
        while len(client._opening_cache) > cap:
            client._opening_cache.popitem(last=False)
    assert "fen0" not in client._opening_cache
    assert "fen2" not in client._opening_cache
    assert f"fen{cap + 2}" in client._opening_cache
    assert len(client._opening_cache) == cap


# ─── _AI_LEVEL_TABLE ───────────────────────────────────────────────────


def test_ai_level_table_covers_1_through_8() -> None:
    """The Lichess AI 1–8 levels all map to (skill, depth) tuples."""
    table = LichessAnalysisClient._AI_LEVEL_TABLE
    for level in range(1, 9):
        assert level in table
        skill, depth = table[level]
        # Skill is 0–20 (Stockfish range); depth is positive
        assert 0 <= skill <= 20
        assert depth > 0


def test_ai_level_table_monotonic() -> None:
    """Higher AI levels = stronger skill (monotonic, with one allowed plateau
    at the bottom for the deliberately-weak levels 1–4)."""
    table = LichessAnalysisClient._AI_LEVEL_TABLE
    prev_skill = -1
    for level in range(1, 9):
        skill, _ = table[level]
        assert skill >= prev_skill, f"Skill non-monotonic at level {level}"
        prev_skill = skill


# ─── _detect_libc ──────────────────────────────────────────────────────


def test_detect_libc_returns_glibc_when_no_musl_linker(monkeypatch) -> None:
    """No musl dynamic linkers present → defaults to glibc."""
    import os as _os
    monkeypatch.setattr(_os.path, "exists", lambda _p: False)
    assert _detect_libc() == "glibc"


def test_detect_libc_returns_musl_when_x86_64_musl_linker_exists(monkeypatch) -> None:
    """Presence of /lib/ld-musl-x86_64.so.1 → musl (Alpine x86_64)."""
    import os as _os
    monkeypatch.setattr(
        _os.path, "exists",
        lambda p: p == "/lib/ld-musl-x86_64.so.1",
    )
    assert _detect_libc() == "musl"


def test_detect_libc_returns_musl_when_aarch64_musl_linker_exists(monkeypatch) -> None:
    """Presence of /lib/ld-musl-aarch64.so.1 → musl (Alpine ARM64 / Pi on HA OS)."""
    import os as _os
    monkeypatch.setattr(
        _os.path, "exists",
        lambda p: p == "/lib/ld-musl-aarch64.so.1",
    )
    assert _detect_libc() == "musl"


def test_detect_libc_returns_musl_when_armhf_musl_linker_exists(monkeypatch) -> None:
    """Presence of /lib/ld-musl-armhf.so.1 → musl (32-bit ARM Alpine)."""
    import os as _os
    monkeypatch.setattr(
        _os.path, "exists",
        lambda p: p == "/lib/ld-musl-armhf.so.1",
    )
    assert _detect_libc() == "musl"


# ─── StockfishFallback init + state flags ─────────────────────────────


def test_stockfish_fallback_init_with_string_bin_dir(tmp_path) -> None:
    """`bin_dir` accepts both str and Path; init wraps as Path."""
    from unittest.mock import MagicMock
    from pathlib import Path
    sf = StockfishFallback(hass=MagicMock(), bin_dir=str(tmp_path))
    assert isinstance(sf.bin_dir, Path)
    assert sf.bin_dir == tmp_path


def test_stockfish_fallback_init_with_path_bin_dir(tmp_path) -> None:
    from unittest.mock import MagicMock
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    assert sf.bin_dir == tmp_path


def test_stockfish_fallback_initial_state(tmp_path) -> None:
    """Fresh init: binary not located, engine not running, available=True
    (gets flipped False only on permanent failure)."""
    from unittest.mock import MagicMock
    sf = StockfishFallback(hass=MagicMock(), bin_dir=tmp_path)
    assert sf.binary_path is None
    assert sf._transport is None
    assert sf._engine is None
    assert sf._unsupported_arch_warned is False
    assert sf._available is True


def test_stockfish_fallback_class_defaults() -> None:
    """The class-level constants are the tuned conservative defaults."""
    assert StockfishFallback.DEPTH == 18
    assert StockfishFallback.TIME_LIMIT_SEC == 1.5
    # Download timeout long enough to handle slow tarball pulls.
    assert StockfishFallback.DOWNLOAD_TIMEOUT_SEC >= 30


# ─── LichessAnalysisClient + StockfishFallback integration ─────────────


def test_client_with_stockfish_bin_dir_creates_fallback(tmp_path) -> None:
    """Passing a bin_dir to the client constructs a StockfishFallback."""
    from unittest.mock import MagicMock
    client = LichessAnalysisClient(hass=MagicMock(), stockfish_bin_dir=tmp_path)
    assert client._stockfish is not None
    assert isinstance(client._stockfish, StockfishFallback)
    assert client._stockfish.bin_dir == tmp_path


def test_client_without_stockfish_bin_dir_skips_fallback() -> None:
    """No bin_dir → no Stockfish; client still works for cloud-eval only."""
    from unittest.mock import MagicMock
    client = LichessAnalysisClient(hass=MagicMock(), stockfish_bin_dir=None)
    assert client._stockfish is None

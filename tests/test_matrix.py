"""Tests for the pure-function matrix.py module.

These are the cheapest and highest-value tests in the integration —
matrix.py is pure (no HA, no BLE, no async) so the tests run instantly
and have zero scaffolding cost.

Run:
    pytest custom_components/phantom_chess/tests/test_matrix.py
"""
from __future__ import annotations

import pytest

from custom_components.phantom_chess.matrix import (
    PIECE_NAMES,
    build_matrix_from_fen,
    check_consistency,
    diff_grid_vs_sensor,
    format_mismatch_instructions,
    grid_index_to_square,
    grid_to_fen,
    parse_matrix_notification,
)


# ─── grid_index_to_square ────────────────────────────────────────────────


def test_grid_index_to_square_corners() -> None:
    # a8 is at col=1, row=1 → idx 11
    assert grid_index_to_square(11) == "a8"
    # h1 is at col=8, row=8 → idx 88
    assert grid_index_to_square(88) == "h1"
    # e4 is at col=5 (file 'e'), row=5 (rank '4') → idx 55
    assert grid_index_to_square(55) == "e4"


def test_grid_index_to_square_gutter_returns_none() -> None:
    # Top-left corner (col 0, row 0)
    assert grid_index_to_square(0) is None
    # Right gutter column (col 9)
    assert grid_index_to_square(90) is None
    assert grid_index_to_square(99) is None
    # Top-row gutter
    assert grid_index_to_square(10) is None  # col 1, row 0


# ─── build_matrix_from_fen ↔ grid_to_fen round-trip ─────────────────────


STARTING_FEN_BOARD = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"


def test_build_matrix_from_fen_length() -> None:
    matrix = build_matrix_from_fen(STARTING_FEN_BOARD)
    assert len(matrix) == 100


def test_build_matrix_from_fen_gutters_clean() -> None:
    """Cols 0 and 9 and rows 0 and 9 must all be '.' on a fresh board."""
    matrix = build_matrix_from_fen(STARTING_FEN_BOARD)
    # Left + right gutter columns (idx 0-9 and 90-99)
    assert matrix[0:10] == "." * 10
    assert matrix[90:100] == "." * 10
    # Top and bottom gutter rows on each file column
    for col_start in range(10, 90, 10):
        assert matrix[col_start] == ".", f"top gutter not clean at col idx {col_start}"
        assert matrix[col_start + 9] == ".", f"bot gutter not clean at col idx {col_start + 9}"


def test_build_matrix_round_trips_starting_fen() -> None:
    matrix = build_matrix_from_fen(STARTING_FEN_BOARD)
    back = grid_to_fen(matrix)
    assert back == STARTING_FEN_BOARD


def test_build_matrix_round_trips_complex_position() -> None:
    """Sicilian Najdorf middlegame — non-trivial piece distribution."""
    fen = "r1bq1rk1/pp2bppp/2np1n2/4p3/4P3/2NP1N2/PPPBBPPP/R2Q1RK1"
    matrix = build_matrix_from_fen(fen)
    back = grid_to_fen(matrix)
    assert back == fen


def test_build_matrix_accepts_full_fen() -> None:
    """build_matrix_from_fen should accept full FEN (with turn/castling fields)."""
    full = STARTING_FEN_BOARD + " w KQkq - 0 1"
    matrix = build_matrix_from_fen(full)
    assert grid_to_fen(matrix) == STARTING_FEN_BOARD


def test_build_matrix_rejects_malformed_fen() -> None:
    with pytest.raises(ValueError):
        build_matrix_from_fen("only/seven/ranks/here/not/eight/total")  # 7 ranks


def test_grid_to_fen_rejects_wrong_length() -> None:
    assert grid_to_fen("too short") is None
    assert grid_to_fen("." * 99) is None
    assert grid_to_fen("." * 101) is None


# ─── parse_matrix_notification ──────────────────────────────────────────


def _synth_payload(prefix: str, grid: str, bitmap: str) -> bytes:
    return f"{prefix},{grid},{bitmap}".encode()


def test_parse_matrix_notification_clean() -> None:
    grid = build_matrix_from_fen(STARTING_FEN_BOARD)
    bitmap = "0" + ("0" * 8 + "0") + ("1" * 10) * 2 + ("0" * 10) * 4 + ("1" * 10) * 2 + "0" * 10
    # Bitmap is just any 100-char 0/1 string for this test
    bitmap = "0" * 100
    payload = _synth_payload("CLEAN: Match.", grid, bitmap)
    parsed = parse_matrix_notification(payload)
    assert parsed is not None
    assert parsed["status"] == "Clean"
    assert parsed["piece_grid"] == grid
    assert parsed["sensor_bitmap"] == bitmap


def test_parse_matrix_notification_error() -> None:
    grid = build_matrix_from_fen(STARTING_FEN_BOARD)
    bitmap = "1" * 100
    payload = _synth_payload(
        "ERROR: Chessboard and sensor matrix do not match.", grid, bitmap
    )
    parsed = parse_matrix_notification(payload)
    assert parsed is not None
    assert parsed["status"] == "Error"
    assert "do not match" in parsed["status_message"]


def test_parse_matrix_notification_accepts_promoted_pawn_markers() -> None:
    """fw0.3.2 doc §9.2: 'X'/'Z' mark promoted pawns. The grid must still
    parse (occupancy diffing + piece counting work on opaque markers);
    previously the whole payload was rejected right after any promotion."""
    grid = build_matrix_from_fen(STARTING_FEN_BOARD)
    # replace white e2 pawn's slot with a promoted-pawn marker
    assert "P" in grid
    grid_x = grid.replace("P", "X", 1)
    grid_z = grid.replace("p", "Z", 1)
    for g in (grid_x, grid_z):
        payload = _synth_payload("CLEAN: Match.", g, "0" * 100)
        parsed = parse_matrix_notification(payload)
        assert parsed is not None
        assert parsed["piece_grid"] == g
        # markers count as occupied squares
        assert sum(1 for c in parsed["piece_grid"] if c != ".") == 32


def test_grid_to_fen_refuses_promoted_pawn_markers() -> None:
    """'X'/'Z' have no documented side mapping (doc §9.2 'in some contexts'),
    so FEN reconstruction returns None rather than emitting an invalid FEN.
    The coordinator keeps its last-known-good live_fen in that case."""
    grid = build_matrix_from_fen(STARTING_FEN_BOARD)
    assert grid_to_fen(grid) == STARTING_FEN_BOARD  # sanity: normal grid works
    assert grid_to_fen(grid.replace("P", "X", 1)) is None
    assert grid_to_fen(grid.replace("p", "Z", 1)) is None


def test_diff_grid_vs_sensor_treats_marker_as_occupied() -> None:
    grid = build_matrix_from_fen(STARTING_FEN_BOARD).replace("P", "X", 1)
    # sensor bitmap: everything empty → every occupied grid square is "missing"
    diffs = diff_grid_vs_sensor(grid, "0" * 100)
    assert len(diffs) == 32
    # the marker square reports the generic fallback name, not a crash
    marker_diffs = [d for d in diffs if d["piece"] == "piece"]
    assert len(marker_diffs) == 1


def test_parse_matrix_notification_rejects_garbage() -> None:
    assert parse_matrix_notification(b"random garbage") is None
    # CLEAN prefix but no grid/bitmap
    assert parse_matrix_notification(b"CLEAN: Match.,,") is None


def test_parse_matrix_notification_surfaces_error_without_grid() -> None:
    """The 'matrix do not match' wedge sometimes arrives with no usable
    trailing grid. We must still surface status='Error' + the message
    (piece_grid/sensor_bitmap None) so the coordinator can report it,
    rather than dropping the whole payload (v0.4-beta3, C2)."""
    parsed = parse_matrix_notification(
        b"ERROR: Chessboard and sensor matrix do not match."
    )
    assert parsed is not None
    assert parsed["status"] == "Error"
    assert parsed["piece_grid"] is None
    assert parsed["sensor_bitmap"] is None
    assert "do not match" in (parsed["status_message"] or "")
    # A non-error payload with no grid is still discarded.
    assert parse_matrix_notification(b"Managing Mismatch.,,") is None


# ─── check_consistency ──────────────────────────────────────────────────


def test_check_consistency_clean() -> None:
    grid = build_matrix_from_fen(STARTING_FEN_BOARD)
    # Build a matching bitmap: 1 where grid has piece, 0 where empty.
    bitmap = "".join("1" if c != "." else "0" for c in grid)
    ok, count = check_consistency(grid, bitmap)
    assert ok is True
    assert count == 0


def test_check_consistency_detects_mismatch() -> None:
    grid = build_matrix_from_fen(STARTING_FEN_BOARD)
    # Lie: bitmap claims everything is empty.
    bitmap = "0" * 100
    ok, count = check_consistency(grid, bitmap)
    assert ok is False
    assert count == 32  # 32 pieces on a fresh board


def test_check_consistency_rejects_bad_lengths() -> None:
    ok, count = check_consistency("short", "0" * 100)
    assert ok is False
    assert count == -1


# ─── diff_grid_vs_sensor + format_mismatch_instructions ────────────────


def test_diff_grid_vs_sensor_missing_pawn() -> None:
    # Build a starting-position grid, then claim e2 is empty.
    grid_list = list(build_matrix_from_fen(STARTING_FEN_BOARD))
    # Confirm e2 has a pawn in the starting position.
    # e2 = col 5, row 7 → idx 57
    assert grid_list[57] == "P"
    # Make bitmap match grid everywhere EXCEPT e2 where we say empty.
    bitmap = ["1" if c != "." else "0" for c in grid_list]
    bitmap[57] = "0"  # claim e2 empty
    diffs = diff_grid_vs_sensor("".join(grid_list), "".join(bitmap))
    assert len(diffs) == 1
    assert diffs[0]["square"] == "e2"
    assert diffs[0]["type"] == "missing"
    assert diffs[0]["piece"] == "white pawn"


def test_diff_grid_vs_sensor_extra_piece() -> None:
    grid = build_matrix_from_fen(STARTING_FEN_BOARD)
    bitmap_list = ["1" if c != "." else "0" for c in grid]
    # Claim there's a piece on a stray square (e4 is empty in starting position).
    # e4 = col 5, row 5 → idx 55
    bitmap_list[55] = "1"
    diffs = diff_grid_vs_sensor(grid, "".join(bitmap_list))
    assert len(diffs) == 1
    assert diffs[0]["square"] == "e4"
    assert diffs[0]["type"] == "extra"


def test_format_mismatch_instructions_single_missing() -> None:
    diffs = [{"square": "e2", "type": "missing", "piece": "white pawn"}]
    msg = format_mismatch_instructions(diffs)
    assert "e2" in msg
    assert "white pawn" in msg
    assert "Missing" in msg


def test_format_mismatch_instructions_multiple() -> None:
    diffs = [
        {"square": "e2", "type": "missing", "piece": "white pawn"},
        {"square": "d2", "type": "missing", "piece": "white pawn"},
        {"square": "f4", "type": "extra", "piece": ""},
    ]
    msg = format_mismatch_instructions(diffs)
    assert "Missing pieces" in msg
    assert "e2" in msg
    assert "d2" in msg
    assert "Extra piece" in msg
    assert "f4" in msg


def test_format_mismatch_instructions_empty() -> None:
    msg = format_mismatch_instructions([])
    assert "sensors disagree" in msg.lower() or "sensors" in msg


# ─── PIECE_NAMES coverage ───────────────────────────────────────────────


def test_piece_names_covers_all_chess_pieces() -> None:
    # All 12 piece letters (white + black, 6 piece types each)
    expected_letters = set("KQRBNPkqrbnp")
    assert set(PIECE_NAMES.keys()) == expected_letters
    # Every value follows "color piece-type" pattern
    for letter, name in PIECE_NAMES.items():
        assert " " in name, f"{letter} → {name}: expected 'color piece' format"
        color, _ = name.split(" ", 1)
        assert color in ("white", "black")

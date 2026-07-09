"""Pure functions for the Phantom Chess Board's 10×10 column-major matrix.

The firmware operates on a 100-char "wire" matrix:
  - 10 columns × 10 cells each, column-major layout
  - Cols 0 and 9: side gutters (graveyard for captured pieces)
  - Cols 1-8: files a-h
  - Rows 0 and 9: top/bottom gutters
  - Rows 1-8: ranks 8-1 (rank 8 at top of file column)

This module holds all the stateless functions that convert between this
matrix layout, standard FEN, sensor bitmaps, and human-readable
square coordinates. Extracted from coordinator.py 2026-05-16 as the
first step of the Task #21 coordinator split — these functions never
needed to be on a class.

Anyone using this module:
  from .matrix import (
      build_matrix_from_fen,           # FEN → 100-char wire
      grid_to_fen,                     # 100-char wire → FEN
      parse_matrix_notification,       # raw BLE bytes → parsed dict
      check_consistency,               # (grid, bitmap) → (ok, count)
      diff_grid_vs_sensor,             # (grid, bitmap) → [{square, type, piece}]
      format_mismatch_instructions,    # diffs → markdown text
      grid_index_to_square,            # idx 0-99 → "e4" or None
  )
"""
from __future__ import annotations


# ─── Piece-letter → human name ──────────────────────────────────────────
# Used by diff_grid_vs_sensor when generating user notifications about
# missing/extra pieces. Keep in sync with the FEN piece letters that
# parse_matrix_notification accepts in the piece_grid regex.

PIECE_NAMES: dict[str, str] = {
    "K": "white king",  "Q": "white queen",  "R": "white rook",
    "B": "white bishop", "N": "white knight", "P": "white pawn",
    "k": "black king",  "q": "black queen",  "r": "black rook",
    "b": "black bishop", "n": "black knight", "p": "black pawn",
}


# ─── Coordinate conversion ──────────────────────────────────────────────


def grid_index_to_square(idx: int) -> str | None:
    """Convert a position in the 100-char column-major grid to algebraic.

    Returns None for gutter positions (cols 0/9 or rows 0/9).

    Example: grid_index_to_square(14) → "a5" (idx 14 → col=1, row=4 →
    file 'a', rank str(9-4)='5').
    """
    col, row = divmod(idx, 10)
    if not (1 <= col <= 8 and 1 <= row <= 8):
        return None
    file_letter = chr(ord("a") + col - 1)  # col 1 → 'a'
    rank_digit = str(9 - row)               # row 1 → '8', row 8 → '1'
    return file_letter + rank_digit


# ─── FEN ↔ matrix conversion ────────────────────────────────────────────


def build_matrix_from_fen(fen: str) -> str:
    """Convert a position FEN to the Phantom 100-char column-major matrix.

    Accepts either a board-only FEN (8 rank groups) or a full FEN —
    trailing fields (turn, castling, etc.) are stripped.

    Wire layout:
      - Cols 0 and 9: side gutters (all '.')
      - Cols 1-8: files a-h
      - Within each file column: pos 0 = top gutter, pos 1 = rank 8,
        … pos 8 = rank 1, pos 9 = bottom gutter.

    Raises ValueError on malformed FEN.
    """
    board_fen = fen.split(" ")[0]
    ranks = board_fen.split("/")
    if len(ranks) != 8:
        raise ValueError(f"Expected 8 ranks in FEN, got {len(ranks)}: {fen}")

    # board[file_idx][rank_idx]: file 0..7 = a..h, rank_idx 0..7 = rank 8..1
    board = [["."] * 8 for _ in range(8)]
    for rank_idx, rank_str in enumerate(ranks):
        file_idx = 0
        for ch in rank_str:
            if ch.isdigit():
                file_idx += int(ch)
            else:
                if file_idx >= 8:
                    raise ValueError(f"FEN rank overflow: {rank_str!r}")
                board[file_idx][rank_idx] = ch
                file_idx += 1
        if file_idx != 8:
            raise ValueError(f"FEN rank wrong width: {rank_str!r} → {file_idx}")

    cols = ["." * 10]  # left gutter column
    for file_idx in range(8):
        col = "."
        for rank_idx in range(8):
            col += board[file_idx][rank_idx]
        col += "."
        cols.append(col)
    cols.append("." * 10)  # right gutter column
    wire = "".join(cols)
    assert len(wire) == 100, f"matrix is {len(wire)} chars, expected 100"
    return wire


def grid_to_fen(grid: str) -> str | None:
    """Convert a 100-char column-major piece grid to a board-only FEN.

    Returns None if the input is malformed (wrong length).

    The transposition: input grid has columns [gutter, file_a, ..., file_h, gutter];
    each non-gutter column holds [gutter, rank_8, rank_7, ..., rank_1, gutter].
    FEN ranks are listed rank 8 → rank 1 (left to right), with file a → file h
    within each rank.

    Grids containing the fw0.3.2 promoted-pawn markers 'X'/'Z' (doc §9.2)
    also return None: the doc doesn't define which side each marker maps to,
    and emitting them verbatim would produce an invalid FEN. Callers keep
    their last-known-good FEN instead.
    """
    if len(grid) != 100:
        return None
    if "X" in grid or "Z" in grid:
        return None
    rows = [grid[i*10:(i+1)*10] for i in range(10)]
    # playing[file_idx][rank_idx]: file 0=a..7=h, rank_idx 0=rank8..7=rank1
    playing = [r[1:9] for r in rows[1:9]]
    fen_ranks = []
    for rank_idx in range(8):
        encoded = ""
        empty = 0
        for file_idx in range(8):
            ch = playing[file_idx][rank_idx]
            if ch == ".":
                empty += 1
            else:
                if empty:
                    encoded += str(empty)
                    empty = 0
                encoded += ch
        if empty:
            encoded += str(empty)
        fen_ranks.append(encoded)
    return "/".join(fen_ranks)


# ─── BLE matrix-notification parsing ────────────────────────────────────


def parse_matrix_notification(data: bytes) -> dict[str, str | None] | None:
    """Parse a UUID_SEND_MATRIX notification or read value.

    The firmware emits two distinct payload shapes on this channel:
      - "CLEAN: Match.,<grid>,<bitmap>" — normal state (expected = sensed)
      - "ERROR: <reason>.,<grid>,<bitmap>" — mismatch detected
        (e.g. "ERROR: Chessboard and sensor matrix do not match")
    Both end with a 100-char piece grid and a 100-char binary bitmap
    separated by commas. We accept both shapes and surface the prefix
    as `status`.

    Returns None for malformed/empty input or a CLEAN payload with no
    parseable grid. On success: dict with keys raw, piece_grid,
    sensor_bitmap, status, status_message. NOTE: for an ERROR/OTHER payload
    that carries no usable trailing matrix, piece_grid and sensor_bitmap are
    None (status/status_message are still populated) — callers MUST guard
    for the None grid case.
    """
    try:
        decoded = data.decode("utf-8", errors="replace").strip()
    except Exception:
        return None

    if decoded.startswith("CLEAN:"):
        status = "Clean"
    elif decoded.startswith("ERROR:"):
        status = "Error"
    else:
        # Accept other prefixes that embed matrix data — used during
        # Managing Mismatch and other modes where firmware may emit
        # non-standard messages with the same trailing layout.
        status = "Other"

    parts = decoded.split(",")
    # 'X'/'Z' are the fw0.3.2 promoted-pawn markers (EFRAIN_GAMEPLAY_DOC
    # 2026-06-09 §9.2: "Special markers: 'X', 'Z' for promoted pawns in some
    # contexts"). The doc does NOT define which side each maps to, so they are
    # accepted as opaque occupied-square markers only — occupancy diffing,
    # piece counting, and status all work; FEN reconstruction refuses to
    # guess (see grid_to_fen). Previously grids containing them were rejected
    # wholesale, blinding matrix state right after any pawn promotion.
    grid = next(
        (p for p in parts if len(p) == 100 and all(c in ".PNBRQKpnbrqkXZ" for c in p)),
        None,
    )
    bitmap = next(
        (p for p in parts if len(p) == 100 and all(c in "01" for c in p)),
        None,
    )

    head = decoded.split(",", 1)[0]
    if ":" in head:
        message = head.split(":", 1)[1].strip().rstrip(".").strip()
    else:
        message = ""

    if grid is None or bitmap is None:
        # No usable trailing matrix. Previously the WHOLE payload was
        # dropped here — which silently hid the firmware's
        # "ERROR: Chessboard and sensor matrix do not match." wedge, because
        # that error sometimes arrives with no (or a malformed) trailing
        # grid. Still surface a genuine ERROR/OTHER status so the coordinator
        # can set matrix_status and the user sees what's wrong; grid/bitmap
        # are None and consumers must guard for that. A bare "CLEAN" with no
        # grid is meaningless and is still discarded.
        if status == "Error":
            return {
                "raw": decoded,
                "piece_grid": None,
                "sensor_bitmap": None,
                "status": status,
                "status_message": message,
            }
        return None

    return {
        "raw": decoded,
        "piece_grid": grid,
        "sensor_bitmap": bitmap,
        "status": status,
        "status_message": message,
    }


# ─── Sensor consistency + diff ──────────────────────────────────────────


def check_consistency(grid: str, bitmap: str) -> tuple[bool, int]:
    """Return (consistent, mismatch_count).

    consistent=True if every '1' in bitmap corresponds to a non-'.' in
    grid AND every '0' corresponds to '.'. mismatch_count is the number
    of cells that disagree. Returns (False, -1) on malformed input.
    """
    if len(grid) != 100 or len(bitmap) != 100:
        return False, -1
    mismatches = 0
    for g, b in zip(grid, bitmap):
        cell_occupied_by_grid = g != "."
        cell_occupied_by_sensor = b == "1"
        if cell_occupied_by_grid != cell_occupied_by_sensor:
            mismatches += 1
    return mismatches == 0, mismatches


def diff_grid_vs_sensor(grid: str, bitmap: str) -> list[dict[str, str]]:
    """Return a list of disagreements between expected grid and sensor bitmap.

    Each entry: {"square": "e4", "type": "missing"|"extra", "piece": "white pawn" | ""}
      - "missing": grid expects a piece here but sensor says empty (user took
        a piece off, or it never arrived during a magnet move).
      - "extra": sensor sees a piece here but grid expects empty (stray
        piece placed where firmware doesn't expect one).
    """
    diffs: list[dict[str, str]] = []
    if len(grid) != 100 or len(bitmap) != 100:
        return diffs
    for idx, (g, b) in enumerate(zip(grid, bitmap)):
        square = grid_index_to_square(idx)
        if square is None:
            continue  # skip gutters
        grid_occupied = g != "."
        sensor_occupied = b == "1"
        if grid_occupied and not sensor_occupied:
            diffs.append({
                "square": square,
                "type": "missing",
                "piece": PIECE_NAMES.get(g, "piece"),
            })
        elif sensor_occupied and not grid_occupied:
            diffs.append({
                "square": square,
                "type": "extra",
                "piece": "",
            })
    return diffs


def format_mismatch_instructions(diffs: list[dict[str, str]]) -> str:
    """Turn a diff list into a human-readable Markdown block.

    Groups missing-piece vs extra-piece cases so the user has clear
    actionable instructions. Falls back to a generic message if the
    diff list is empty (shouldn't happen, but defensive).
    """
    if not diffs:
        return "The board sensors disagree with the expected position."

    missing = [d for d in diffs if d["type"] == "missing"]
    extra = [d for d in diffs if d["type"] == "extra"]

    lines: list[str] = []
    if missing:
        if len(missing) == 1:
            d = missing[0]
            lines.append(f"- **Missing**: a {d['piece']} on **{d['square']}**.")
        else:
            lines.append(f"- **Missing pieces** ({len(missing)}):")
            for d in missing:
                lines.append(f"  - {d['piece']} expected on **{d['square']}**")
    if extra:
        if len(extra) == 1:
            d = extra[0]
            lines.append(
                f"- **Extra piece** detected on **{d['square']}** — "
                "please remove or move it."
            )
        else:
            lines.append(f"- **Extra pieces** detected ({len(extra)}):")
            for d in extra:
                lines.append(f"  - unexpected piece on **{d['square']}**")
    return "\n".join(lines)

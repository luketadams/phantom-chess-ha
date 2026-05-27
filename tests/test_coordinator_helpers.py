"""Tests for the pure-function helpers in coordinator.py.

The coordinator module is mostly async BLE/HA infrastructure that needs
the full HA test loop. These tests cover only the pure-function pieces
at the module top + the staticmethods inside ``PhantomChessCoordinator``:

- ``_phantom_to_uci`` — parses firmware move-notation strings to UCI.
- ``_rotate_uci_180`` — applies the rank-mirror + from-to swap that
  firmware 0.3.0 applies to black-piece sensor reports.
- ``PhantomChessCoordinator._coarse_accuracy`` — CPL → accuracy heuristic.
- ``PhantomChessCoordinator._describe_mistake`` — post-game review text.

Run:
    pytest tests/test_coordinator_helpers.py
"""
from __future__ import annotations

import pytest

from custom_components.phantom_chess.coordinator import (
    PhantomChessCoordinator,
    _phantom_to_uci,
    _rotate_uci_180,
)


# ─── _phantom_to_uci ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Firmware 0.1.6 / 0.3.0 indexed format
        ("M 1 e2-e4", "e2e4"),
        ("M 2 e7-e5", "e7e5"),
        # Capture (the 'x' separator)
        ("M 1 d5xe4", "d5e4"),
        # Castling — king target square is enough
        ("M 1 e1-g1", "e1g1"),
        # Older firmware variant without index
        ("M e2-e4", "e2e4"),
        # Weird whitespace / formatting tolerance
        ("M 1 a7-a5", "a7a5"),
        ("M 1 h2-h4", "h2h4"),
    ],
)
def test_phantom_to_uci_normal_moves(raw: str, expected: str) -> None:
    assert _phantom_to_uci(raw) == expected


def test_phantom_to_uci_extracts_first_token_when_extra_text() -> None:
    """The parser uses a regex to find the first '<sq>[-x]<sq>' pair,
    tolerating noisy framing the firmware may add in future versions."""
    # Synthetic edge case — leading noise stripped.
    assert _phantom_to_uci("CLEAN: e2-e4 ok") == "e2e4"


def test_phantom_to_uci_no_match_fallback() -> None:
    """A string with no square-pair just strips '-'/'x' and the 'M' prefix."""
    # Doesn't match the regex; falls back to the legacy strip logic.
    out = _phantom_to_uci("M abcdef")
    # Legacy fallback strips dashes/x; returns whatever's left.
    assert "-" not in out
    assert "x" not in out


# ─── _rotate_uci_180 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "uci,expected",
    [
        # Validated 2026-05-10: firmware reports black e7→e5 as "e4-e2"
        ("e4e2", "e7e5"),
        # The transform is an involution — applying twice returns the input.
        ("e7e5", "e4e2"),
        # Corner-to-corner: a1→h8 rotates to h1a8 (rank-mirror + from-to swap).
        ("a1h8", "h1a8"),
        ("a8h1", "h8a1"),
        # White move h2→h4 rotates to h5h7 (used for AI-echo set membership).
        ("h2h4", "h5h7"),
    ],
)
def test_rotate_uci_180_round_trip(uci: str, expected: str) -> None:
    assert _rotate_uci_180(uci) == expected


def test_rotate_uci_180_is_involution() -> None:
    """rotate(rotate(x)) == x for every valid UCI."""
    for uci in ("e2e4", "g1f3", "d7d5", "e7e5", "f8c5", "a1h8", "h7h5"):
        assert _rotate_uci_180(_rotate_uci_180(uci)) == uci


def test_rotate_uci_180_preserves_promotion() -> None:
    """Promotion suffix carries through the rotation."""
    out = _rotate_uci_180("e7e8q")
    # White's e7→e8 promotion becomes the rotated equivalent w/ same promo suffix.
    assert out.endswith("q")
    assert len(out) == 5


def test_rotate_uci_180_short_input_returned_unchanged() -> None:
    """UCI strings shorter than 4 chars can't be rotated; returned as-is."""
    assert _rotate_uci_180("e2") == "e2"
    assert _rotate_uci_180("") == ""


def test_rotate_uci_180_invalid_rank_returns_input() -> None:
    """Non-digit rank characters cause ValueError → input returned unchanged."""
    assert _rotate_uci_180("e2eX") == "e2eX"


# ─── _coarse_accuracy ──────────────────────────────────────────────────


def test_coarse_accuracy_empty_returns_none() -> None:
    assert PhantomChessCoordinator._coarse_accuracy([]) is None


def test_coarse_accuracy_all_zero_cpls_is_hundred() -> None:
    """Perfect moves (cpl=0 mean) → 100%."""
    assert PhantomChessCoordinator._coarse_accuracy([0, 0, 0, 0]) == 100.0


def test_coarse_accuracy_mean_fifty_reads_seventy_five() -> None:
    """100 - 50/2 = 75."""
    assert PhantomChessCoordinator._coarse_accuracy([50, 50, 50]) == 75.0


def test_coarse_accuracy_huge_mean_clamps_to_zero() -> None:
    """A mean CPL of 300+ clamps to 0% accuracy in the v1 heuristic."""
    assert PhantomChessCoordinator._coarse_accuracy([300, 400, 500]) == 0.0


def test_coarse_accuracy_returns_rounded_to_one_decimal() -> None:
    """The v1 heuristic rounds to one decimal place."""
    # mean = 33 → 100 - 16.5 = 83.5
    val = PhantomChessCoordinator._coarse_accuracy([33, 33, 33])
    assert val == 83.5


# ─── _describe_mistake ─────────────────────────────────────────────────


def test_describe_mistake_fork_motif_takes_priority() -> None:
    """The fork motif overrides classification-based text."""
    out = PhantomChessCoordinator._describe_mistake(
        {"classification": "mistake", "motif": "fork", "cpl": 150}
    )
    assert "fork" in out.lower()


def test_describe_mistake_mate_transition_is_special_cased() -> None:
    """A 9000+ CPL drop is described as 'allowed a forced mate'."""
    out = PhantomChessCoordinator._describe_mistake(
        {"classification": "blunder", "cpl": 9500}
    )
    assert "mate" in out.lower()


def test_describe_mistake_blunder_mentions_pawn_drop() -> None:
    """Non-mate blunder text includes a pawn-count description."""
    out = PhantomChessCoordinator._describe_mistake(
        {"classification": "blunder", "cpl": 350}
    )
    assert "3.5 pawns" in out or "drop" in out.lower()


def test_describe_mistake_mistake_classification() -> None:
    out = PhantomChessCoordinator._describe_mistake(
        {"classification": "mistake", "cpl": 200}
    )
    assert "2.0 pawns" in out or "material" in out.lower()


def test_describe_mistake_missing_fields_doesnt_crash() -> None:
    """Empty / missing input dict produces an empty string, not an exception.

    Unclassified moves (no classification, no motif, no cpl) return ""
    by design — the post-game review skips empty descriptions instead of
    showing a generic "Sub-optimal" placeholder.
    """
    out = PhantomChessCoordinator._describe_mistake({})
    assert out == ""


def test_describe_mistake_appends_engine_suggestion() -> None:
    """When best_san is present, it's appended to the description."""
    out = PhantomChessCoordinator._describe_mistake(
        {"classification": "mistake", "cpl": 150, "best_san": "Nf3"}
    )
    assert "Nf3" in out

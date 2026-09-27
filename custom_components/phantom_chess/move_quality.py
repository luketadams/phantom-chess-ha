"""Shared, disclosed position-relative grading for live and saved-game coaching."""
from __future__ import annotations

import math


def winning_chances(cp: int | None, mate: int | None) -> float | None:
    """Lichess's evaluation curve, not a calibrated forecast for this player."""
    if mate is not None:
        if type(mate) is not int or mate == 0:
            return None  # Mate zero alone does not identify the winning side.
        return 100.0 if mate > 0 else 0.0
    if type(cp) is not int:
        return None
    return 100 / (1 + math.exp(-0.00368208 * max(-10000, min(10000, cp))))


def chance_loss(before: float, after: float, mover_is_white: bool) -> float:
    return max(0.0, (before - after) * (1 if mover_is_white else -1))


def quality_grade(loss: float, best: bool) -> str:
    """Thresholds in percentage points; matching a PV is not brilliance."""
    return ("blunder" if loss >= 20 else "mistake" if loss >= 10 else
            "inaccuracy" if loss >= 5 else "best" if best else
            "excellent" if loss < 2 else "good")

"""Two-player recording mode — pure-logic tests.

The move-detection and live tracking are hardware paths (two humans moving
physical pieces), so they're verified on the board, not here. What we CAN
unit-test in the minimal CI env is the PGN export: given a recorded
``self._board`` move stack, ``_save_two_player_pgn`` must write a valid PGN
with the right moves and result header. We bind the real (unbound) method to
a lightweight stub carrying just the attributes it touches.

Runs in the minimal CI env — only needs ``chess`` (python-chess ships
``chess.pgn``).
"""
from __future__ import annotations

import os
import types

import chess

from custom_components.phantom_chess.coordinator import PhantomChessCoordinator


def _stub(
    tmpdir: str,
    ucis: list[str],
    result_str: str,
    history_ucis: list[str] | None = None,
) -> types.SimpleNamespace:
    """Lightweight stub for ``_save_two_player_pgn``.

    ``ucis`` populates ``self._board`` (the inline-mutated game board).
    ``history_ucis`` populates the displayed ``move_history_moves`` — the
    saver's authoritative source. They default to the same list; pass them
    differently to simulate the self._board-vs-history drift the fix guards.
    """
    board = chess.Board()
    for uci in ucis:
        board.push(chess.Move.from_uci(uci))
    if history_ucis is None:
        history_ucis = ucis
    stub = types.SimpleNamespace()
    stub._board = board
    stub._state = {
        "last_game_result": result_str,
        "move_history_moves": [{"uci": u} for u in history_ucis],
    }
    stub.hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *a: os.path.join(tmpdir, *a))
    )
    return stub


def test_save_two_player_pgn_writes_moves_and_result(tmp_path):
    stub = _stub(str(tmp_path), ["e2e4", "e7e5", "g1f3", "b8c6"], "1-0 (checkmate)")
    path = PhantomChessCoordinator._save_two_player_pgn(stub)
    assert path is not None and os.path.exists(path)
    text = open(path, encoding="utf-8").read()
    assert "1. e4 e5 2. Nf3 Nc6" in text
    assert '[Result "1-0"]' in text
    assert '[White "White"]' in text and '[Black "Black"]' in text
    # The latest recording path is exposed for the dashboard.
    assert stub._state["last_recording_pgn"] == path


def test_save_two_player_pgn_draw_result(tmp_path):
    stub = _stub(str(tmp_path), ["e2e4", "e7e5"], "1/2-1/2 (stalemate)")
    path = PhantomChessCoordinator._save_two_player_pgn(stub)
    text = open(path, encoding="utf-8").read()
    assert '[Result "1/2-1/2"]' in text


def test_save_two_player_pgn_lands_in_recordings_dir(tmp_path):
    stub = _stub(str(tmp_path), ["d2d4"], "* (ended early)")
    path = PhantomChessCoordinator._save_two_player_pgn(stub)
    assert os.path.join("phantom_chess", "recordings") in path
    assert path.endswith(".pgn")


def test_save_two_player_pgn_uses_displayed_history_on_drift(tmp_path):
    """When self._board has drifted from the displayed history, the saved
    PGN must reflect what the dashboard showed, not the drifted board.

    Mirrors the 2026-06-03 live finding: PGN 7 plies vs dashboard 9.
    """
    stub = _stub(
        str(tmp_path),
        ucis=["e2e4", "e7e5"],  # self._board lost the last two plies
        result_str="1-0 (checkmate)",
        history_ucis=["e2e4", "e7e5", "g1f3", "b8c6"],  # dashboard showed 4
    )
    path = PhantomChessCoordinator._save_two_player_pgn(stub)
    text = open(path, encoding="utf-8").read()
    # Full displayed game, not the truncated self._board.
    assert "1. e4 e5 2. Nf3 Nc6" in text


def test_save_two_player_pgn_falls_back_to_board_when_history_garbled(tmp_path):
    """A garbled/illegal UCI in the displayed history makes the saver fall
    back to self._board rather than write a truncated or empty game."""
    stub = _stub(
        str(tmp_path),
        ucis=["e2e4", "e7e5"],
        result_str="* (ended early)",
        history_ucis=["e2e4", "zzzz"],  # second entry is not a valid UCI
    )
    path = PhantomChessCoordinator._save_two_player_pgn(stub)
    text = open(path, encoding="utf-8").read()
    assert "1. e4 e5" in text


def test_save_two_player_pgn_falls_back_to_board_when_history_empty(tmp_path):
    """No displayed history (e.g. analysis pipeline never ran) → self._board."""
    stub = _stub(str(tmp_path), ucis=["e2e4", "e7e5"], result_str="* (ended early)",
                 history_ucis=[])
    path = PhantomChessCoordinator._save_two_player_pgn(stub)
    text = open(path, encoding="utf-8").read()
    assert "1. e4 e5" in text

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


def _stub(tmpdir: str, ucis: list[str], result_str: str) -> types.SimpleNamespace:
    board = chess.Board()
    for uci in ucis:
        board.push(chess.Move.from_uci(uci))
    stub = types.SimpleNamespace()
    stub._board = board
    stub._state = {"last_game_result": result_str}
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

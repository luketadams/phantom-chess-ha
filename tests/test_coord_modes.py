"""Coordinator mode-orchestration tests: two-player, sculpture, AI-vs-AI.

These exercise the high-level game-mode entry points and their loops via the
shared BLE-mock harness (``make_coordinator``). The heavy async collaborators
(the snapshot protocol, Stockfish, analysis pipeline, TTS) are stubbed so the
mode logic itself — state shaping, branch selection, loop termination, error
paths — is what's under test, not the hardware I/O it delegates to.

Runs against the same coordinator object the platform builds, so the branch
coverage here reflects real call paths.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import chess
import pytest

from custom_components.phantom_chess import coordinator as coord_mod
from custom_components.phantom_chess import const

from .ble_mock import FakeBleakClient, make_coordinator


# ── shared helpers ───────────────────────────────────────────────────────────


def _tasks_run_inline(coord) -> list:
    """Make hass.async_create_task close coros (no un-awaited-coro warnings).

    Returns the list of coros it received so a test can inspect what was
    scheduled without leaking them.
    """
    seen: list = []

    def _create(coro, **kwargs):
        seen.append(coro)
        try:
            coro.close()
        except Exception:
            pass
        return MagicMock()

    coord.hass.async_create_task = MagicMock(side_effect=_create)
    return seen


def _stub_loop_create_task(coord) -> list:
    """Give ``hass.loop`` a fake whose ``create_task`` closes coros.

    The harness sets ``hass.loop`` to the *real* running event loop; monkey-
    patching ``create_task`` on that would corrupt the test runner. Instead we
    swap in a lightweight fake that still exposes ``time`` (a monotonic clock
    some helpers read) and a ``create_task`` that just closes the coro so the
    long mode loops never actually run. Returns the list of coros scheduled.
    """
    created: list = []
    fake = MagicMock()
    fake.time = MagicMock(side_effect=lambda: __import__("time").monotonic())

    def _ct(coro, **kwargs):
        created.append(coro)
        try:
            coro.close()
        except Exception:
            pass
        return MagicMock()

    fake.create_task = MagicMock(side_effect=_ct)
    coord.hass.loop = fake
    return created


def _stub_mode_collaborators(coord) -> None:
    """Stub the async collaborators the mode entry points delegate to."""
    coord._phantom_send_game_assistance = AsyncMock()
    coord.async_phantom_start_game = AsyncMock()
    coord._phantom_execute_position = AsyncMock(return_value=True)
    coord.async_phantom_apply_ai_move = AsyncMock()
    coord._analyze_starting_position = AsyncMock()
    coord._announce_via_tts = AsyncMock()
    coord._build_post_game_review = AsyncMock()
    coord._record_and_analyze_local_move = MagicMock()


# ── async_start_two_player_game ──────────────────────────────────────────────


async def test_two_player_start_not_connected_raises():
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="not connected"):
        await coord.async_start_two_player_game()


async def test_two_player_start_shapes_state_and_activates():
    client = FakeBleakClient()
    coord = make_coordinator(client=client, ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)

    await coord.async_start_two_player_game()

    assert coord._two_player_active is True
    assert coord._state["two_player_active"] is True
    assert coord._state["game_status"] == const.STATUS_PLAYING
    assert coord._state["lichess_game_id"] == "two_player"
    assert coord._state["two_player_out_of_sync"] is False
    assert coord._our_color == chess.WHITE
    # SIDE-0 = 2-local-player.
    coord.async_phantom_start_game.assert_awaited_once_with(side="W", side_opcode="0")
    coord.async_set_updated_data.assert_called()


async def test_two_player_start_cancels_existing_tasks():
    client = FakeBleakClient()
    coord = make_coordinator(client=client, ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    lt = MagicMock()
    lt.done.return_value = False
    coord._lichess_task = lt
    local = MagicMock()
    local.done.return_value = False
    coord._local_game_task = local

    await coord.async_start_two_player_game()

    lt.cancel.assert_called_once()
    local.cancel.assert_called_once()


async def test_two_player_start_game_assistance_failure_is_swallowed():
    client = FakeBleakClient()
    coord = make_coordinator(client=client, ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._phantom_send_game_assistance = AsyncMock(side_effect=RuntimeError("boom"))

    # Must not raise — GA failure is warned + swallowed.
    await coord.async_start_two_player_game()
    assert coord._two_player_active is True


# ── _flag_two_player_out_of_sync / _clear_two_player_out_of_sync ─────────────


async def test_flag_two_player_out_of_sync_sets_flag_and_notifies():
    coord = make_coordinator(ble_connected=True)
    seen = _tasks_run_inline(coord)
    coord._state["two_player_out_of_sync"] = False
    coord._state["game_status"] = const.STATUS_PLAYING  # so it announces
    coord._sculpture_active = False

    coord._flag_two_player_out_of_sync("e2e4", "d7d5")

    assert coord._state["two_player_out_of_sync"] is True
    coord.async_set_updated_data.assert_called()
    # scheduled a notification create + a TTS announcement.
    assert len(seen) >= 1


async def test_flag_two_player_out_of_sync_idempotent_when_already_flagged():
    coord = make_coordinator(ble_connected=True)
    seen = _tasks_run_inline(coord)
    coord._state["two_player_out_of_sync"] = True

    coord._flag_two_player_out_of_sync("e2e4", "d7d5")

    # Early return: no new scheduled work.
    assert seen == []


async def test_flag_two_player_out_of_sync_no_announce_when_idle():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    coord._state["two_player_out_of_sync"] = False
    coord._state["game_status"] = const.STATUS_IDLE  # not an active game

    coord._flag_two_player_out_of_sync("e2e4", "d7d5")
    assert coord._state["two_player_out_of_sync"] is True


async def test_clear_two_player_out_of_sync_clears_and_dismisses():
    coord = make_coordinator(ble_connected=True)
    seen = _tasks_run_inline(coord)
    coord._state["two_player_out_of_sync"] = True

    coord._clear_two_player_out_of_sync()

    assert coord._state["two_player_out_of_sync"] is False
    assert len(seen) == 1  # persistent_notification.dismiss scheduled


# ── async_resync_two_player ─────────────────────────────────────────────────


async def test_resync_two_player_not_active_raises():
    coord = make_coordinator(ble_connected=True)
    coord._two_player_active = False
    with pytest.raises(RuntimeError, match="No two-player recording"):
        await coord.async_resync_two_player()


async def test_resync_two_player_not_connected_raises():
    coord = make_coordinator(ble_connected=False)
    coord._two_player_active = True
    with pytest.raises(RuntimeError, match="not connected"):
        await coord.async_resync_two_player()


async def test_resync_two_player_drives_position_and_clears_flag():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    coord._two_player_active = True
    coord._state["two_player_out_of_sync"] = True
    coord._phantom_execute_position = AsyncMock(return_value=True)
    coord._announce_via_tts = AsyncMock()

    await coord.async_resync_two_player()

    coord._phantom_execute_position.assert_awaited_once()
    kwargs = coord._phantom_execute_position.await_args.kwargs
    assert kwargs["side_opcode"] == "0"
    assert kwargs["side"] == "W"  # fresh board, white to move
    assert coord._state["two_player_out_of_sync"] is False


async def test_resync_two_player_timeout_still_clears_and_announces():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    coord._two_player_active = True
    coord._phantom_execute_position = AsyncMock(return_value=False)  # timed out

    await coord.async_resync_two_player()  # must not raise
    assert coord._state["two_player_out_of_sync"] is False


# ── _finalize_two_player_game ───────────────────────────────────────────────


async def test_finalize_two_player_noop_when_inactive():
    coord = make_coordinator(ble_connected=True)
    coord._two_player_active = False
    await coord._finalize_two_player_game()
    # Nothing scheduled, status untouched.
    assert coord._two_player_active is False


async def test_finalize_two_player_checkmate_result():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    coord._two_player_active = True
    # Fool's mate: 1. f3 e5 2. g4 Qh4#
    b = chess.Board()
    for uci in ["f2f3", "e7e5", "g2g4", "d8h4"]:
        b.push(chess.Move.from_uci(uci))
    coord._board = b
    coord._save_two_player_pgn = MagicMock(return_value="/tmp/x.pgn")

    await coord._finalize_two_player_game()

    assert coord._state["game_status"] == const.STATUS_CHECKMATE
    # Fool's mate: Black delivers mate, so after the mating move it's White's
    # turn and the result is "0-1".
    assert coord._state["last_game_result"].startswith("0-1")
    assert coord._two_player_active is False
    assert coord._state["lichess_review_ready"] is True
    coord._save_two_player_pgn.assert_called_once()


async def test_finalize_two_player_stalemate_result():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    coord._two_player_active = True
    coord._board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")  # black stalemated
    coord._save_two_player_pgn = MagicMock(return_value=None)

    await coord._finalize_two_player_game()
    assert coord._state["game_status"] == const.STATUS_STALEMATE
    assert "stalemate" in coord._state["last_game_result"]


async def test_finalize_two_player_insufficient_material():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    coord._two_player_active = True
    coord._board = chess.Board("8/8/8/4k3/8/8/4K3/8 w - - 0 1")  # kings only
    coord._save_two_player_pgn = MagicMock(return_value=None)

    await coord._finalize_two_player_game()
    assert coord._state["game_status"] == const.STATUS_DRAW
    assert "insufficient" in coord._state["last_game_result"]


async def test_finalize_two_player_ended_early():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    coord._two_player_active = True
    coord._board = chess.Board()  # in-progress
    coord._save_two_player_pgn = MagicMock(return_value=None)

    await coord._finalize_two_player_game()
    assert coord._state["game_status"] == const.STATUS_IDLE
    assert coord._state["last_game_result"].startswith("*")


async def test_finalize_two_player_pgn_save_failure_swallowed():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    coord._two_player_active = True
    coord._board = chess.Board()
    coord._save_two_player_pgn = MagicMock(side_effect=OSError("disk full"))

    # async_add_executor_job runs inline; the OSError must be caught + warned.
    await coord._finalize_two_player_game()
    assert coord._two_player_active is False


# ── _save_two_player_pgn ────────────────────────────────────────────────────


async def test_save_two_player_pgn_from_history(tmp_path):
    coord = make_coordinator(ble_connected=True)
    coord.hass.config = MagicMock()
    coord.hass.config.path = lambda *a: os.path.join(str(tmp_path), *a)
    b = chess.Board()
    ucis = ["e2e4", "e7e5", "g1f3", "b8c6"]
    for u in ucis:
        b.push(chess.Move.from_uci(u))
    coord._board = b
    coord._state["move_history_moves"] = [{"uci": u} for u in ucis]
    coord._state["last_game_result"] = "1-0 (checkmate)"

    path = coord._save_two_player_pgn()
    assert path is not None and os.path.exists(path)
    text = open(path, encoding="utf-8").read()
    assert "1. e4 e5 2. Nf3 Nc6" in text
    assert '[Result "1-0"]' in text
    assert coord._state["last_recording_pgn"] == path


async def test_save_two_player_pgn_falls_back_to_board_on_bad_history(tmp_path):
    coord = make_coordinator(ble_connected=True)
    coord.hass.config = MagicMock()
    coord.hass.config.path = lambda *a: os.path.join(str(tmp_path), *a)
    b = chess.Board()
    for u in ["e2e4", "e7e5"]:
        b.push(chess.Move.from_uci(u))
    coord._board = b
    # History contains a garbled UCI so the replay aborts -> board fallback.
    coord._state["move_history_moves"] = [{"uci": "zzzz"}]
    coord._state["last_game_result"] = "* (ended early)"

    path = coord._save_two_player_pgn()
    assert path is not None and os.path.exists(path)
    text = open(path, encoding="utf-8").read()
    assert "1. e4 e5" in text  # came from self._board fallback


# ── sculpture: _load_sculpture_games_blocking / _async_get_sculpture_games ───


async def test_load_sculpture_games_blocking_reads_bundle():
    coord = make_coordinator(ble_connected=True)
    games = coord._load_sculpture_games_blocking()
    assert isinstance(games, dict) and games
    # Every bundled record shaped as expected.
    sample = next(iter(games.values()))
    assert "moves" in sample


async def test_async_get_sculpture_games_caches():
    coord = make_coordinator(ble_connected=True)
    assert coord._sculpture_games_cache is None
    games = await coord._async_get_sculpture_games()
    assert games and coord._sculpture_games_cache is games
    # Second call returns the cached object without re-reading.
    coord._load_sculpture_games_blocking = MagicMock(side_effect=AssertionError)
    again = await coord._async_get_sculpture_games()
    assert again is games


async def test_async_get_sculpture_games_load_failure_returns_empty():
    coord = make_coordinator(ble_connected=True)
    coord.hass.async_add_executor_job = AsyncMock(side_effect=OSError("no file"))
    games = await coord._async_get_sculpture_games()
    assert games == {}


# ── async_play_selected_sculpture ───────────────────────────────────────────


async def test_play_selected_sculpture_not_connected_raises():
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="not connected"):
        await coord.async_play_selected_sculpture()


async def test_play_selected_sculpture_unknown_falls_back_to_firmware():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    coord._sculpture_games_cache = {}  # bypass file IO; nothing bundled
    coord.selected_sculpture = "no-such-game"
    coord.async_start_sculpture = AsyncMock()

    await coord.async_play_selected_sculpture()

    coord.async_start_sculpture.assert_awaited_once()


async def test_play_selected_sculpture_drives_selected_game():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    # Bypass file IO with a tiny bundled game.
    coord._sculpture_games_cache = {
        "TestGame": {
            "white": "Alice", "black": "Bob", "eco": "C20",
            "moves": ["e2e4", "e7e5"],
        }
    }
    coord.selected_sculpture = "TestGame"
    # Prevent the real loop task from running; capture the coro.
    created = _stub_loop_create_task(coord)

    await coord.async_play_selected_sculpture()

    assert coord._sculpture_active is True
    assert coord._local_game_active is True
    assert coord._state["lichess_game_id"] == "sculpture"
    assert coord._state["opening_eco"] == "C20"
    assert coord._state["lichess_white_name"] == "Alice (W)"
    coord.async_phantom_start_game.assert_awaited_once_with(side="W", side_opcode="1")
    assert created  # loop task was created


# ── _sculpture_loop ─────────────────────────────────────────────────────────


async def test_sculpture_loop_plays_all_moves_then_completes():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._sculpture_active = True
    coord._local_game_active = True
    coord._sculpture_move_delay = 0.0
    coord.selected_sculpture = "TestGame"

    # The real apply_ai_move pushes the move onto self._board; the loop checks
    # legality against self._board before each ply, so the stub must advance it.
    async def _apply(uci):
        coord._board.push(chess.Move.from_uci(uci))

    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=_apply)

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord._sculpture_loop(["e2e4", "e7e5"])

    assert coord._sculpture_active is False
    assert coord._local_game_active is False
    assert coord._state["lichess_game_id"] is None
    assert coord._state["lichess_review_ready"] is True
    assert coord._state["last_game_result"] == "Historic game complete"
    assert coord.async_phantom_apply_ai_move.await_count == 2


async def test_sculpture_loop_stops_on_illegal_move():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._sculpture_active = True
    coord._local_game_active = True
    coord._sculpture_move_delay = 0.0

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        # e7e5 is illegal as the very first move (white to move).
        await coord._sculpture_loop(["e7e5"])

    # Stopped early -> no review, idle status.
    assert coord._state["game_status"] == const.STATUS_IDLE
    assert coord._state["lichess_review_ready"] is False
    coord.async_phantom_apply_ai_move.assert_not_awaited()


async def test_sculpture_loop_stops_on_bad_uci():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._sculpture_active = True
    coord._sculpture_move_delay = 0.0

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord._sculpture_loop(["zzzz"])

    coord.async_phantom_apply_ai_move.assert_not_awaited()
    assert coord._sculpture_active is False


async def test_sculpture_loop_reconnect_recovery():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._sculpture_active = True
    coord._sculpture_move_delay = 0.0
    # First apply raises (BLE drop); reconnect succeeds; re-drive succeeds.
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=RuntimeError("ble drop"))
    coord._ai_vs_ai_await_reconnect = AsyncMock(return_value=True)
    coord._phantom_execute_position = AsyncMock(return_value=True)

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord._sculpture_loop(["e2e4"])

    coord._ai_vs_ai_await_reconnect.assert_awaited_once()
    coord._phantom_execute_position.assert_awaited()


async def test_sculpture_loop_reconnect_fails_stops():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._sculpture_active = True
    coord._sculpture_move_delay = 0.0
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=RuntimeError("ble drop"))
    coord._ai_vs_ai_await_reconnect = AsyncMock(return_value=False)

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord._sculpture_loop(["e2e4"])

    # Never re-drove because reconnect failed.
    coord._phantom_execute_position.assert_not_awaited()


async def test_sculpture_loop_inactive_flag_breaks_immediately():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._sculpture_active = False  # cleared before loop body
    coord._sculpture_move_delay = 0.0

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord._sculpture_loop(["e2e4", "e7e5"])

    coord.async_phantom_apply_ai_move.assert_not_awaited()
    assert coord._state["game_status"] == const.STATUS_IDLE


# ── async_start_sculpture ───────────────────────────────────────────────────


async def test_async_start_sculpture_not_connected_raises():
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="BLE not connected"):
        await coord.async_start_sculpture()


async def test_async_start_sculpture_writes_mode_and_ends_game():
    client = FakeBleakClient()
    coord = make_coordinator(client=client, ble_connected=True)
    coord._phantom_send_game_end = AsyncMock()
    coord._ble_write = AsyncMock()
    coord._phantom_session_initialized = True

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord.async_start_sculpture()

    coord._phantom_send_game_end.assert_awaited_once()
    coord._ble_write.assert_awaited_once()
    wr_uuid = coord._ble_write.await_args.args[0]
    assert wr_uuid == const.UUID_SELECT_MODE
    assert coord._phantom_session_initialized is False


# ── async_start_ai_vs_ai_game ───────────────────────────────────────────────


async def test_ai_vs_ai_start_not_connected_raises():
    coord = make_coordinator(ble_connected=False)
    with pytest.raises(RuntimeError, match="not connected"):
        await coord.async_start_ai_vs_ai_game()


async def test_ai_vs_ai_start_shapes_state_and_levels():
    client = FakeBleakClient()
    coord = make_coordinator(client=client, ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord.ai_level = 5
    created = _stub_loop_create_task(coord)

    await coord.async_start_ai_vs_ai_game(white_ai_level=7, move_delay_seconds=0.0)

    assert coord._ai_vs_ai_active is True
    assert coord._ai_vs_ai_white_level == 7
    assert coord._ai_vs_ai_black_level == 7  # defaults to white when None
    assert coord._ai_vs_ai_move_delay == 0.0
    assert coord._state["lichess_game_id"] == "ai_vs_ai"
    assert "level 7" in coord._state["lichess_white_name"]
    coord.async_phantom_start_game.assert_awaited_once_with(side="W", side_opcode="1")
    assert created  # loop task created


async def test_ai_vs_ai_start_defaults_levels_to_ai_level():
    client = FakeBleakClient()
    coord = make_coordinator(client=client, ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord.ai_level = 4
    _stub_loop_create_task(coord)

    await coord.async_start_ai_vs_ai_game()
    assert coord._ai_vs_ai_white_level == 4
    assert coord._ai_vs_ai_black_level == 4


async def test_ai_vs_ai_start_negative_delay_clamped():
    client = FakeBleakClient()
    coord = make_coordinator(client=client, ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    _stub_loop_create_task(coord)

    await coord.async_start_ai_vs_ai_game(move_delay_seconds=-5.0)
    assert coord._ai_vs_ai_move_delay == 0.0


# ── _ai_vs_ai_await_reconnect ───────────────────────────────────────────────


async def test_await_reconnect_returns_true_when_connected():
    coord = make_coordinator(ble_connected=True)
    coord._ai_vs_ai_active = True
    coord._ble_connected = True
    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        ok = await coord._ai_vs_ai_await_reconnect(timeout=5.0)
    assert ok is True


async def test_await_reconnect_returns_false_when_inactive():
    coord = make_coordinator(ble_connected=True)
    coord._ai_vs_ai_active = False
    coord._sculpture_active = False
    coord._ble_connected = False
    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        ok = await coord._ai_vs_ai_await_reconnect(timeout=5.0)
    assert ok is False


async def test_await_reconnect_times_out_when_never_connects():
    coord = make_coordinator(ble_connected=True)
    coord._ai_vs_ai_active = True
    coord._sculpture_active = False
    coord._ble_connected = False
    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        # hass.loop.time() advances via the real loop; give a tiny timeout so
        # the deadline is reached after a couple of iterations.
        ok = await coord._ai_vs_ai_await_reconnect(timeout=0.0)
    assert ok is False


# ── _ai_vs_ai_loop ──────────────────────────────────────────────────────────


async def test_ai_vs_ai_loop_plays_moves_then_stops_on_flag():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._ai_vs_ai_active = True
    coord._ai_vs_ai_move_delay = 0.0
    coord._ai_vs_ai_white_level = 3
    coord._ai_vs_ai_black_level = 3

    moves = iter(["e2e4", "e7e5"])

    async def _next_move(board):
        try:
            uci = next(moves)
        except StopIteration:
            coord._ai_vs_ai_active = False  # halt the loop
            return None
        # apply_ai_move is stubbed and won't push; push here to advance turn.
        coord._board.push(chess.Move.from_uci(uci))
        return uci

    coord._get_ai_move = AsyncMock(side_effect=_next_move)

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord._ai_vs_ai_loop()

    assert coord._ai_vs_ai_active is False
    assert coord._local_game_active is False
    assert coord._state["lichess_review_ready"] is True
    assert coord.async_phantom_apply_ai_move.await_count == 2


async def test_ai_vs_ai_loop_no_move_breaks():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._ai_vs_ai_active = True
    coord._ai_vs_ai_move_delay = 0.0
    coord._get_ai_move = AsyncMock(return_value=None)  # Stockfish gave nothing

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord._ai_vs_ai_loop()

    coord.async_phantom_apply_ai_move.assert_not_awaited()
    assert coord._state["game_status"] == const.STATUS_IDLE


async def test_ai_vs_ai_loop_reaches_checkmate():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._ai_vs_ai_active = True
    coord._ai_vs_ai_move_delay = 0.0
    # Fool's mate line.
    moves = iter(["f2f3", "e7e5", "g2g4", "d8h4"])

    async def _next_move(board):
        uci = next(moves)
        coord._board.push(chess.Move.from_uci(uci))
        return uci

    coord._get_ai_move = AsyncMock(side_effect=_next_move)

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord._ai_vs_ai_loop()

    assert coord._state["game_status"] == const.STATUS_CHECKMATE
    # Fool's mate: Black delivers mate, so after the mating move it's White's
    # turn and the result is "0-1".
    assert coord._state["last_game_result"].startswith("0-1")


async def test_ai_vs_ai_loop_reconnect_recovery():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._ai_vs_ai_active = True
    coord._ai_vs_ai_move_delay = 0.0

    calls = {"n": 0}

    async def _next_move(board):
        calls["n"] += 1
        if calls["n"] == 1:
            coord._board.push(chess.Move.from_uci("e2e4"))
            return "e2e4"
        coord._ai_vs_ai_active = False
        return None

    coord._get_ai_move = AsyncMock(side_effect=_next_move)
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=RuntimeError("ble drop"))
    coord._ai_vs_ai_await_reconnect = AsyncMock(return_value=True)
    coord._phantom_execute_position = AsyncMock(return_value=True)

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord._ai_vs_ai_loop()

    coord._ai_vs_ai_await_reconnect.assert_awaited()
    coord._phantom_execute_position.assert_awaited()


async def test_ai_vs_ai_loop_reconnect_fails_stops():
    coord = make_coordinator(ble_connected=True)
    _tasks_run_inline(coord)
    _stub_mode_collaborators(coord)
    coord._ai_vs_ai_active = True
    coord._ai_vs_ai_move_delay = 0.0

    async def _next_move(board):
        coord._board.push(chess.Move.from_uci("e2e4"))
        return "e2e4"

    coord._get_ai_move = AsyncMock(side_effect=_next_move)
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=RuntimeError("ble drop"))
    coord._ai_vs_ai_await_reconnect = AsyncMock(return_value=False)

    with patch.object(coord_mod.asyncio, "sleep", new=AsyncMock()):
        await coord._ai_vs_ai_loop()

    coord._phantom_execute_position.assert_not_awaited()
    assert coord._ai_vs_ai_active is False

"""Persistence, corruption and import/export behavior without physical hardware."""
from dataclasses import replace
from unittest.mock import AsyncMock

import chess
import pytest

from custom_components.phantom_chess.game_library import GameLibrary, SavedGame


def sample():
    board = chess.Board()
    for move in ("e2e4", "e7e5", "g1f3"):
        board.push_uci(move)
    return SavedGame.capture(board, player_color="black", ai_level=6)


async def test_session_survives_storage_round_trip():
    store = AsyncMock()
    library = GameLibrary(store)
    game = sample()
    await library.put(game)
    store.async_load.return_value = store.async_save.call_args.args[0]
    restored = GameLibrary(store)
    await restored.load()
    assert restored.recovery_id == game.game_id
    assert restored.get(game.game_id).board().fen() == game.board().fen()
    assert restored.get(game.game_id).player_color == "black"
    assert restored.get(game.game_id).ai_level == 6


async def test_failed_write_preserves_previous_checkpoint():
    store = AsyncMock()
    library = GameLibrary(store)
    game = sample()
    await library.put(game)
    store.async_save.side_effect = OSError("disk full")
    with pytest.raises(OSError):
        await library.put(replace(game, revision=1, status="finished"))
    assert library.get(game.game_id) == game
    assert library.recovery_id == game.game_id


async def test_stale_revision_cannot_replace_newer_game():
    store = AsyncMock()
    library = GameLibrary(store)
    game = sample()
    await library.put(replace(game, revision=2, status="finished"))
    await library.put(replace(game, revision=1))
    assert library.get(game.game_id).status == "finished"
    assert library.recovery_id is None
    assert store.async_save.await_count == 1


async def test_corrupt_record_is_not_silently_overwritten():
    game = sample()
    corrupt = game.encode()
    corrupt["moves"] = ["e2e5"]
    store = AsyncMock()
    store.async_load.return_value = {"games": [corrupt], "recovery_id": game.game_id}
    library = GameLibrary(store)
    await library.load()
    assert library.invalid_records == 1
    assert library.recovery_id is None
    with pytest.raises(ValueError, match="could not be read"):
        await library.put(game)
    store.async_save.assert_not_awaited()


def test_pgn_roundtrip_preserves_position_and_moves():
    original = sample()
    imported = SavedGame.from_pgn(original.pgn())
    assert imported.moves == original.moves
    assert imported.board().fen() == original.board().fen()


@pytest.mark.parametrize("text", ["", "1. e5 *", "x" * 256001])
def test_bad_pgn_rejected(text):
    with pytest.raises(ValueError):
        SavedGame.from_pgn(text)


async def test_library_search_and_delete():
    library = GameLibrary(AsyncMock())
    game = replace(sample(), headers={"White": "Luke", "Black": "Computer"})
    await library.put(game)
    assert library.list("luke")[0]["game_id"] == game.game_id
    assert library.list("absent") == []
    await library.delete(game.game_id)
    assert library.list() == []
    assert library.recovery_id is None


async def test_recovery_does_not_activate_until_board_confirms():
    from .ble_mock import make_coordinator
    c = make_coordinator()
    c._library = GameLibrary(AsyncMock())
    c._saved_game_id = None
    c._saved_revision = 0
    c._journal_tasks = set()
    saved = sample()
    await c._library.put(saved)
    c._phantom_execute_position = AsyncMock(return_value=False)
    c._replace_local_game_task = AsyncMock()
    original = c._board.fen()
    with pytest.raises(TimeoutError):
        await c.async_resume_game()
    assert not c._local_game_active
    assert c._board.fen() == original
    assert c._library.recovery_id == saved.game_id
    c._replace_local_game_task.assert_not_awaited()
    c._phantom_execute_position.return_value = True
    await c.async_resume_game()
    assert c._local_game_active
    assert c._board.fen() == saved.board().fen()
    assert c.ai_level == 6
    assert c._our_color == chess.BLACK
    assert c._state["move_history_moves"][-1]["san"] == "Nf3"


async def test_checkpoint_captures_position_before_async_save():
    from .ble_mock import make_coordinator
    c = make_coordinator()
    c._library = GameLibrary(AsyncMock())
    c._saved_game_id = "session1"
    c._saved_revision = 0
    c._journal_tasks = set()
    c._our_color = chess.WHITE
    c._board.push_uci("e2e4")
    c._queue_checkpoint()
    c._board.push_uci("e7e5")
    await c._flush_journal()
    assert c._library.get("session1").moves == ("e2e4",)


async def test_corrupt_recovery_pointer_does_not_crash_loading():
    store = AsyncMock()
    game = sample()
    store.async_load.return_value = {"games": [game.encode()], "recovery_id": []}
    library = GameLibrary(store)
    await library.load()
    assert library.get(game.game_id).moves == game.moves
    assert library.recovery_id is None


async def test_practice_forks_position_and_export_replays_legally():
    from .ble_mock import make_coordinator
    c = make_coordinator()
    c._library = GameLibrary(AsyncMock())
    saved = sample()
    await c._library.put(saved)
    export = await c.async_game_library("export", game_id=saved.game_id)
    assert len(export["positions"]) == len(saved.moves) + 1
    assert export["positions"][-1] == saved.board().fen()
    practice = await c.async_game_library("practice", game_id=saved.game_id, ply=2)
    assert c._library.get(practice["game_id"]).initial_fen == export["positions"][2]
    assert c._library.get(practice["game_id"]).moves == ()
    assert c._library.get(saved.game_id) == saved
    with pytest.raises(ValueError, match="within"):
        await c.async_game_library("practice", game_id=saved.game_id, ply=999)


async def test_recovery_can_reconcile_uncertainty_but_cannot_replace_live_game():
    from .ble_mock import make_coordinator
    c = make_coordinator()
    c._library = GameLibrary(AsyncMock())
    await c._library.put(sample())
    c._state["physical_operation"] = "uncertain"
    c._phantom_execute_position = AsyncMock(return_value=True)
    c._replace_local_game_task = AsyncMock()
    c._game_id = "online"
    with pytest.raises(RuntimeError, match="already running"):
        await c.async_resume_game()
    c._phantom_execute_position.assert_not_awaited()
    c._game_id = None
    await c.async_resume_game()
    assert c._local_game_active


async def test_failed_save_pauses_play_and_surfaces_error():
    from .ble_mock import make_coordinator
    c = make_coordinator()
    store = AsyncMock()
    store.async_save.side_effect = OSError("disk full")
    c._library = GameLibrary(store)
    c._saved_game_id = "active1"
    c._our_color = chess.WHITE
    with pytest.raises(OSError):
        await c.async_checkpoint()
    assert c.paused
    assert "disk full" in c._state["journal_error"]


async def test_save_pause_library_import_delete_and_restart_lifecycle():
    from .ble_mock import make_coordinator
    c = make_coordinator()
    c._library = GameLibrary(AsyncMock())
    c._our_color = chess.WHITE
    c._local_game_active = True
    c.async_set_pause = AsyncMock()
    await c._begin_saved_session()
    saved = await c.async_save_game()
    assert saved["saved"]
    c.async_set_pause.assert_awaited_once_with(True)
    assert c._library.get(saved["game_id"]).status == "paused"
    await c.async_resume_game(saved["game_id"])  # duplicate request preserves active session
    with pytest.raises(ValueError, match="End the active"):
        await c.async_game_library("delete", game_id=saved["game_id"])
    async with c._physical_operation_lock:
        with pytest.raises(RuntimeError, match="finish moving"):
            await c.async_save_game()
    c._local_game_active = False
    with pytest.raises(ValueError, match="no active"):
        await c.async_save_game()
    imported = await c.async_game_library("import", pgn=sample().pgn())
    assert len((await c.async_game_library("list"))["games"]) == 2
    await c.async_game_library("delete", game_id=imported["game_id"])
    assert len(c._library.list()) == 1
    with pytest.raises(ValueError, match="Unknown"):
        await c.async_game_library("wrong", game_id=saved["game_id"])
    await c.async_game_library("delete", game_id=saved["game_id"])
    c._saved_game_id = None
    with pytest.raises(ValueError, match="Choose"):
        await c.async_game_library("export")
    with pytest.raises(ValueError, match="no unfinished"):
        await c.async_resume_game()


@pytest.mark.parametrize("method,args", [("async_save_game",()), ("async_resume_game",()), ("async_game_library",("list",))])
async def test_missing_library_reports_actionable_error(method, args):
    from .ble_mock import make_coordinator
    c = make_coordinator()
    with pytest.raises(RuntimeError, match="unavailable"):
        await getattr(c, method)(*args)


@pytest.mark.parametrize("field,value", [
    ("game_id", "../escape"), ("revision", -1), ("ai_level", 99),
    ("player_color", "red"), ("moves", 12), ("headers", {"White": 7}),
    ("updated", None), ("updated", "2026-09-05T00:00:00"),
    ("initial_fen", None), ("initial_fen", "8/8/8/8/8/8/8/8 w - - 0 1"),
])
def test_invalid_checkpoint_metadata_is_rejected(field, value):
    raw = sample().encode()
    raw[field] = value
    with pytest.raises(ValueError):
        SavedGame.decode(raw)


async def test_finishing_previous_game_cannot_clear_new_recovery():
    library = GameLibrary(AsyncMock())
    old, new = sample(), sample()
    await library.put(old)
    await library.put(new)
    await library.put(replace(old, status="finished", revision=1))
    assert library.recovery_id == new.game_id

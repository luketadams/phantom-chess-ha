"""Coverage for coordinator.py analysis, echo-suppression, TTS-announce,
post-game review, and dashboard-service surfaces.

These methods are pure-ish (no BLE, no HA scheduling) so they exercise well
against the ble_mock harness. The Lichess analysis client is stubbed with
AsyncMocks returning ``EvalResult`` shapes; the announce path drives the
``phantom_chess_announce`` event bus and ``tts.speak`` service call.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import chess

from custom_components.phantom_chess import const
from custom_components.phantom_chess import coordinator as coord_mod
from custom_components.phantom_chess.coordinator import PhantomChessCoordinator
from custom_components.phantom_chess.lichess_analysis import EvalResult

from .ble_mock import make_coordinator


# ── helpers ──────────────────────────────────────────────────────────────────


def _entry_with_tts(**extra) -> types.SimpleNamespace:
    options = {
        "tts_service": "tts.home_assistant_cloud",
        "tts_media_player_entity_id": "media_player.living_room_voice",
    }
    options.update(extra)
    return types.SimpleNamespace(options=options)


def _wire_tts(coord) -> None:
    """Make the announce path's service call observable/awaitable."""
    coord.hass.services = MagicMock()
    coord.hass.services.async_call = AsyncMock()
    coord.hass.bus = MagicMock()
    coord.hass.bus.async_fire = MagicMock()


# ── _set_last_ai_move / _is_ai_echo ──────────────────────────────────────────


async def test_set_last_ai_move_too_short_is_noop():
    c = make_coordinator()
    c._set_last_ai_move("e2")  # < 4 chars
    assert c._last_ai_uci is None
    assert c._last_ai_echo_ucis == set()


async def test_set_last_ai_move_records_primary_and_rotated():
    c = make_coordinator()
    c._set_last_ai_move("e2e4")
    assert c._last_ai_uci == "e2e4"
    # rotated form (180deg) of e2e4 is e5e7 per _rotate_uci_180
    assert c._last_ai_uci_rotated == coord_mod._rotate_uci_180("e2e4")
    assert "e2e4" in c._last_ai_echo_ucis
    assert c._last_ai_uci_rotated in c._last_ai_echo_ucis


async def test_set_last_ai_move_castling_adds_rook_ucis():
    c = make_coordinator()
    # White kingside castle: king e1g1, rook h1f1.
    board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    mv = chess.Move.from_uci("e1g1")
    assert board.is_castling(mv)
    c._set_last_ai_move("e1g1", mv=mv, pre_move_board=board)
    assert "h1f1" in c._last_ai_echo_ucis  # rook primary
    assert coord_mod._rotate_uci_180("h1f1") in c._last_ai_echo_ucis


async def test_set_last_ai_move_queenside_castle_rook():
    c = make_coordinator()
    board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    mv = chess.Move.from_uci("e1c1")  # queenside
    assert board.is_queenside_castling(mv)
    c._set_last_ai_move("e1c1", mv=mv, pre_move_board=board)
    assert "a1d1" in c._last_ai_echo_ucis  # a-file rook to d-file


async def test_is_ai_echo_matches_primary():
    c = make_coordinator()
    c._set_last_ai_move("e2e4")
    assert c._is_ai_echo("M 1 e2-e4") is True


async def test_is_ai_echo_no_last_move_false():
    c = make_coordinator()
    # Fresh coordinator: no echo set, no last uci.
    assert c._is_ai_echo("M 1 e2-e4") is False


async def test_is_ai_echo_window_expired_false():
    c = make_coordinator()
    c._set_last_ai_move("e2e4")
    # Force the set-at time far into the past so the 60s window has expired.
    c._last_ai_uci_set_at -= 120.0
    assert c._is_ai_echo("M 1 e2-e4") is False


async def test_is_ai_echo_unrelated_move_false():
    c = make_coordinator()
    c._set_last_ai_move("e2e4")
    assert c._is_ai_echo("M 1 d2-d4") is False


async def test_is_ai_echo_unparseable_payload_false():
    c = make_coordinator()
    c._set_last_ai_move("e2e4")
    assert c._is_ai_echo("garbage no squares") is False


# ── _build_move_speech ───────────────────────────────────────────────────────


async def test_build_move_speech_pawn_move():
    c = make_coordinator()
    c._board = chess.Board()
    mv = chess.Move.from_uci("e2e4")
    assert c._build_move_speech(mv) == "White pawn to e4"


async def test_build_move_speech_capture():
    c = make_coordinator()
    # White pawn on e4, black pawn on d5 -> exd5 is a capture.
    c._board = chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2")
    mv = chess.Move.from_uci("e4d5")
    assert c._build_move_speech(mv) == "White pawn takes d5"


async def test_build_move_speech_castle_kingside():
    c = make_coordinator()
    c._board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    mv = chess.Move.from_uci("e1g1")
    assert c._build_move_speech(mv) == "White castles kingside"


async def test_build_move_speech_castle_queenside():
    c = make_coordinator()
    c._board = chess.Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    mv = chess.Move.from_uci("e1c1")
    assert c._build_move_speech(mv) == "White castles queenside"


async def test_build_move_speech_black_knight():
    c = make_coordinator()
    c._board = chess.Board()
    c._board.push_uci("e2e4")  # now black to move
    mv = chess.Move.from_uci("g8f6")
    assert c._build_move_speech(mv) == "Black knight to f6"


async def test_build_move_speech_empty_square_returns_blank():
    c = make_coordinator()
    c._board = chess.Board()
    # a3 is empty at start; from_square has no piece.
    mv = chess.Move(chess.A3, chess.A4)
    assert c._build_move_speech(mv) == ""


# ── _post_move_event_speech ──────────────────────────────────────────────────


async def test_post_move_event_speech_checkmate():
    c = make_coordinator()
    # Fool's mate reached; black to move and mated.
    c._board = chess.Board()
    for u in ("f2f3", "e7e5", "g2g4", "d8h4"):
        c._board.push_uci(u)
    assert c._board.is_checkmate()
    # turn is WHITE (side to move, mated) -> Black wins.
    assert c._post_move_event_speech() == "Checkmate. Black wins."


async def test_post_move_event_speech_check():
    c = make_coordinator()
    c._board = chess.Board("rnbqkbnr/ppp2ppp/8/1B1pp3/4P3/8/PPPP1PPP/RNBQK1NR b KQkq - 1 3")
    assert c._board.is_check()
    assert c._post_move_event_speech() == "Check on Black."


async def test_post_move_event_speech_stalemate():
    c = make_coordinator()
    c._board = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    assert c._board.is_stalemate()
    assert c._post_move_event_speech() == "Stalemate. Draw."


async def test_post_move_event_speech_none():
    c = make_coordinator()
    c._board = chess.Board()
    assert c._post_move_event_speech() == ""


# ── _announce_via_tts ────────────────────────────────────────────────────────


async def test_announce_via_tts_empty_noop():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = True
    await c._announce_via_tts("")
    c.hass.bus.async_fire.assert_not_called()
    c.hass.services.async_call.assert_not_called()


async def test_announce_via_tts_muted_fires_event_only():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = False
    await c._announce_via_tts("Knight to f3")
    c.hass.bus.async_fire.assert_called_once()
    _, payload = c.hass.bus.async_fire.call_args.args
    assert payload["voice_enabled"] is False
    assert payload["message"] == "Knight to f3"
    c.hass.services.async_call.assert_not_called()


async def test_announce_via_tts_unmuted_speaks():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = True
    await c._announce_via_tts("Check")
    c.hass.services.async_call.assert_called_once()
    domain, service = c.hass.services.async_call.call_args.args[:2]
    assert (domain, service) == ("tts", "speak")


async def test_announce_via_tts_with_language_and_voice():
    c = make_coordinator(entry=_entry_with_tts(tts_language="en-GB", tts_voice="RyanNeural"))
    _wire_tts(c)
    c.voice_announcements = True
    await c._announce_via_tts("Best move")
    data = c.hass.services.async_call.call_args.args[2]
    assert data["language"] == "en-GB"
    assert data["options"] == {"voice": "RyanNeural"}


async def test_announce_via_tts_no_tts_configured_only_event():
    c = make_coordinator(entry=types.SimpleNamespace(options={}))
    _wire_tts(c)
    c.voice_announcements = True
    await c._announce_via_tts("Mate")
    c.hass.bus.async_fire.assert_called_once()
    c.hass.services.async_call.assert_not_called()


async def test_announce_via_tts_bad_service_format_swallowed():
    # tts_service without a dot -> ValueError caught, no crash, no call.
    c = make_coordinator(
        entry=_entry_with_tts(tts_service="notadotservice")
    )
    _wire_tts(c)
    c.voice_announcements = True
    await c._announce_via_tts("Hi")
    c.hass.services.async_call.assert_not_called()


async def test_announce_via_tts_event_fire_failure_is_swallowed():
    c = make_coordinator(entry=types.SimpleNamespace(options={}))
    c.hass.bus = MagicMock()
    c.hass.bus.async_fire = MagicMock(side_effect=RuntimeError("boom"))
    c.hass.services = MagicMock()
    c.hass.services.async_call = AsyncMock()
    c.voice_announcements = False
    # Should not raise despite the event fire blowing up.
    await c._announce_via_tts("something")


# ── _should_announce_active_game ─────────────────────────────────────────────


async def test_should_announce_active_game_true():
    c = make_coordinator()
    c._sculpture_active = False
    c._state["game_status"] = coord_mod.STATUS_PLAYING
    assert c._should_announce_active_game() is True


async def test_should_announce_active_game_sculpture_false():
    c = make_coordinator()
    c._sculpture_active = True
    c._state["game_status"] = coord_mod.STATUS_PLAYING
    assert c._should_announce_active_game() is False


async def test_should_announce_active_game_not_playing_false():
    c = make_coordinator()
    c._sculpture_active = False
    c._state["game_status"] = "idle"
    assert c._should_announce_active_game() is False


# ── _analyze_starting_position ───────────────────────────────────────────────


async def test_analyze_starting_position_no_client_noop():
    c = make_coordinator()
    c._analysis_client = None
    await c._analyze_starting_position()  # no crash
    assert c._state.get("eval_cp") is None


async def test_analyze_starting_position_populates_eval_and_opening():
    c = make_coordinator()
    c._analysis_client = MagicMock()
    ev = EvalResult(cp=20, mate=None, depth=30, best_uci="e2e4", source="lichess-cloud")
    c._analysis_client.get_eval = AsyncMock(return_value=ev)
    c._analysis_client.get_opening = AsyncMock(return_value=("King's Pawn", "B00"))
    await c._analyze_starting_position()
    assert c._state["eval_cp"] == 20
    assert c._state["eval_depth"] == 30
    assert c._state["eval_source"] == "lichess-cloud"
    assert c._state["best_move_san"] == "e4"
    assert c._state["opening_name"] == "King's Pawn"
    assert c._state["opening_eco"] == "B00"
    c.async_set_updated_data.assert_called()


async def test_analyze_starting_position_illegal_best_uci_skips_san():
    c = make_coordinator()
    c._analysis_client = MagicMock()
    # best_uci that is not legal from the start position.
    ev = EvalResult(cp=0, mate=None, depth=1, best_uci="e2e5", source="stub")
    c._analysis_client.get_eval = AsyncMock(return_value=ev)
    c._analysis_client.get_opening = AsyncMock(return_value=(None, None))
    await c._analyze_starting_position()
    assert c._state.get("best_move_san") is None


async def test_analyze_starting_position_eval_none():
    c = make_coordinator()
    c._analysis_client = MagicMock()
    c._analysis_client.get_eval = AsyncMock(return_value=None)
    c._analysis_client.get_opening = AsyncMock(return_value=(None, None))
    await c._analyze_starting_position()
    assert c._state.get("eval_cp") is None


async def test_analyze_starting_position_exception_swallowed():
    c = make_coordinator()
    c._analysis_client = MagicMock()
    c._analysis_client.get_eval = AsyncMock(side_effect=RuntimeError("net"))
    await c._analyze_starting_position()  # must not raise


# ── _analyze_move ────────────────────────────────────────────────────────────


async def test_analyze_move_no_client_noop():
    c = make_coordinator()
    c._analysis_client = None
    await c._analyze_move(0, chess.Board(), chess.Board(), chess.Move.from_uci("e2e4"), True)


async def test_analyze_move_updates_history_and_eval():
    c = make_coordinator()
    c._our_color = chess.WHITE
    c._analysis_client = MagicMock()
    pre = EvalResult(cp=20, mate=None, depth=20, best_uci="e2e4", source="lichess-cloud")
    post = EvalResult(cp=15, mate=None, depth=20, best_uci="e7e5", source="lichess-cloud")
    c._analysis_client.get_eval = AsyncMock(side_effect=[pre, post])
    c._analysis_client.get_opening = AsyncMock(return_value=("King's Pawn", "B00"))

    board_before = chess.Board()
    board_after = chess.Board()
    move = chess.Move.from_uci("e2e4")
    board_after.push(move)

    # Seed a single history slot at ply 0 so the in-place update path runs.
    c._state["move_history_moves"] = [{"side": "white", "san": "e4"}]

    await c._analyze_move(0, board_before, board_after, move, mover_is_white=True)

    entry = c._state["move_history_moves"][0]
    assert "classification" in entry
    assert "cpl" in entry
    # ply 0 is the last -> last_move_* strip populated.
    assert c._state["last_move_classification"] == entry["classification"]
    # post-move eval surfaced.
    assert c._state["eval_cp"] == 15
    assert c._state["eval_source"] == "lichess-cloud"
    c.async_set_updated_data.assert_called()


async def test_analyze_move_exception_swallowed():
    c = make_coordinator()
    c._analysis_client = MagicMock()
    c._analysis_client.get_eval = AsyncMock(side_effect=RuntimeError("fail"))
    board = chess.Board()
    after = chess.Board()
    mv = chess.Move.from_uci("e2e4")
    after.push(mv)
    # must not raise
    await c._analyze_move(0, board, after, mv, True)


# ── _maybe_announce_classification ───────────────────────────────────────────


async def test_maybe_announce_blunder_speaks_pawns():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = True
    c.training_wheels = False
    await c._maybe_announce_classification(const.CLASSIFICATION_BLUNDER, 300, "")
    msg = c.hass.services.async_call.call_args.args[2]["message"]
    assert "Blunder" in msg
    assert "3.0 pawns" in msg


async def test_maybe_announce_blunder_mate_transition():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = True
    await c._maybe_announce_classification(const.CLASSIFICATION_BLUNDER, 9999, "")
    msg = c.hass.services.async_call.call_args.args[2]["message"]
    assert "forced mate" in msg


async def test_maybe_announce_mistake():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = True
    await c._maybe_announce_classification(const.CLASSIFICATION_MISTAKE, 150, "")
    msg = c.hass.services.async_call.call_args.args[2]["message"]
    assert "Mistake" in msg


async def test_maybe_announce_good_silent_without_training_wheels():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = True
    c.training_wheels = False
    await c._maybe_announce_classification(const.CLASSIFICATION_GOOD, 5, "")
    c.hass.services.async_call.assert_not_called()


async def test_maybe_announce_good_verbose_with_training_wheels():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = True
    c.training_wheels = True
    await c._maybe_announce_classification(const.CLASSIFICATION_GOOD, 5, "")
    msg = c.hass.services.async_call.call_args.args[2]["message"]
    assert msg == "Good move."


async def test_maybe_announce_best_verbose():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = True
    c.training_wheels = True
    await c._maybe_announce_classification(const.CLASSIFICATION_BEST, 0, "")
    msg = c.hass.services.async_call.call_args.args[2]["message"]
    assert msg == "Best move."


async def test_maybe_announce_inaccuracy_verbose():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = True
    c.training_wheels = True
    await c._maybe_announce_classification(const.CLASSIFICATION_INACCURACY, 60, "")
    msg = c.hass.services.async_call.call_args.args[2]["message"]
    assert "inaccuracy" in msg.lower()


async def test_maybe_announce_fork_suffix():
    c = make_coordinator(entry=_entry_with_tts())
    _wire_tts(c)
    c.voice_announcements = True
    await c._maybe_announce_classification(const.CLASSIFICATION_MISTAKE, 150, "fork")
    msg = c.hass.services.async_call.call_args.args[2]["message"]
    assert "Watch for forks here." in msg


# ── _coarse_accuracy (staticmethod) ──────────────────────────────────────────


def test_coarse_accuracy_empty_none():
    assert PhantomChessCoordinator._coarse_accuracy([]) is None


def test_coarse_accuracy_low_cpl_high_score():
    # mean 50 -> 100 - 25 = 75.0
    assert PhantomChessCoordinator._coarse_accuracy([50, 50]) == 75.0


def test_coarse_accuracy_clamped_to_zero():
    # mean 400 -> 100 - 200 -> clamped 0.0
    assert PhantomChessCoordinator._coarse_accuracy([400]) == 0.0


def test_coarse_accuracy_perfect_clamped_to_100():
    assert PhantomChessCoordinator._coarse_accuracy([0, 0]) == 100.0


# ── _describe_mistake (staticmethod) ─────────────────────────────────────────


def test_describe_mistake_fork():
    out = PhantomChessCoordinator._describe_mistake({"motif": "fork"})
    assert out.startswith("Allowed a fork")


def test_describe_mistake_mate_transition():
    out = PhantomChessCoordinator._describe_mistake(
        {"classification": "blunder", "cpl": 9999}
    )
    assert "forced mate" in out


def test_describe_mistake_blunder_with_best():
    out = PhantomChessCoordinator._describe_mistake(
        {"classification": "blunder", "cpl": 500, "best_san": "Nf3"}
    )
    assert "Major drop" in out
    assert "Engine preferred Nf3." in out


def test_describe_mistake_mistake():
    out = PhantomChessCoordinator._describe_mistake(
        {"classification": "mistake", "cpl": 150}
    )
    assert "Lost material" in out


def test_describe_mistake_inaccuracy():
    out = PhantomChessCoordinator._describe_mistake(
        {"classification": "inaccuracy", "cpl": 60}
    )
    assert "sharper plan" in out


def test_describe_mistake_unknown_blank():
    assert PhantomChessCoordinator._describe_mistake({}) == ""


# ── _build_post_game_review ──────────────────────────────────────────────────


async def test_build_post_game_review_empty_history():
    c = make_coordinator()
    c._our_color = chess.WHITE
    c._state["move_history_moves"] = []
    await c._build_post_game_review()
    assert c._state["lichess_review_ready"] is False


async def test_build_post_game_review_ranks_mistakes():
    c = make_coordinator()
    c._our_color = chess.WHITE
    c._state["move_history_moves"] = [
        {"side": "white", "classification": "blunder", "cpl": 400, "san": "a3"},
        {"side": "black", "classification": "mistake", "cpl": 200, "san": "h6"},
        {"side": "white", "classification": "inaccuracy", "cpl": 60, "san": "b3"},
        {"side": "white", "classification": "good", "cpl": 5, "san": "Nf3"},
    ]
    await c._build_post_game_review()
    assert c._state["lichess_review_ready"] is True
    top = c._state["last_game_top_mistakes"]
    # Only white's inaccuracy/mistake/blunder; sorted by cpl desc.
    assert [m["san"] for m in top] == ["a3", "b3"]
    assert top[0]["description"]  # populated
    # accuracy computed for both colors.
    assert c._state["last_game_accuracy_white"] is not None
    assert c._state["last_game_accuracy_black"] is not None


async def test_build_post_game_review_black_perspective():
    c = make_coordinator()
    c._our_color = chess.BLACK
    c._state["move_history_moves"] = [
        {"side": "black", "classification": "blunder", "cpl": 300, "san": "Qh4"},
        {"side": "white", "classification": "blunder", "cpl": 999, "san": "g4"},
    ]
    await c._build_post_game_review()
    top = c._state["last_game_top_mistakes"]
    assert [m["san"] for m in top] == ["Qh4"]  # only black's move


# ── async_dismiss_review ─────────────────────────────────────────────────────


async def test_async_dismiss_review_clears_flags():
    c = make_coordinator()
    c._state["lichess_active"] = True
    c._state["lichess_review_ready"] = True
    c._game_id = "abc123"
    c._lichess_task = None
    await c.async_dismiss_review()
    assert c._state["lichess_active"] is False
    assert c._state["lichess_review_ready"] is False
    assert c._game_id is None
    c.async_set_updated_data.assert_called()


async def test_async_dismiss_review_cancels_live_task():
    c = make_coordinator()
    task = MagicMock()
    task.done.return_value = False
    c._lichess_task = task
    await c.async_dismiss_review()
    task.cancel.assert_called_once()
    assert c._lichess_task is None


# ── async_request_hint ───────────────────────────────────────────────────────


async def test_async_request_hint_no_client_noop():
    c = make_coordinator()
    c._analysis_client = None
    await c.async_request_hint()  # no crash


async def test_async_request_hint_populates_best_move():
    c = make_coordinator()
    c._board = chess.Board()
    c._analysis_client = MagicMock()
    ev = EvalResult(cp=30, mate=None, depth=25, best_uci="e2e4", source="lichess-cloud")
    c._analysis_client.get_eval = AsyncMock(return_value=ev)
    await c.async_request_hint()
    assert c._state["eval_cp"] == 30
    assert c._state["best_move_san"] == "e4"
    c.async_set_updated_data.assert_called()


async def test_async_request_hint_no_eval_returns():
    c = make_coordinator()
    c._board = chess.Board()
    c._analysis_client = MagicMock()
    c._analysis_client.get_eval = AsyncMock(return_value=None)
    await c.async_request_hint()
    # nothing set
    assert c._state.get("eval_cp") is None


async def test_async_request_hint_illegal_best_uci_skips_san():
    c = make_coordinator()
    c._board = chess.Board()
    c._analysis_client = MagicMock()
    ev = EvalResult(cp=0, mate=None, depth=1, best_uci="e2e5", source="stub")
    c._analysis_client.get_eval = AsyncMock(return_value=ev)
    await c.async_request_hint()
    assert c._state.get("best_move_san") is None


async def test_async_request_hint_exception_swallowed():
    c = make_coordinator()
    c._board = chess.Board()
    c._analysis_client = MagicMock()
    c._analysis_client.get_eval = AsyncMock(side_effect=RuntimeError("net"))
    await c.async_request_hint()  # must not raise

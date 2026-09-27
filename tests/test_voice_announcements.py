"""Voice-announcements master mute (v0.4-beta3).

``_announce_via_tts`` must:
  - ALWAYS fire the ``phantom_chess_announce`` event (carrying the
    current ``voice_enabled`` flag), so event-driven automations keep
    working regardless of the toggle.
  - Only perform the direct ``tts.speak`` call (the spoken voiceover)
    when ``coordinator.voice_announcements`` is True.

The method is async and only touches a handful of attributes, so — like
the two-player PGN tests — we bind the unbound method to a lightweight
stub and drive it directly. Runs in the minimal CI env (no board, no
real HA services).
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

import chess

import custom_components.phantom_chess.lichess_analysis as la
from custom_components.phantom_chess.coordinator import PhantomChessCoordinator


def _stub(voice_on: bool) -> types.SimpleNamespace:
    """Stub carrying just what ``_announce_via_tts`` reads/calls.

    Options include a configured TTS engine + speaker so the direct
    ``tts.speak`` path is *eligible* — whether it actually fires is then
    governed solely by ``voice_announcements``.
    """
    stub = types.SimpleNamespace()
    stub.voice_announcements = voice_on
    stub._ble_address = "C8:C9:A3:F2:7C:0A"
    stub._entry = types.SimpleNamespace(
        options={
            "tts_service": "tts.home_assistant_cloud",
            "tts_media_player_entity_id": "media_player.living_room_voice",
        }
    )
    stub.hass = types.SimpleNamespace(
        bus=types.SimpleNamespace(async_fire=MagicMock()),
        services=types.SimpleNamespace(async_call=AsyncMock()),
    )
    stub._state = {}
    stub.async_set_updated_data = MagicMock()
    return stub


def test_generic_tts_failure_is_surfaced_then_cleared() -> None:
    stub = _stub(voice_on=True)
    stub.hass.services.async_call.side_effect = RuntimeError("Entity not found")
    asyncio.run(PhantomChessCoordinator._announce_via_tts(stub, "Check"))
    assert stub._state["speech_error"] == "Speech unavailable: Entity not found"
    assert stub.hass.services.async_call.call_args.kwargs["blocking"] is True
    stub.hass.services.async_call.side_effect = None
    asyncio.run(PhantomChessCoordinator._announce_via_tts(stub, "Check"))
    assert stub._state["speech_error"] is None


def test_mute_suppresses_tts_but_still_fires_event() -> None:
    stub = _stub(voice_on=False)
    asyncio.run(PhantomChessCoordinator._announce_via_tts(stub, "Knight takes e5"))

    # Event always fires, with voice_enabled reflecting the muted state.
    stub.hass.bus.async_fire.assert_called_once()
    event_name, payload = stub.hass.bus.async_fire.call_args.args
    assert event_name == "phantom_chess_announce"
    assert payload["message"] == "Knight takes e5"
    assert payload["voice_enabled"] is False
    # The spoken voiceover is suppressed.
    stub.hass.services.async_call.assert_not_called()


def test_unmuted_speaks_via_tts() -> None:
    stub = _stub(voice_on=True)
    asyncio.run(PhantomChessCoordinator._announce_via_tts(stub, "Check"))

    stub.hass.bus.async_fire.assert_called_once()
    _, payload = stub.hass.bus.async_fire.call_args.args
    assert payload["voice_enabled"] is True
    # tts.speak is dispatched exactly once.
    stub.hass.services.async_call.assert_called_once()
    domain, service = stub.hass.services.async_call.call_args.args[:2]
    assert (domain, service) == ("tts", "speak")


def test_empty_message_is_a_noop() -> None:
    stub = _stub(voice_on=True)
    asyncio.run(PhantomChessCoordinator._announce_via_tts(stub, ""))
    stub.hass.bus.async_fire.assert_not_called()
    stub.hass.services.async_call.assert_not_called()


# ── Color attribution (VOICE_BRIEF, Luke 2026-07-08) ────────────────────
#
# "you" is reserved for the human player in a human-vs-AI game. Every other
# announcement names the color: "Best move by White", "Blunder by Black."
# Two levels of coverage:
#   1. Phrasing — drive ``_maybe_announce_classification`` directly and
#      inspect the spoken string for each classification/mode.
#   2. Gate — drive the real ``_analyze_move`` and prove mode detection
#      picks the right attribution, including the live stale-``_our_color``
#      bug in AI-vs-AI / sculpture / two-player.


def _classify_stub(training_wheels: bool) -> types.SimpleNamespace:
    """Stub carrying just what ``_maybe_announce_classification`` reads.

    ``_announce_via_tts`` is replaced with an AsyncMock so the spoken
    string is captured without touching the event/TTS machinery.
    """
    stub = types.SimpleNamespace()
    stub.training_wheels = training_wheels
    stub._announce_via_tts = AsyncMock()
    return stub


def _spoken(stub: types.SimpleNamespace) -> str | None:
    """The single message passed to ``_announce_via_tts``, or None."""
    if not stub._announce_via_tts.await_args_list:
        return None
    return stub._announce_via_tts.await_args.args[0]


def _announce(
    *, classification: str, cpl: int, motif: str = "",
    mover_is_white: bool, you_case: bool, training_wheels: bool = False,
) -> str | None:
    stub = _classify_stub(training_wheels)
    asyncio.run(PhantomChessCoordinator._maybe_announce_classification(
        stub, classification, cpl, motif,
        mover_is_white=mover_is_white, you_case=you_case,
    ))
    return _spoken(stub)


# --- Phrasing: human-vs-AI (you-case) is unchanged wording ---------------


def test_you_case_blunder_unchanged() -> None:
    msg = _announce(
        classification="blunder", cpl=180, mover_is_white=True, you_case=True,
    )
    assert msg == "Blunder. The evaluation dropped by about 1.8 pawns."


def test_you_case_best_move_unchanged() -> None:
    msg = _announce(
        classification="best", cpl=0, mover_is_white=False, you_case=True,
        training_wheels=True,
    )
    assert msg == "Best move."


def test_you_case_mate_transition_unchanged() -> None:
    msg = _announce(
        classification="blunder", cpl=9999, mover_is_white=True, you_case=True,
    )
    assert msg == "Blunder. The evaluation changed sharply."


# --- Phrasing: attributed (human-free) modes name the color --------------


def test_attributed_blunder_by_white() -> None:
    msg = _announce(
        classification="blunder", cpl=510, mover_is_white=True, you_case=False,
    )
    assert msg == "Blunder by White. The evaluation dropped by about 5.1 pawns."
    assert "You" not in msg
    # No-comma-around-names rule.
    assert "Blunder," not in msg


def test_attributed_mistake_by_black() -> None:
    msg = _announce(
        classification="mistake", cpl=230, mover_is_white=False, you_case=False,
    )
    assert msg == "Mistake by Black. The evaluation dropped by about 2.3 pawns."


def test_attributed_inaccuracy_by_white_verbose() -> None:
    msg = _announce(
        classification="inaccuracy", cpl=180, mover_is_white=True,
        you_case=False, training_wheels=True,
    )
    assert msg == "Slight inaccuracy by White. The evaluation dropped by about 1.8 pawns."


def test_attributed_best_and_good_need_verbose() -> None:
    # training_wheels OFF → best/good/inaccuracy stay silent in every mode.
    assert _announce(
        classification="best", cpl=0, mover_is_white=True, you_case=False,
    ) is None
    assert _announce(
        classification="good", cpl=20, mover_is_white=False, you_case=False,
    ) is None
    # ON → attributed.
    assert _announce(
        classification="best", cpl=0, mover_is_white=True, you_case=False,
        training_wheels=True,
    ) == "Best move by White."
    assert _announce(
        classification="good", cpl=20, mover_is_white=False, you_case=False,
        training_wheels=True,
    ) == "Good move by Black."


def test_attributed_mate_transition_by_black() -> None:
    msg = _announce(
        classification="blunder", cpl=9999, mover_is_white=False,
        you_case=False,
    )
    assert msg == "Blunder by Black. The evaluation changed sharply."
    assert "You" not in msg


def test_attributed_fork_suffix_preserved() -> None:
    msg = _announce(
        classification="blunder", cpl=510, motif="fork",
        mover_is_white=True, you_case=False,
    )
    assert msg == "Blunder by White. The evaluation dropped by about 5.1 pawns. Watch for forks here."


# --- Gate: mode detection in the real ``_analyze_move`` ------------------


class _FakeAnalysisClient:
    """Minimal analysis client. Evals resolve to None (the sensor-refresh
    branch is skipped); ``classify_move`` is monkeypatched per test."""

    async def get_eval(self, _fen: str) -> None:
        return None

    async def get_opening(self, _fen: str) -> tuple[None, None]:
        return (None, None)


def _analyze_stub(
    *, our_color: bool, sculpture: bool = False, ai_vs_ai: bool = False,
    two_player: bool = False, training_wheels: bool = False,
    lichess_game_id: str | None = None,
) -> types.SimpleNamespace:
    """Stub for driving the real ``_analyze_move`` gate end-to-end. The real
    ``_maybe_announce_classification`` runs, so the captured string is the
    exact spoken text; only ``_announce_via_tts`` is mocked."""
    stub = types.SimpleNamespace()
    stub._board = chess.Board()
    stub._analysis_client = _FakeAnalysisClient()
    stub._state = {
        "move_history_moves": [{}],
        "lichess_game_id": lichess_game_id,
    }
    stub._our_color = our_color
    stub._sculpture_active = sculpture
    stub._ai_vs_ai_active = ai_vs_ai
    stub._two_player_active = two_player
    stub.training_wheels = training_wheels
    stub.async_set_updated_data = MagicMock()
    stub._announce_via_tts = AsyncMock()
    # Bind the real classification method so phrasing flows through the gate.
    stub._maybe_announce_classification = types.MethodType(
        PhantomChessCoordinator._maybe_announce_classification, stub,
    )
    return stub


def _run_analyze(
    stub: types.SimpleNamespace, *, classification: str, cpl: int,
    mover_is_white: bool, monkeypatch,
) -> str | None:
    """Drive ``_analyze_move`` for one ply with a controlled classification,
    returning the spoken string (or None)."""
    monkeypatch.setattr(la, "classify_move", lambda *a, **k: (classification, cpl))
    monkeypatch.setattr(la, "classification_color_glyph", lambda c: ("gray", "?"))
    monkeypatch.setattr(la, "detect_fork", lambda *a, **k: False)

    async def _no_threat(*_a, **_k):
        return None

    monkeypatch.setattr(la, "compute_threat_san", _no_threat)

    board_before = chess.Board()
    if not mover_is_white:
        board_before.push(chess.Move.from_uci("e2e4"))
        move = chess.Move.from_uci("e7e5")
    else:
        move = chess.Move.from_uci("e2e4")
    board_after = board_before.copy()
    board_after.push(move)

    asyncio.run(PhantomChessCoordinator._analyze_move(
        stub, 0, board_before, board_after, move, mover_is_white,
    ))
    if not stub._announce_via_tts.await_args_list:
        return None
    return stub._announce_via_tts.await_args.args[0]


def test_gate_sculpture_white_blunder_attributed(monkeypatch) -> None:
    # Sculpture active; stale _our_color left on BLACK from a prior game.
    stub = _analyze_stub(our_color=chess.BLACK, sculpture=True)
    msg = _run_analyze(
        stub, classification="blunder", cpl=510, mover_is_white=True,
        monkeypatch=monkeypatch,
    )
    assert msg == "Blunder by White. The evaluation dropped by about 5.1 pawns."
    assert "You" not in msg


def test_gate_sculpture_via_lichess_game_id_flag(monkeypatch) -> None:
    # Sculpture detected purely via the state flag, not _sculpture_active.
    stub = _analyze_stub(
        our_color=chess.WHITE, sculpture=False, lichess_game_id="sculpture",
        training_wheels=True,
    )
    msg = _run_analyze(
        stub, classification="best", cpl=0, mover_is_white=False,
        monkeypatch=monkeypatch,
    )
    assert msg == "Best move by Black."


def test_gate_two_player_both_colors_announce(monkeypatch) -> None:
    # White mover.
    white = _analyze_stub(our_color=chess.WHITE, two_player=True)
    assert _run_analyze(
        white, classification="blunder", cpl=510, mover_is_white=True,
        monkeypatch=monkeypatch,
    ) == "Blunder by White. The evaluation dropped by about 5.1 pawns."
    # Black mover — the OTHER human is no longer silent.
    black = _analyze_stub(our_color=chess.WHITE, two_player=True)
    assert _run_analyze(
        black, classification="mistake", cpl=230, mover_is_white=False,
        monkeypatch=monkeypatch,
    ) == "Mistake by Black. The evaluation dropped by about 2.3 pawns."


def test_gate_ai_vs_ai_stale_our_color_never_says_you(monkeypatch) -> None:
    # The exact live bug: AI-vs-AI, _our_color still matches the mover from
    # a prior human game. Old code would have said "You lost…"; the mode
    # check must win and attribute by color instead.
    stub = _analyze_stub(our_color=chess.WHITE, ai_vs_ai=True)
    msg = _run_analyze(
        stub, classification="blunder", cpl=180, mover_is_white=True,
        monkeypatch=monkeypatch,
    )
    assert msg == "Blunder by White. The evaluation dropped by about 1.8 pawns."
    assert "You" not in msg
    # And the opposing engine color also announces (attributed).
    other = _analyze_stub(our_color=chess.WHITE, ai_vs_ai=True)
    assert _run_analyze(
        other, classification="mistake", cpl=230, mover_is_white=False,
        monkeypatch=monkeypatch,
    ) == "Mistake by Black. The evaluation dropped by about 2.3 pawns."


def test_gate_human_vs_ai_human_move_says_you(monkeypatch) -> None:
    # game_id set (Lichess), human is White, White moves → "You lost…".
    stub = _analyze_stub(our_color=chess.WHITE, lichess_game_id="abc123")
    msg = _run_analyze(
        stub, classification="blunder", cpl=180, mover_is_white=True,
        monkeypatch=monkeypatch,
    )
    assert msg == "Blunder. The evaluation dropped by about 1.8 pawns."


def test_gate_human_vs_ai_opponent_move_silent(monkeypatch) -> None:
    # Human is White; the AI (Black) blunders → stays UNANNOUNCED.
    stub = _analyze_stub(our_color=chess.WHITE, lichess_game_id="abc123")
    msg = _run_analyze(
        stub, classification="blunder", cpl=510, mover_is_white=False,
        monkeypatch=monkeypatch,
    )
    assert msg is None


def test_gate_mate_transition_attributed(monkeypatch) -> None:
    stub = _analyze_stub(our_color=chess.WHITE, ai_vs_ai=True)
    msg = _run_analyze(
        stub, classification="blunder", cpl=9999, mover_is_white=False,
        monkeypatch=monkeypatch,
    )
    assert msg == "Blunder by Black. The evaluation changed sharply."
    assert "You" not in msg

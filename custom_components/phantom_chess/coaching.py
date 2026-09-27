"""Move analysis, grading, post-game review, hints and spoken announcements.

Methods of PhantomChessCoordinator, defined here as functions and bound
onto the class in coordinator.py. Moved verbatim from coordinator.py.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .coordinator import PhantomChessCoordinator  # noqa: F401

import chess


from .runtime import (  # noqa: F401 — shared names
    AI_VS_AI_TWO_STEP_SETTLE_S,
    BleakClient,
    BleakError,
    BleakGATTCharacteristic,
    _phantom_to_uci,
    _rotate_uci_180,
    _is_move_frame,
    _build_matrix_from_fen_module,
    _check_consistency,
    _diff_grid_vs_sensor,
    _format_mismatch_instructions,
    _grid_to_fen,
    _parse_matrix_notification,
)

from .const import (
    DOMAIN,
    STATUS_PLAYING,
    STATUS_CHECK,
)

# Same logger as before the split, so log filters keep working.
_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".coordinator")


def _build_move_speech(self, mv: chess.Move) -> str:
    """Describe a move in natural English. Call BEFORE pushing the move
    (uses self._board's pre-move state to detect capture / castle / piece type).
    Returns '' if move is malformed.
    """
    piece = self._board.piece_at(mv.from_square)
    if piece is None:
        return ""
    side = "White" if self._board.turn == chess.WHITE else "Black"
    if self._board.is_castling(mv):
        castle_side = "kingside" if chess.square_file(mv.to_square) > 4 else "queenside"
        return f"{side} castles {castle_side}"
    piece_name = self._PIECE_NAMES.get(piece.piece_type, "piece")
    to_sq = chess.square_name(mv.to_square)
    verb = "takes" if self._board.is_capture(mv) else "to"
    return f"{side} {piece_name} {verb} {to_sq}"


def _post_move_event_speech(self) -> str:
    """Describe post-move events (check/mate/stalemate). Call AFTER push.
    Returns '' if none apply. self._board.turn is the side NOW to move
    (i.e. the side whose king might be in check)."""
    if self._board.is_checkmate():
        winner = "Black" if self._board.turn == chess.WHITE else "White"
        return f"Checkmate. {winner} wins."
    if self._board.is_stalemate():
        return "Stalemate. Draw."
    if self._board.is_check():
        in_check = "White" if self._board.turn == chess.WHITE else "Black"
        return f"Check on {in_check}."
    return ""


async def _announce_via_tts(self, message: str) -> None:
    """Emit the announcement event and deliver configured speech.

    Events include voice-enabled and managed-delivery flags so forwarding
    automations can honor mute and avoid duplicate playback. Direct speech
    honors the voice toggle. Managed HomePod playback is awaited and
    publishes failures on the dashboard; generic TTS is also supported.
    """
    if not message:
        return

    entry = getattr(self, "_entry", None)
    options = (entry.options if entry is not None else {}) or {}
    managed = bool(options.get("homepod_speech"))
    # ── Event fan-out (always) ──────────────────────────────────────
    # The event ALWAYS fires (even when the spoken voiceover is muted)
    # so event-driven automations keep working; the `voice_enabled`
    # flag lets them honour the dashboard toggle if they choose.
    try:
        self.hass.bus.async_fire(
            "phantom_chess_announce",
            {
                "message": message,
                "board_address": self._ble_address,
                "voice_enabled": bool(self.voice_announcements),
                "delivery_managed": managed,
            },
        )
    except Exception as ev_err:
        _LOGGER.debug("phantom_chess_announce event fire failed: %s", ev_err)

    # ── Master mute for the spoken voiceover (v0.4-beta3) ───────────
    # The Voice-announcements dashboard switch gates managed and generic
    # speech below, across every mode (AI, Stockfish, Lichess,
    # 2-player, and historic — all of which reach the user's TTS
    # through this one method). The event above still fired.
    if not self.voice_announcements:
        _LOGGER.debug("Voice announcements muted — skipping TTS: %s", message)
        return

    if managed:
        try:
            await self.hass.services.async_call(
                DOMAIN, "speak_homepod",
                {"media_player_entity_id": options.get("tts_media_player_entity_id"),
                 "message": message, "volume_level": options.get("speech_volume", 0.8)},
                blocking=True,
            )
            self._state["speech_error"] = None
        except Exception as err:
            self._state["speech_error"] = f"Speech unavailable: {err}"
            _LOGGER.warning("Phantom speech failed: %s", err)
        self.async_set_updated_data(dict(self._state))
        return

    # ── Optional direct TTS call (when configured in options) ──────
    # Two-step null-check so mypy can narrow `self._entry` past
    # the truthy guard before we touch `.options`.
    entry = getattr(self, "_entry", None)
    options = (entry.options if entry is not None else {}) or {}
    tts_service = options.get("tts_service")
    tts_media_player = options.get("tts_media_player_entity_id")
    # Optional voice pinning. When set, these force a specific language
    # and voice instead of the engine's default. For Home Assistant Cloud
    # that's a locale (e.g. "en-GB") plus a short Azure neural voice name
    # (e.g. "RyanNeural"), passed as options.voice. Both are optional and
    # independent, so existing setups (no language/voice) are unaffected.
    tts_language = options.get("tts_language")
    tts_voice = options.get("tts_voice")
    if tts_service and tts_media_player:
        # tts_service is "domain.service" e.g. "tts.google_ai_tts" — split
        # into ("tts", "google_ai_tts") for the service call. We don't
        # validate the format here; misconfiguration surfaces in HA logs.
        try:
            domain, _, service = tts_service.partition(".")
            if not domain or not service:
                raise ValueError(f"tts_service must be 'domain.service', got {tts_service!r}")
            service_data: dict[str, Any] = {
                "entity_id": tts_service,
                "media_player_entity_id": tts_media_player,
                "message": message,
                "cache": True,
            }
            if tts_language:
                service_data["language"] = tts_language
            if tts_voice:
                service_data["options"] = {"voice": tts_voice}
            # Awaited like managed speech so a wrong engine, voice or
            # player reaches the dashboard instead of failing silently.
            await self.hass.services.async_call(
                "tts", "speak",
                service_data,
                blocking=True,
            )
            _LOGGER.debug("TTS dispatched via %s → %s (lang=%s voice=%s): %s",
                          tts_service, tts_media_player,
                          tts_language or "default", tts_voice or "default",
                          message)
            if self._state.get("speech_error"):
                self._state["speech_error"] = None
                self.async_set_updated_data(dict(self._state))
        except Exception as e:
            self._state["speech_error"] = f"Speech unavailable: {e}"
            _LOGGER.warning("Phantom speech via %s failed: %s", tts_service, e)
            self.async_set_updated_data(dict(self._state))


def _should_announce_active_game(self) -> bool:
    """True if we're in an active Lichess or local Stockfish game.
    Sculpture playback has its own TTS path and shouldn't double-announce."""
    # Sculpture playback drives the board through STATUS_PLAYING too (so
    # the analysis pipeline + learning view populate), but it announces
    # only an intro + result — not every ply — so a long historic game
    # (e.g. the 271-ply 2021 WCC marathon) doesn't spam TTS.
    if self._sculpture_active:
        return False
    # Check is still active play. A reply after check must not lose its
    # move announcement or the terminal result appended after execution.
    return self._state.get("game_status") in (STATUS_PLAYING, STATUS_CHECK)


def _record_history_stub(self, move: chess.Move, mover_color: chess.Color) -> int:
    """Append a placeholder entry to move_history_moves; return its index.

    The placeholder shows "unknown" classification until the async
    _analyze_move task fills in CPL, label, motif, color, glyph.
    """
    from .lichess_analysis import classification_color_glyph
    # Compute SAN from a copy of the analysis board *before* it's pushed.
    # (Caller pushes immediately after calling this.)
    try:
        san = self._analysis_board.san(move)
    except (chess.IllegalMoveError, AssertionError, ValueError):
        san = move.uci()
    side = "white" if mover_color == chess.WHITE else "black"
    ply = (self._state.get("move_history_moves") or [])
    move_num = (len(ply) // 2) + 1
    color, glyph = classification_color_glyph("unknown")
    entry = {
        "ply": len(ply) + 1,
        "move_num": move_num,
        "side": side,
        "san": san,
        "uci": move.uci(),
        "classification": "unknown",
        "cpl": 0,
        "motif": "",
        "color": color,
        "glyph": glyph,
    }
    new_history = list(ply)
    new_history.append(entry)
    self._state["move_history_moves"] = new_history
    return len(new_history) - 1


async def _analyze_starting_position(self) -> None:
    """Fetch initial eval + opening name for a brand-new game."""
    if self._analysis_client is None:
        return
    try:
        owner = self._board
        board = owner.copy()
        ev = await self._analysis_client.get_eval(board.fen())
        if self._board is not owner or self._board.fen() != board.fen():
            return
        if ev is not None:
            self._state["eval_fen"] = board.fen()
            self._state["eval_cp"] = ev.cp
            self._state["eval_mate"] = ev.mate
            self._state["eval_depth"] = ev.depth
            self._state["eval_source"] = ev.source
            if ev.best_uci:
                try:
                    m = chess.Move.from_uci(ev.best_uci)
                    if m in board.legal_moves:
                        self._state["best_move_san"] = board.san(m)
                except (ValueError, chess.IllegalMoveError):
                    pass
        name, eco = await self._analysis_client.get_opening(board.fen())
        if self._board is not owner or self._board.fen() != board.fen():
            return
        if name:
            self._state["opening_name"] = name
            self._state["opening_eco"] = eco
        self.async_set_updated_data(dict(self._state))
    except Exception as err:
        _LOGGER.debug("Starting-position analysis failed: %s", err)


async def _analyze_move(
    self,
    ply_index: int,
    board_before: chess.Board,
    board_after: chess.Board,
    move: chess.Move,
    mover_is_white: bool,
    session_board: chess.Board | None = None,
) -> None:
    """Background analysis for a single move. Updates move_history_moves
    in-place at ply_index, and refreshes the eval/best-move/threat
    sensors with the POST-move position. Failures degrade gracefully —
    the stub entry stays as 'unknown'."""
    if self._analysis_client is None:
        return
    owner = self._board if session_board is None else session_board
    initial_history = self._state.get("move_history_moves") or []
    if not 0 <= ply_index < len(initial_history):
        return
    entry = initial_history[ply_index]

    def current() -> bool:
        history = self._state.get("move_history_moves") or []
        return (self._board is owner and 0 <= ply_index < len(history)
                and history[ply_index] is entry)

    if not current():
        return
    try:
        from .lichess_analysis import (
            classify_move,
            classification_color_glyph,
            compute_threat_san,
            detect_fork,
        )
        pre_eval = await self._analysis_client.get_eval(board_before.fen())
        if not current():
            return
        post_eval = await self._analysis_client.get_eval(board_after.fen())
        if not current():
            return
        classification, cpl = classify_move(
            pre_eval, post_eval, move.uci(), mover_is_white
        )
        motif = "fork" if detect_fork(board_before, move) else ""
        color, glyph = classification_color_glyph(classification)

        # Update the history entry in-place. The list may have grown since
        # we appended (other moves arrived) — that's OK, we still own our
        # ply_index slot.
        history = list(self._state.get("move_history_moves") or [])
        # Retain the engine's PRE-move recommendation in the entry so the
        # post-game review can answer "what should I have played?" with a
        # concrete SAN rather than the v1 placeholder "—".
        pre_best_san: str | None = None
        if pre_eval is not None and pre_eval.best_uci:
            try:
                pre_best_move = chess.Move.from_uci(pre_eval.best_uci)
                if pre_best_move in board_before.legal_moves:
                    pre_best_san = board_before.san(pre_best_move)
            except (ValueError, chess.IllegalMoveError):
                pre_best_san = None

        if 0 <= ply_index < len(history):
            history[ply_index] = {
                **history[ply_index],
                "classification": classification,
                "cpl": int(cpl),
                "motif": motif,
                "color": color,
                "glyph": glyph,
                "best_san": pre_best_san,
            }
            self._state["move_history_moves"] = history
            entry = history[ply_index]

        # If this is the most-recent move, surface its details to the
        # last-move-detail strip.
        if ply_index == len(history) - 1:
            self._state["last_move_classification"] = classification
            self._state["last_move_cpl"] = int(cpl)
            self._state["last_move_motif"] = motif

        # Refresh top-level eval sensors from the POST-move position.
        if post_eval is not None and ply_index == len(history) - 1:
            self._state["eval_fen"] = board_after.fen()
            self._state["eval_cp"] = post_eval.cp
            self._state["eval_mate"] = post_eval.mate
            self._state["eval_depth"] = post_eval.depth
            self._state["eval_source"] = post_eval.source
            self._state["best_move_san"] = None
            if post_eval.best_uci:
                try:
                    m = chess.Move.from_uci(post_eval.best_uci)
                    if m in board_after.legal_moves:
                        self._state["best_move_san"] = board_after.san(m)
                except (ValueError, chess.IllegalMoveError):
                    pass

        # Threat warning for the side-to-move on the post-move position.
        try:
            threat = await compute_threat_san(
                board_after, self._analysis_client
            )
            if not current():
                return
            if ply_index == len(self._state["move_history_moves"]) - 1:
                self._state["threat_san"] = threat
        except Exception as err:
            _LOGGER.debug("threat detection failed: %s", err)

        # Opening lookup, only while plausibly still in book.
        if board_after.fullmove_number <= 15:
            try:
                name, eco = await self._analysis_client.get_opening(
                    board_after.fen()
                )
                if not current():
                    return
                if name and ply_index == len(self._state["move_history_moves"]) - 1:
                    self._state["opening_name"] = name
                    self._state["opening_eco"] = eco
            except Exception:
                pass  # leave previous value

        # TTS announcement gate. In a human-vs-AI game only the human's
        # own move is narrated, with "you" phrasing. In modes with no
        # human at the board (sculpture playback, AI-vs-AI, two-player
        # recording) BOTH colors are announced, attributed by color and
        # never "you" — "you" is reserved for the human player. The mode
        # check must win over `_our_color`, which retains a stale value
        # from the previous game and would otherwise mis-attribute an
        # engine move as the human's. See VOICE_BRIEF.
        mover_color = chess.WHITE if mover_is_white else chess.BLACK
        sculpture = (
            self._sculpture_active
            or self._state.get("lichess_game_id") == "sculpture"
        )
        if sculpture or self._ai_vs_ai_active or self._two_player_active:
            # No human at the board: announce both colors, attributed.
            await self._maybe_announce_classification(
                classification, int(cpl), motif,
                mover_is_white=mover_is_white, you_case=False,
            )
        elif mover_color == self._our_color:
            # Human-vs-AI, the human's own move.
            await self._maybe_announce_classification(
                classification, int(cpl), motif,
                mover_is_white=mover_is_white, you_case=True,
            )

        self.async_set_updated_data(dict(self._state))
    except Exception as err:
        _LOGGER.debug("Move analysis failed for ply %d: %s", ply_index, err)


async def _maybe_announce_classification(
    self, classification: str, cpl: int, motif: str,
    mover_is_white: bool = True, you_case: bool = True,
) -> None:
    """TTS for move quality.

    Default (training_wheels OFF): announce only mistake/blunder.
    Training wheels ON: announce every classification.

    ``you_case`` selects attribution: True in a human-vs-AI game for the
    human's own move ("Blunder. The evaluation dropped by about 1.8 pawns."), False in
    human-free modes where the move is attributed to its color
    ("Blunder by White. The evaluation dropped by about 1.8 pawns."). ``mover_is_white`` names
    the color for the attributed phrasing. Naming the color also serves
    as timing calibration when speech lags the physical move.
    """
    if (puzzle := getattr(self, "_puzzle", None)) is not None and puzzle.status == "active":
        return  # puzzle mode gives its own right/wrong feedback
    from .lichess_analysis import (
        CLASSIFICATION_BEST, CLASSIFICATION_GOOD, CLASSIFICATION_EXCELLENT,
        CLASSIFICATION_BLUNDER, CLASSIFICATION_MISTAKE,
        CLASSIFICATION_INACCURACY,
    )
    verbose = bool(self.training_wheels)

    # Large sentinel losses can mean either missing a winning mate or
    # allowing a losing one. CPL alone cannot distinguish those events.
    mate_transition = cpl >= 9000
    pawns = round(cpl / 100.0, 1)
    # Attribution prefix for the human-free modes. Respect the TTS
    # no-comma-around-names rule: "Blunder by White." not "Blunder, White."
    by = "White" if mover_is_white else "Black"
    msg: str | None = None
    if classification == CLASSIFICATION_BLUNDER:
        if you_case:
            msg = ("Blunder. The evaluation changed sharply." if mate_transition
                   else f"Blunder. The evaluation dropped by about {pawns} pawns.")
        else:
            msg = (f"Blunder by {by}. The evaluation changed sharply."
                   if mate_transition
                   else f"Blunder by {by}. The evaluation dropped by about {pawns} pawns.")
    elif classification == CLASSIFICATION_MISTAKE:
        if you_case:
            msg = ("Mistake. The evaluation changed sharply." if mate_transition
                   else f"Mistake. The evaluation dropped by about {pawns} pawns.")
        else:
            msg = (f"Mistake by {by}. The evaluation changed sharply."
                   if mate_transition
                   else f"Mistake by {by}. The evaluation dropped by about {pawns} pawns.")
    elif verbose and classification == CLASSIFICATION_INACCURACY:
        msg = (f"Slight inaccuracy. The evaluation dropped by about {pawns} pawns." if you_case
               else f"Slight inaccuracy by {by}. The evaluation dropped by about {pawns} pawns.")
    elif verbose and classification == CLASSIFICATION_BEST:
        msg = "Best move." if you_case else f"Best move by {by}."
    elif verbose and classification == CLASSIFICATION_EXCELLENT:
        msg = "Excellent move." if you_case else f"Excellent move by {by}."
    elif verbose and classification == CLASSIFICATION_GOOD:
        msg = "Good move." if you_case else f"Good move by {by}."

    if motif == "fork" and msg is not None:
        msg = msg.rstrip(".") + ". Watch for forks here."

    if msg:
        await self._announce_via_tts(msg)


async def _build_post_game_review(self) -> None:
    """Post-game review payload: top 3 mistakes by the user's color,
    plus accuracy for both sides. Called when the game ends."""
    try:
        history = list(self._state.get("move_history_moves") or [])
        if not history:
            self._state["lichess_review_ready"] = False
            self.async_set_updated_data(dict(self._state))
            return

        our_color_str = "white" if self._our_color == chess.WHITE else "black"

        # Top 3 mistakes by CPL, only for the user's color, only
        # mistake/blunder/inaccuracy. Skip anything not yet analyzed.
        user_moves = [m for m in history if m.get("side") == our_color_str]
        scored = [
            m for m in user_moves
            if m.get("classification") in (
                "inaccuracy", "mistake", "blunder"
            )
        ]
        scored.sort(key=lambda m: -int(m.get("cpl") or 0))
        top_n = scored[:3]
        # Augment each with best-move SAN and a description.
        top_with_best: list[dict[str, Any]] = []
        for m in top_n:
            # Best SAN at the time isn't stored per-move yet — for v1,
            # leave it as a placeholder. v2 will retain best_uci per ply.
            top_with_best.append({
                **m,
                "best_san": m.get("best_san") or "—",
                "description": self._describe_mistake(m),
            })
        self._state["last_game_top_mistakes"] = top_with_best

        # Accuracy — coarse estimate from the per-ply CPLs, but ONLY over
        # plies that were actually ANALYZED (a resolved Lichess/engine eval,
        # classification != "unknown"). Task 5 (live bug 2026-07-02): the
        # per-ply analysis is fire-and-forget, so during a fast sculpture
        # playback most plies stay at their stub cpl=0 ("unknown") while a
        # handful of resolved mate-swing plies read ~9999. Averaging that
        # mix over ALL plies (the old behaviour) fabricated a 0.0/0.0 for a
        # game that never got graded. Now: compute from the analyzed subset
        # if it covers enough of that colour's plies, else report None so
        # the sensor shows "unknown" instead of a made-up number. A fully
        # analyzed game (e.g. a slow two-player game) is unaffected.
        self._state["last_game_accuracy_white"] = self._accuracy_for_side(
            history, "white"
        )
        self._state["last_game_accuracy_black"] = self._accuracy_for_side(
            history, "black"
        )

        self._state["lichess_review_ready"] = True
        self.async_set_updated_data(dict(self._state))
    except Exception as err:
        _LOGGER.debug("Post-game review build failed: %s", err)


async def async_dismiss_review(self) -> None:
    """Return-to-menu service.

    Clears the lichess_active and lichess_review_ready flags, cancels
    any still-running Lichess stream task, and forgets the current
    game ID. This is invoked by the dashboard's "Back to modes" button
    from EITHER the rich in-game view OR the post-game review view —
    either way the user is asking to get back to the picker.

    We don't touch the underlying review payload (top_mistakes,
    accuracy, move_history) — those stay populated in the sensor
    attributes so they can still be inspected after dismissal.
    """
    self._state["lichess_active"] = False
    self._state["lichess_review_ready"] = False
    # If the Lichess stream task is alive, cancel it. The next
    # async_start_game call will spawn a fresh one.
    if self._lichess_task is not None and not self._lichess_task.done():
        try:
            self._lichess_task.cancel()
        except Exception:
            pass
        self._lichess_task = None
    self._game_id = None
    self.async_set_updated_data(dict(self._state))


async def async_request_hint(self) -> None:
    """Refresh the engine recommendation for the current position.

    Service handler for phantom_chess.request_hint. The dashboard's Hint
    tile already shows best_move_san pulled from the running stream
    analysis — this service forces a re-fetch of the cloud-eval for the
    position-as-currently-known, useful when the cache has changed or
    when the dashboard wants a deliberate refresh.
    """
    if self._analysis_client is None:
        return
    try:
        # AUDIT FIX (beta5 audit, 2026-07-08): pass bypass_cache=True —
        # this service's whole purpose is a deliberate refresh, but the
        # flag added for it in get_eval() was never wired to this caller,
        # so a cached FEN made request_hint a no-op.
        owner = self._board
        fen = owner.fen()
        ev = await self._analysis_client.get_eval(
            fen, bypass_cache=True
        )
        if self._board is not owner or self._board.fen() != fen:
            return
        if ev is None:
            _LOGGER.info("Hint: no cloud-eval data for current position")
            return
        self._state["eval_fen"] = fen
        self._state["eval_cp"] = ev.cp
        self._state["eval_mate"] = ev.mate
        self._state["eval_depth"] = ev.depth
        self._state["eval_source"] = ev.source
        self._state["best_move_san"] = None
        if ev.best_uci:
            try:
                m = chess.Move.from_uci(ev.best_uci)
                if m in self._board.legal_moves:
                    self._state["best_move_san"] = self._board.san(m)
            except (ValueError, chess.IllegalMoveError):
                pass
        self.async_set_updated_data(dict(self._state))
    except Exception as err:
        _LOGGER.debug("Hint request failed: %s", err)

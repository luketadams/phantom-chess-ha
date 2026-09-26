# Phantom Chess — engineering reference

Canonical working knowledge for build **0.5.0b7**, September 2026. Correct or remove stale claims in place. Keep dated observations as evidence; do not retain superseded conclusions as alternate guidance.

**Product goal:** say “I want to play chess,” start reliably, play a physical game, hear spoken commentary on a chosen speaker, and return later without losing the game.

## Current build and evidence

The incremental rebuild preserves integration identity and protocol knowledge. It provides a bundled authenticated dashboard, automatic local checkpoints, explicit restart recovery, PGN library/replay/practice, serialized starts and physical execution, and preferred-pipeline HomePod speech.

Earlier supervised evidence includes a 24-ply local game with captures/castling and a subsequent cancellation/reset. That observation predates this build's physical-execution changes and is not acceptance evidence for them.

## Analysis, coaching and review

The build includes a browser-persisted optional advantage bar on Play/Learn/Review. Live scores require `eval_fen` to match the current full FEN. The bar uses the Lichess winning-chance curve as its visual scale and displays white-positive pawn units or mate winner. Missing/stale evaluations remain unavailable. Online games hide live bar and badges. Training mode adds recent-move labels and destination-square icons; live coaching uses the same chance-loss thresholds for live labels and saved-game review.

Both local and cloud evaluations are White-positive. The [official CloudEval schema](https://github.com/lichess-org/api/blob/master/doc/specs/schemas/CloudEval.yaml) specifies White's viewpoint for centipawns and moves-to-mate; the parser preserves these signs on Black's turn. Mate distance is moves, not plies.

Starting-position and explicit-hint analysis capture board ownership/FEN and reject delayed results after the position changes. Starting analysis supports practice positions. Local Stockfish analysis explicitly restores Skill Level 20, independent of the last opponent's difficulty.

`game_review.py` provides a separate restart-safe HA Store cache and one bounded local-only engine job per board. The library service actions `analyze`, `reanalyze`, `review`, and `cancel_review` start, replace, inspect or pause analysis. Review supports 400 half-moves, depth up to 18/1.5 seconds per position, per-call timeouts (150 seconds for initial engine startup, 15 subsequently), and a 30-minute job ceiling. It pauses between positions if play starts. Shutdown cancels and checkpoints it. Progress saves every five positions and on termination; restart resumes only on explicit request. Analysis fingerprints include grading version, starting FEN and moves; metadata saves do not invalidate it, changed moves do. Saved moves live in a separate protected Store. Unreadable analysis is recomputable and cannot overwrite the game library.

The review report includes legal PVs capped at eight half-moves, pre/post score, mover-relative chance loss, quality labels, and deterministic coaching with a suggested alternative. It does not invent tactical explanations. Rules determine terminal positions, including fivefold repetition with full move history. The UI shows analysis progress, mistake counts for analyzed moves, replay feedback, key moments, and a practice fork from before the selected move. A completed review can be reanalyzed. Interrupted browser reads retry with increasing delay, stopping after five consecutive failures; Refresh analysis explicitly retries or fetches current status. Last-received results are labeled during failures. Older responses cannot overwrite newer requests or another selected game, disconnected cards ignore late replies, and leaving Review stops polling. Results remain accessible without starting the board.

[Primary Lichess research](https://lichess.org/page/accuracy) supports the winning-chance curve. Phantom's live/review thresholds are 5/10/20 percentage points lost for inaccuracy/mistake/blunder, with engine-best matching below 5 and excellent below 2. These are a disclosed heuristic, not a calibrated probability for this player or Chess.com's proprietary expected-points model. [Chess.com's classification documentation](https://support.chess.com/en/articles/8572705-how-are-moves-classified-what-is-a-blunder-or-brilliant-etc) distinguishes special Great/Brilliant rules; matching Stockfish's first choice alone does not justify those awards. No proprietary game-accuracy score or unsupported brilliance detector is claimed.

Validation: 1,562 tests passed on both supported HA versions, 94.23% coverage, 54 browser assertions. A separate Stockfish 18 process on the HA host analyzed Fool's Mate through the actual new review manager, identified the mating blunder, and restored an identical report from storage. This involved no board or audio commands.

## Engine and cache reliability

Build 0.5.0b7 adds [pinned engine artifacts and preflight](ENGINE_ARTIFACTS.md), bounded streaming, exact binary extraction and atomic installation. It also rejects incomplete review-cache records before rendering, validates terminal claims against the saved game, and offers an engine readiness/recovery control. Successful replacement cache writes clear resolved warnings. Live and saved-game labels share `move_quality.py`; missing or ambiguous scores never become equality. Local mate-zero results preserve the winner using the signed engine score. Speech describes evaluation changes rather than asserting actual material loss or an unverified mate event. Deleted games remove their analysis cache, and orphaned caches do not produce false corruption warnings. The minimal test environment passed 1,360 tests with seven expected skips.

## Source and runtime ownership

| Component | Responsibility |
|---|---|
| `coordinator.py` | BLE lifecycle, rules/engine integration, physical completion, modes, analysis |
| `game_library.py` | Validated immutable game snapshots, PGN parsing, atomic Store writes, revision protection |
| `engine_artifacts.py` | Pinned publisher artifacts, bounded streaming, exact extraction and atomic installation |
| `move_quality.py` | Shared winning-chance conversion and live/review quality thresholds |
| `game_review.py` | Bounded local game-analysis jobs, restart-safe cache, position-relative grading and legal-line coaching |
| `sessions.py` | Checkpoints, explicit physical recovery, library actions and practice forks |
| `dashboard_app.yaml` + `www/phantom-chess-card.js` | Default Play/Learn/Review/Board interface; HA authenticated WebSocket service calls |
| `dashboard_template.yaml` | Optional classic renderer; no longer the default interface |
| `homepod_speech.py` | Whole-message buffered native HomePod playback and current Assist voice resolution |
| `config_flow.py` / `__init__.py` | Options and services; speech-only changes apply without reload |
| `examples/voice-assist.yaml` | Start/stop conversation automations and usual-game script |

Local code: `custom_components/phantom_chess/`. Production: `/config/custom_components/phantom_chess/`. Engines: `/config/phantom_chess/bin/`. Two-player recordings: `/config/phantom_chess/recordings/`. Local journal: HA Store `.storage/phantom_chess_games_<MAC-without-colons>`, version 1, up to 200 games. Credentials stay in existing credential storage.

## Session and physical contract

All user-facing game starts share an activation lock. Repeated local start preserves an existing local game; other modes cannot replace an active session. AI results check session board identity, position and pause revision before dispatch. Analysis from older positions cannot overwrite current headline analysis.

Local AI/dashboard moves propose a target on a copy and commit the chess move only after physical completion. Failure pauses with an uncertain physical position; no blind snapshot retry or spectator continuation follows. Online moves retain server authority independently of the physical board. A dashboard move that fails cannot schedule an AI reply.

Undo and reset also wait before committing the model. One physical lock owns the single firmware completion future. Firmware supplies no operation ID, so delayed/duplicate notifications and mechanical settling still require hardware qualification.

Local checkpoints capture immutable move history before asynchronous writes. Revisions prevent older saves overwriting later state. Write errors pause play and surface a visible error; corrupt records are protected against overwriting. Startup never moves the board automatically. Resume explicitly drives the saved position, waits for confirmation, restores history and then schedules the appropriate turn. Practice creates a separate game from the selected replay position.

## 4. Protocol contract and evidence limits

Primary sources are the manufacturer's gameplay protocol documents for firmware 0.3.0 (May 2026) and 0.3.2 (June 2026), plus an nRF52840 capture of the official app on firmware 0.3.3. They are not redistributed here. The later document corrects some details but contains internal contradictions.

### Stable working contract

| Claim | Current contract and qualification |
|---|---|
| P01 — Game channel | `cc68a66e-3bfa-4614-a77f-f46954a4c103` carries the opcode protocol. This is the current game channel |
| P02 — GAME_START | Current sender writes one binary opcode byte `0x00`, 100 ASCII matrix characters, then `,W` or `,B`: 103 bytes. Outbound matrix builder is column-major. Do not send the printable string “0” as the opcode |
| P03 — Write mode | Current `_ble_write` defaults to `response=True`. Official-app capture reports ATT Write Request, including successful firmware 0.3.3 operation. Do not use unverified write-without-response workarounds |
| P04 — SIDE | Current `_phantom_send_side` and manufacturer §2.2 use opcode 10 followed by ASCII `0` = two local players, `1` = board side, `2` = BLE side. These values describe controller/turn ownership, not piece color |
| P05 — TAKE_BACK | Manufacturer June §3.6 specifies opcode 5, `count,FEN,side`; the target FEN drives rearrangement, count is parsed but not used to step history. Its side field uses `1` = board next, `0` = BLE next. Do not reuse SIDE opcode's numeric mapping |
| P06 — Matrix notifications | Manufacturer June §6.4 describes `error,logical_100,sensor_100` on `1b034927-77e8-433e-ac4c-27302e5e853f`, with incoming matrices row-major. This is a different direction/format from outbound GAME_START |
| P07 — Completion | Opcode 12 / move-done is a completion signal used by the executor. Startup waits briefly for WAITING_SIDE, sends SIDE, then awaits completion. Timeout means uncertain completion, not proof of a completed physical move |
| P08 — Piece markers | Normal uppercase/lowercase identify white/black; `.` is empty. `X`/`Z` promotion markers are underspecified. Current code preserves known FEN information instead of universally guessing color; physical promotion qualification remains open |

The general service UUID is `fd31a840-22e7-11eb-adc1-0242ac120002`. Mode selection uses `c08d3691-e60f-4467-b2d0-4a4b7c72777e`. The sculpture characteristic `7eeaef37-1078-4462-9fcc-1a2a1152da45` retains a misleading legacy `GAME_CONFIG` alias: it is not evidence that `(level*2)+color` configures engine difficulty. Keep the full UUID list in [const.py](../custom_components/phantom_chess/const.py), with this qualification. Legacy constants are not proof that a characteristic exists on the current board.

### Current executor, not a proposed universal firmware protocol

`_phantom_execute_position` establishes HOME where needed, optionally selects mode 2 for a new game on firmware ≥0.3.2, sends the snapshot, waits up to roughly five seconds for WAITING_SIDE, sends SIDE even if that wait expires, then awaits move-done under the caller's timeout. It sets a long settle window while rearrangement is underway. Current AI/dashboard movement uses the snapshot executor; a legacy opcode-2 movement method remains in code.

Reset can take several minutes while pieces are rearranged. The rebuild commits the reset model only after completion and raises on timeout. A supervised reset in September 2026 failed to confirm: firmware did not reach HOME within 30 seconds after GAME_END (reported mode `N b?-b1`). The integration marked the position uncertain; physical reset completion is unverified and no retry was sent. Board sound volume (0–32 protocol scale) is separate from HomePod speech volume (0–1 HA scale).

### Current constraints and open questions

- Use write-with-response on the game channel. A write returning successfully does not establish physical completion.
- Mode selection is part of current startup ordering. Its causal role in transport failures is unproven.
- The observed official-app session worked without bonding. The exact cause of the observed BlueZ failure remains unresolved; ESPHome/NimBLE and CoreBluetooth have successful observations.
- Manufacturer documentation supports app-to-board opcode-2 movement. Current integration AI/dashboard movement uses snapshots. Both facts must inform future protocol changes.
- Use the explicit three-value SIDE mapping above. The manufacturer's other summaries conflict with §2.2 and need clarification. TAKE_BACK has its own distinct mapping, documented in §3.6.
- Matrix mismatch text alone is insufficient to diagnose failure; correlate it with physical observations, legal state and completion signals. Its occurrence during apparently valid play remains unexplained.
- No verified firmware update candidate is recorded. The saved app-bundled image is not suitable as an update source.

Raw captures support these conclusions. Draft questions do not establish a manufacturer response. Matching firmware and experimental conditions are required before generalizing a result.

## Voice

Spoken announcements go to the media player chosen in the options flow. The optional managed-speech path resolves the preferred Assist pipeline's TTS for every message, fetches complete audio, streams a seekable buffer and serializes per speaker; it targets Apple TV-integration HomePods and does not use Music Assistant queue commands. A separate event-forwarding automation must be disabled or ignore `delivery_managed: true` to avoid duplicate playback. Start/stop phrases are in `examples/voice-assist.yaml`. Text intent checks do not qualify microphones or acoustic behavior.

## Product limits and next work

The [release roadmap](RELEASE_ROADMAP.md) holds remaining work: supervised hardware qualification, additional engine-platform support, explicit cloud policy, complete session-controller extraction, all-mode persistence, advanced coaching/drills and multi-board navigation. The engine has no random-move failure fallback. Local analysis can still consult cloud sources; local-only privacy policy is not yet a configurable contract.

The UI uses packaged JavaScript and Home Assistant authentication, without user-supplied browser tokens or CDN code. Browser tests exercise the actual card; live configuration checks are separate from synthetic UI tests. HA API references: [custom cards](https://developers.home-assistant.io/docs/frontend/custom-ui/custom-card/) and [WebSocket services](https://developers.home-assistant.io/docs/api/websocket/).

## Difficulty labels

The dashboard displays approximate **Stockfish 18 CCRL benchmark bands** beside local levels. They are obtained by inverting Stockfish's published Elo-to-skill polynomial for the integration's existing skill values (0, 1, 2, 3, 7, 11, 15), then rounding outward to hundreds. Level 8 is maximum strength without an assigned Elo. Bands are reference intervals, not measured confidence intervals or FIDE/Chess.com ratings. Search-depth limits differ from the upstream calibration, and online Lichess levels differ. The engine settings and stored numeric select values are unchanged.

Source: [Stockfish 18 strength conversion](https://github.com/official-stockfish/Stockfish/blob/sf_18/src/search.h#L173-L195). A human-rating-calibrated difficulty ladder remains separate engineering work.


# Phantom Chess HA — Roadmap

*Consolidated 2026-07-02, after the v0.4.0-beta3 release (Silver quality scale, 95.5%
coverage) and the first full remote hardware-verification session. Sources:
`IMPROVEMENTS.md` (2026-06-09 audit; IDs cited below), `FINDINGS_TEST_PORTABILITY.md`,
the beta3 ship checklist, and the 2026-07-02 live-test findings. This file is the
forward-looking plan; IMPROVEMENTS.md remains the historical audit record.*

**Release discipline:** work is batched and staged; Luke ships. Every release needs a
GitHub **Release** (not just a tag) for HACS, `--prerelease` while in beta.

---

## Beta4 — already staged (ship next)

The staged batch is gate-green (885 tests, 95.5%) and contains four fixes:

1. **Pawn-promotion matrix markers** — `X`/`Z` (fw0.3.2 doc §9.2) no longer blind
   matrix state after a promotion; FEN reconstruction refuses to guess the side
   mapping and holds last-known-good `live_fen`. *(was IMPROVEMENTS L4)*
2. **M5 notify-thread race** — the whole move-apply block is marshalled onto the
   event loop (`_apply_move` + `call_soon_threadsafe`), with a cross-thread
   regression test. *(was IMPROVEMENTS M5 — the last item of the §1.5 race class)*
3. **Two-player finalize clears `lichess_game_id`** — dashboard no longer sticks in
   the recording view after a game ends *(found live 2026-07-02)*.
4. **Battery clamped 0–100** — fw0.3.3 reports 105–106% on wall power *(found live)*.

Before tagging beta4: hardware-verify #1 with a real promotion and #2 with live play.

---

## Stability & correctness

**Near-term (code-ready, from the June audit — all should ride in beta4/beta5):**
- **M2 — double-move dedup**: a slow piece-slide fires two `\x03M` notifications; add
  `(uci, monotonic_ts)` dedup (<~400 ms window) in the discovery callback.
- **M3 — AI-loop circuit-breaker**: N consecutive `_phantom_execute_position`
  timeouts → stop the loop + persistent notification, instead of hammering a wedged
  board forever.
- **M4 — scope `last_seen` dedup** to non-actionable heartbeats so a legitimately
  repeated payload can't be swallowed.
- **M9 — echo-window mismatch**: drive AI-echo expiry off BLE_MOVE_DONE rather than
  the fixed 60 s (vs the 600 s settle window).
- **L9/L10 — parser anchoring + except-narrowing** (small hygiene items).

**Found live 2026-07-02 (needs investigation before code):**
- **fw0.3.3 matrix status unreliable**: SEND_MATRIX reads carried
  `ERROR: Chessboard and sensor matrix do not match` for an entire session of
  flawless play, and `resync_detection` (opcode 14) does not clear it. Either the
  integration should stop treating the status prefix as a health signal on 0.3.3,
  or this is a firmware question for Efraín. Decide, then fix or document.
- **Sculpture post-game review computes 0.0/0.0 accuracy** (two-player review gives
  real numbers). Reproduce in tests, fix the review pipeline.
- **Adapter-race**: HA's local BlueZ adapter can win the board connection after a
  restart → MTU 23 → every ≥21 B write fails 0x0D. Mitigated on Luke's box by
  disabling the local adapter entry. For other users: document prominently
  (README + a repair issue?) — consider detecting MTU ≤ 23 at connect and raising
  a repair issue pointing at the proxy requirement.

**Longer-term:**
- **Split coordinator.py (~5,900 lines)** per IMPROVEMENTS §5: `ble_session.py`,
  `protocol.py`, `lichess_game.py`, `local_modes.py`, `announce.py`; target ~1,500
  lines of orchestration. Now tractable — the 95.5% suite + `ble_mock.py` harness
  + drift-guard test protect each extraction.
- **Restore the 95 coverage floor** *(updated at beta4 ship, 2026-07-08)*: the
  beta4 batch landed at 94.7% full-HA coverage; the gate was floored at 94
  (still enforcing) to ship. Raise-back targets: `dashboard_provision.py`
  83.1% (42 missed - dashboards_collection branches), `config_flow.py` 86.1%
  (22 missed - user_manual/reauth/reconfigure edges), `coordinator.py` 92.5%
  (183 missed - full-HA-only lifecycle paths). Cover ~15+ lines -> back over
  95.0, then bump `fail_under` to 95.
- **Coverage headroom** *(historical, pre-beta4)*: `dashboard_provision.py` (90%) is the lowest-covered
  module; the CI floor (95) has only a 0.5-point buffer.

## Features

- **Hardware-verify the historic-games catalog**: 1 of 18 verified end-to-end
  (Opera Game, 2026-07-02, flawless incl. captures + mate). Remaining 17 need
  playback runs — the unusual-geometry ones (multiple promotions, long king hunts:
  Wei Yi, Carlsen–Nepo 136-move G6) are the interesting cases for magnet timing.
- **Expose fw0.3.2+ tunables**: `UUID_SLIDE_DELAY` as a number entity;
  `SD`/`JC` (slide-detection / jump-to-center) GAME_ASSISTANCE fields as options.
- **Takeback on 0.3.2+**: reconcile the 8-opcode TAKE_BACK `"count,FEN,side"`
  format (doc §3.6) against the current implementation; verify live.
- **Pin/skewer motif detection** (fork detection shipped; pin/skewer marked
  "in progress" in the README).
- **Multi-board stress-testing** — architecturally supported, never exercised
  (single-board household). Community-driven; keep `entry_id` paths tested.

## Dashboard & UI

- The big structural work shipped in beta3 (state-matrix audit: no reachable state
  renders blank; sculpture picker → now-playing → review flow; `Running` stand-by
  card). Remaining:
  - **Two-player post-game review parity** with the sculpture review flow (once the
    finalize fix ships, verify the review card renders from `lichess_review_ready`).
  - **Surface the mismatch-recovery ladder in one place**: a "board health" card
    that shows matrix status + last resync + one-tap `resync_detection` /
    `resync_two_player` / `back_to_modes`, instead of tiles scattered per-view.
  - **Board-image overlay polish**: last-move + threat glyphs exist; consider eval
    bar on the image entity for wall-tablet dashboards (Luke's Glance pattern).
- Dashboard changes are cheap to regress — keep the state-coverage simulation test
  in step with any new firmware_mode labels.

## Installation & distribution

- **HACS Default submission** — Silver was the gate; now eligible. Prep: repo
  requirements checklist, brands check (local `brand/` mechanism already in use),
  docs audit, then the PR to hacs/default.
- **ESPHome-proxy requirement docs**: the README documents it, but it's the #1
  support trap for fw0.3.2+ users. Consider: config-flow warning when the connected
  adapter negotiates MTU ≤ 23, linking to the proxy guide.
- **Minimum-HA floor**: `hacs.json` says 2024.11, but instant sidebar registration
  uses `frontend.async_panel_exists`/`show_in_sidebar` (HA ≥ ~2026.3); on older HA
  the panel appears after the next restart. Either raise the floor or note it in
  the README. *(FINDINGS_TEST_PORTABILITY §3)*
- **Repo hygiene**: `.gitignore` the `*.bak.*` working files in
  `custom_components/`; decide the fate of the root working docs
  (FINDINGS/IMPROVEMENTS/checklists) — commit as docs/ or ignore.

## Needs Luke at the board (carried from the beta3 checklist)

- Live two-player game end-to-end (physical moves, out-of-sync flag → clear,
  checkmate → PGN matches display).
- `resync_two_player` mid-game.
- Eyes-on check of the persistent fw0.3.3 matrix-mismatch report (off-center piece?).
- A promotion during live play (exercises the staged X/Z fix for real).

## Firmware asks for Efraín

- The fw0.3.2+ BlueZ write/subscribe rejection (root-caused, sniffer-proven;
  proxy is the workaround — an upstream fix would remove the extra hardware).
- fw0.3.3 permanent `ERROR: matrix do not match` status during normal play;
  does opcode 14 actually reset it, and what are the X/Z marker side semantics
  (doc §9.2 says only "in some contexts")?

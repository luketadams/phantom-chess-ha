# Phantom Chess — release roadmap to 0.5.0

Created 2026-09-26. Plan of record for taking 0.5 from beta to a public release.

## Definition of releasable

A stranger with a Phantom board and Home Assistant can install from HACS, finish setup without reading source, play a local game against Stockfish, and get a clear message when something they need (Bluetooth proxy, engine platform, Lichess token, speaker) is missing. Concretely:

1. Source is in git and the tagged release is what users get. No installation-specific files or data in the public repo.
2. CI is green and gating: tests on the minimum supported HA and on the current HA, plus hassfest, HACS validation, ruff, mypy and the frontend card tests.
3. A clean install on a fresh HA instance works end to end, with no specific speakers, entities or pipelines assumed.
4. Upgrading from 0.4.0-beta4 migrates cleanly. Removing the integration cleans up what it created, or the README says what remains.
5. The engine works, or fails with a clear message, on every platform HA commonly runs on — in particular amd64 and aarch64 HAOS, both Alpine/musl.
6. The README describes 0.5 accurately: requirements (ESPHome Bluetooth proxy for firmware ≥ 0.3.2), setup, features, privacy (what goes to Lichess), troubleshooting and known limits.
7. A supervised hardware pass covers the core gameplay paths; every failure is fixed or documented as a known limit.
8. The release follows rc → soak through the real HACS install path → final.

## Milestones

### M0 — Safeguard ✓
- [x] Working-tree backup before any change
- [x] Installation-specific records moved out of the published tree; public docs scrubbed
- [x] 0.5.0b7 committed on `release/0.5` and pushed (fac4fac)

### M1 — Build and CI green ✓
- [x] Full suite against current HA 2026.9.3 and floor 2026.2.3 (9007fed)
- [x] Floor 2026.2.3; `hacs.json`, `pyproject.toml` and the CI matrix aligned to floor + current
- [x] Frontend card tests run in CI; coverage gate (94 %) enforcing; `main` requires all checks
- [x] hassfest and HACS validation green

### M2 — Clean-install and platform correctness ✓
- [x] Fresh HA 2026.9.3 container: token-less, speaker-less config flow; dashboard provisions (`scripts/ha_e2e.py`)
- [x] Engine verified on aarch64 musl, amd64 musl (HA image) and amd64 glibc; launch failures report within the 150 s bound ([details](ENGINE_ARTIFACTS.md))
- [x] 0.4.0-beta4 → 0.5 upgrade in a container: no lost entities or errors
- [x] Removal deletes dashboard, engine, caches and copied images; saved games and recordings are kept and documented
- [x] Speech: generic `tts.speak` path needs no Apple TV integration and reports failures; managed HomePod speech is opt-in. Audible check on a generic speaker moves to M4
- [x] Reset confirmation: the unconfirmed Sept 2026 reset (firmware stuck at `N b?-b1` after GAME_END) is not diagnosable without the board. The timeout message is now actionable; root cause moves to the M4 capture below

### M3 — Docs and quality scale ✓
- [x] README rewritten for 0.5: requirements, install, setup, dashboard tour, voice, configuration, entities, updates, examples, services, privacy, troubleshooting, limits
- [x] CHANGELOG: consolidated 0.5.0 section above the beta entries
- [x] `quality_scale.yaml` re-audited against 0.5 code (0.5.0b8). Findings fixed: repair issues rebuilt (engine, Bluetooth route), all service errors translated, coverage restored above 95 %, dead classic renderer removed, dashboard pause-switch reference fixed. Platinum strict-typing honestly marked todo

### M3.5 — Scope and code quality (decided 2026-09-27) ✓
- [x] Puzzles, endgame drills and the local-only analysis option join 0.5.0 (0.5.0b9); qualified in the board session (QUALIFICATION.md section F)
- [x] `coordinator.py` split into session modules (protocol, online_session, local_game, two_player, autoplay, coaching, runtime), behaviour-free (25a8cfe)
- [x] Strict typing: `mypy --strict` against Home Assistant 2026.9.3's types is the gating `typecheck` job; the manifest claims Platinum (every Bronze–Platinum rule done or exempt)

### M4 — Hardware qualification (supervised, scripted, ~60–90 min)
Script: [QUALIFICATION.md](QUALIFICATION.md).
- [ ] Local game with capture, castling, en passant and promotion
- [ ] Pause during motion; undo; reset; resign; back to modes
- [ ] Reset capture: with `debug_dump` on, reset from a finished game and from a mid-game position; record `firmware_mode` transitions after GAME_END
- [ ] Bluetooth loss mid-game (proxy power pulled); HA restart mid-game → resume
- [ ] Voice start; speech for moves, check and mate, on the HomePod and on one generic TTS speaker
- [ ] Lichess online game incl. takeback; two-player recording; one sculpture game
- [ ] One puzzle including a wrong try (takeback) and a hint; one endgame drill; local-only analysis option on and off

### M5 — Release
- [ ] 0.5.0rc1 tag + GitHub prerelease, installed through HACS on the reference box
- [ ] About one week of normal use
- [ ] 0.5.0 final release and announcement draft
- [ ] HACS default list (after the full release): the repository already meets the other requirements checked 2026-09-27 — public, description, issues, topics, local `brand/icon.png`, HACS action and hassfest green. The remaining one is a full (non-pre) release. Submission is a PR to `hacs/default` adding the repository alphabetically to `integration`, made by the owner

Also done: the engine mirror carries the full GPLv3 Corresponding Source (September 27).

## Deferred past 0.5.0
Variations, annotations and further drill sets; open firmware questions for the manufacturer (draft message prepared; matrix-mismatch text during valid play, reset after game end, BlueZ incompatibility).

## Dropped (2026-09-27)
- glibc ARM64 engine: no official Stockfish build, and Home Assistant retired the install methods that ran HA on that platform. Such hosts get the "engine not available" repair entry; online play still works.
- Multi-board dashboard: one board per household in practice; listed under Known limits.

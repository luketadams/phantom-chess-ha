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

### M0 — Safeguard
- [x] Working-tree backup before any change
- [x] Installation-specific records moved out of the published tree; public docs scrubbed
- [x] 0.5.0b7 committed on `release/0.5` and pushed (fac4fac)

### M1 — Build and CI green
- [ ] Full suite against current HA (2026.9.x); fix breaks
- [ ] Supported HA floor decided; `hacs.json`, `pyproject.toml` and the CI matrix aligned to floor + current
- [ ] Frontend card tests run in CI; coverage gate enforcing; required checks updated
- [ ] hassfest and HACS validation green

### M2 — Clean-install and platform correctness
- [ ] Fresh HA container: install, config flow without Lichess token or speaker, dashboard provisions, sensible entity names
- [ ] Engine on amd64 and aarch64 musl: works or reports clearly; local play refuses to start with an actionable message rather than hanging
- [ ] 0.4.0-beta4 → 0.5 upgrade: config-entry migration, orphaned entities, dashboard reprovision
- [ ] Removal cleans up dashboard, `www` assets, engine binary and storage (or documents what remains)
- [ ] Speech: generic media-player TTS works without the Apple TV integration; managed HomePod speech stays optional
- [ ] Reset confirmation: a supervised reset (Sept 2026) never confirmed because firmware did not reach HOME within 30 s of GAME_END. Investigate the timeout and completion signal; fix or bound it

### M3 — Docs
- [ ] README rewritten for 0.5: requirements, install, setup, dashboard tour, voice, services, privacy, troubleshooting, limits
- [ ] CHANGELOG: consolidated 0.5.0 section above the beta entries
- [ ] `quality_scale.yaml` re-audited against 0.5 code

### M4 — Hardware qualification (supervised, scripted, ~60–90 min)
- [ ] Local game with capture, castling, en passant and promotion
- [ ] Pause during motion; undo; reset; resign; back to modes
- [ ] Bluetooth loss mid-game (proxy power pulled); HA restart mid-game → resume
- [ ] Voice start; speech for moves, check and mate
- [ ] Lichess online game incl. takeback; two-player recording; one sculpture game

### M5 — Release
- [ ] 0.5.0rc1 tag + GitHub prerelease, installed through HACS on the reference box
- [ ] About one week of normal use
- [ ] 0.5.0 final release and announcement draft

## Deferred past 0.5.0
Session-controller extraction from `coordinator.py`; glibc ARM64 engine; explicit local-only analysis policy; puzzles, drills and variations; multi-board dashboard; HACS default-repository submission; open firmware questions for the manufacturer (matrix-mismatch text during valid play, BlueZ incompatibility root cause).

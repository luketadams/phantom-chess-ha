# Changelog

All notable changes to the Phantom Chess Board Home Assistant integration are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0-beta3] — 2026-07-01

Third public beta. **Restores full gameplay on the current public firmware (0.3.2 / 0.3.3) — by routing the board through an ESPHome Bluetooth proxy** (see the firmware note below). Also ships the fw0.3.x diagnostics that root-caused the breakage, plus the robustness, recovery, and hardening batch staged since beta2.

### Added

- **`phantom_chess.diagnose_game_start` service** — logs the negotiated MTU and `UUID_GAME` write limits and (with `experimental: true`) A/B-tests the GAME_START write variants on the live board. This localised the fw0.3.2/0.3.3 root cause (see Known limitations).
- **`phantom_chess.resync_detection` recovery + dashboard tile** for a board wedged in "Snapping Pieces" / "Chessboard and sensor matrix do not match" with the pieces correctly placed. Sends RESET_DETECTION (opcode 14) to re-seed the firmware's expected matrix without driving the magnet.
- **Matrix `ERROR:` payloads are surfaced** to `matrix_status` even when the frame carries no parseable grid (previously dropped), so a firmware wedge is visible.
- Graceful, quiet degradation on firmware 0.3.2/0.3.3 for characteristics it no longer permits/exposes (CCCD-disallowed `SEND_MATRIX`/`BATTERY` notify subscribes, and the now-absent `ERROR_MSG`/`STATUS_BOARD` characteristics), with a battery read-poll fallback, so reconnects don't log-spam.
- **`start_game` exposes its clock fields** (`clock_limit_seconds`/`clock_increment_seconds`) in `services.yaml`; `icons.json` gains the `Watch AI vs AI` mode and `voice_announcements` switch.

### Fixed

- The `GAME_START`/`GAME_ASSISTANCE` 0x0D rejection now raises a **descriptive, actionable error** (with the firmware/MTU diagnostics) instead of a bare stack trace.
- **Diagnostics dump** now reports the live integration version (was hardcoded `0.2.0`) and populates the battery field (`battery_percent`); private-attribute and Stockfish introspection are guarded so a rename can no longer 500 the whole dump.
- **`white_to_move`** no longer raises on a board-only FEN; **`resign`** no longer marks a game resigned if the Lichess POST failed.
- **Restored `number` entity values are clamped** to the entity's current min/max, so a narrowed slider range can't persist an out-of-range value.

### Firmware 0.3.2 / 0.3.3 — requires an ESPHome Bluetooth proxy

**The current public firmware (0.3.2 / 0.3.3, app 4.1.0) refuses all GATT writes and notification-subscribes from Home Assistant's built-in Linux/BlueZ Bluetooth** — `WRITE_NOT_PERMITTED` on characteristic subscribes and `INVALID_ATTRIBUTE_VALUE_LENGTH` (ATT 0x0D) on `UUID_GAME` writes (even a 1-byte `GAME_END`) — while accepting the official iOS app (Apple CoreBluetooth) doing **byte-identical** operations on the **same unencrypted link**. This was confirmed with a Nordic nRF52840 BLE sniffer capturing both the working app session and the failing HA session (see `FW033_CAPTURE_FINDINGS_2026-06-29.md`). It is a **firmware ↔ BlueZ stack incompatibility, not an integration bug** — ruled out: bonding/encryption, write method, payload, SELECT_MODE, subscribe order, and the BlueZ GATT cache.

**Fix — route the board through an [ESPHome Bluetooth proxy](https://esphome.io/projects/):** a ~$10 generic ESP32 web-flashed with the ready-made "Bluetooth Proxy" firmware and powered near the board. Home Assistant then reaches the board over the ESP32's BLE stack (which the firmware accepts), and **all gameplay works end-to-end** — game start, physical-move detection, AI/engine moves, takeback, etc. Verified live: with the proxy, HA subscribes to `UUID_GAME` successfully, `start_two_player_game` starts the game, the firmware enters *Board Playing*, and physical moves are detected and analysed.

A direct HAOS/BlueZ connection to the board does **not** work on firmware 0.3.2+. The **0.3.0** firmware path is unaffected (works over BlueZ directly). On 0.3.2+ *without* a proxy, `start_*` services fail with a descriptive error (see the `diagnose_game_start` service) rather than a bare crash.

## [0.4.0-beta2] — 2026-06-03

Second public beta. Two-player recording robustness, "Back to modes" now re-homes the board, a faithful PGN export, and a polished public README.

### Added

- **"Resync board" action for two-player recording.** A new `phantom_chess.resync_two_player` service (and a one-tap dashboard tile) re-drives the physical pieces back to the last recorded position when the board and the model have drifted apart — for example after an illegal move was rejected. The recorded move history is untouched. _Experimental: behaviour in 2-local-player mode still needs live-hardware verification._
- **Out-of-sync feedback in two-player recording.** When a physical move is illegal in the current position it was previously dropped silently (so a later checkmate never registered). The integration now posts a notification and speaks a prompt ("that move isn't legal — put the piece back"), and clears it automatically once a legal move is played.

### Changed

- **"Back to modes" now re-homes the board.** Returning to the mode picker drives the pieces back to the starting position (and finalizes + saves a two-player recording if one is active), leaving the board clean for the next mode. The board re-home is best-effort, so a disconnected board still returns the dashboard to the picker.
- **Two-player PGN is rebuilt from the displayed move history** rather than directly from the internal board, so the saved game always matches what was shown on the dashboard. A divergence between the two is logged (with both ply counts and FENs) to help root-cause the underlying drift. Fixes a case where a saved PGN had fewer moves than the dashboard showed.
- **README rewritten for the public beta** — accurate five-mode launcher, the auto-provisioned dashboard flow (no more copy/paste of helpers/scripts), an honest account of historic-games playback, and removal of the now-fixed AI-vs-AI hang from Known Limitations.

## [0.4.0-beta1] — 2026-06-03

First public beta. Bronze quality scale, a full ghost-mascot visual identity, the AI-vs-AI capture freeze fixed, and a speaker picker for spoken play-by-play.

### Added

- **Mascot mode launcher.** The dashboard mode picker is now a compact set of custom ghost-mascot picture buttons — Lichess, Stockfish, Historic Games, 2-Player, AI vs AI — in a 3-then-2 layout, replacing the oversized stretched tiles. Button art is served from `/local/phantom_chess/buttons/`.
- **Integration icon** (a ghost holding a knight) via the HA 2026.3+ local `brand/` mechanism — no brands-repo PR required.
- **Speaker picker for voice play-by-play.** The options flow now has entity-picker dropdowns for a text-to-speech engine and the speaker to use (HomePod, Alexa, Sonos, a Voice assistant — any `media_player`), so announcements and coaching aren't tied to one user's setup. The `phantom_chess_announce` event remains for fully custom routing, and everything is a graceful no-op when unset.

### Fixed

- **AI-vs-AI no longer freezes on captures.** Two parts: the firmware's `advancedCapture` is now enabled so a captured piece is routed to the graveyard, and a capture/castle-aware inter-move settle waits for that second physical operation to finish before the next board snapshot (the move-done signal fires before the graveyard move completes, so a short gap collided with the still-moving magnet).
- Carries the alpha34 CI fixes (mypy float annotations on the number entities; `pytest-asyncio` in the matrix-tests job).

### Changed

- Dashboard renamed to **Phantom Chess**; the "Sculpture Library" mode is now shown as **Historic Games** (display text only — the internal mode value is unchanged).
- Declared `"quality_scale": "bronze"` in the manifest.

### Known limitations

- The capture/castle settle is logic-tested; a full live AI-vs-AI spectator game on hardware is the immediate post-beta verification.

## [0.4.0-alpha34] — 2026-06-01

CI is green across all six jobs for the first time since alpha29. Two long-standing red jobs are fixed; there is no change to the integration's runtime behavior.

### Fixed

- **`typecheck` (mypy) job had been red since alpha30.** `PhantomBaseNumber` inferred its `_attr_native_min_value` / `_attr_native_max_value` / `_attr_native_step` class attributes as `int`, but `PhantomAIvsAIMoveDelayNumber` (added in alpha30) overrides them with floats (a 0.5 s step), producing three `[assignment]` errors. The base attributes are now explicitly annotated `float`, matching Home Assistant's own `NumberEntity` typing; integer sliders stay valid since `int` is assignable to `float`.
- **`matrix-tests` job went red in alpha33.** The new `tests/test_ai_vs_ai_resilience.py` uses `async def` tests, but the matrix-tests job installed only `pytest` (no `pytest-asyncio`), so `asyncio_mode = "auto"` was ignored and the four tests errored with "async def functions are not natively supported." `pytest-asyncio` is now in the matrix-tests install step. (These tests already passed in the `ha-tests` job, which bundles the plugin.)

### Notes

- alpha32 and alpha33 shipped on a red CI because the pre-ship gate only ran pytest locally. The gate now runs the full gating set — `pytest` + `mypy` + `ruff` + `compileall` — before tagging.

## [0.4.0-alpha33] — 2026-05-31

AI-vs-AI resilience: a transient BLE disconnect mid-game is now recovered instead of ending the spectator game.

### Fixed

- **AI-vs-AI game died on the first BLE blip.** Under sustained stepper load (a full board snapshot every ply) the board's BLE link occasionally drops mid-game; the loop used to `break` permanently on the first failed move. It now waits for the background maintain loop to reconnect (bounded ~30s) and re-drives the current position, so the game continues. If the board never comes back it still stops gracefully.
  - The recovery re-drives the already-applied position via the snapshot primitive (`_phantom_execute_position` with the current board FEN) rather than re-calling `apply_ai_move` — the latter pushes the move onto the internal board *before* the BLE write and doesn't roll back on failure, so re-calling it would no-op and leave the physical board a move behind.

### Tests

- New `tests/test_ai_vs_ai_resilience.py` (4 tests): transient-drop → reconnect → re-drive → game continues; never-reconnect → graceful stop; the `_ai_vs_ai_await_reconnect` helper returns True once the link is restored and bails when the game is stopped. Added to the CI matrix-tests job. Full minimal-env suite: 228 passed, 1 skipped.

### Known limitations

- The recovery is verified in software but not yet proven against a real hardware disconnect. If the drop turns out to be a physical radio brownout under stepper current (rather than a software staleness false-positive), reducing mechanism speed or adding an inter-move settle may matter more than the retry.

## [0.4.0-alpha32] — 2026-05-31

Dashboard polish: eval bar + per-item colors survive the markdown sanitizer, a Reset board button, the `Board Playing` firmware state, and a steady AI-vs-AI status line.

### Added

- **Reset board** tile in the post-game review controls — drives the magnet back to the starting position via `phantom_chess.reset_position` instead of rearranging pieces by hand.

### Fixed

- **Eval bar rendered blank.** It was built from inline-styled HTML `<div>`s, but HA's markdown card sanitizes its body with js-xss, whose default whitelist allows `<div>`/`<span>` as tags with *zero* attributes — so every inline `style=` was stripped. Redrawn with a card-mod `style:` block (a two-stop `linear-gradient` fill on `ha-card` plus `::before`/`::after` labels), which is injected into the shadow DOM and bypasses the sanitizer.
- **Move colors stripped.** Same root cause: the moves table glyphs, last-move classification/motif/engine-hint, and the game-review "biggest mistakes" cards used inline `style="color:…"`. Migrated to `<font color="…">` (which js-xss keeps via `font: color/size/face`), so the colors render again.
- **Mode picker went blank mid-game.** Added the firmware's `Board Playing` state alongside `BLE Playing` to every mode-section condition, so the dashboard no longer empties when firmware reports `Board Playing`.

### Changed

- **AI-vs-AI status.** During spectator games the in-game "State" tile showed the raw firmware mode, which flickered between "Setting Up" and "Board Playing" every few seconds. Replaced it with a markdown card showing a steady "AI vs AI — move N" (full-move count from move history); other game types keep the raw state.

### Tests

- Dashboard renderer suite re-baselined: surviving tiles 17 → 16 (the in-game State tile became a markdown card). New `test_no_inline_style_colors_use_font_tag` locks the moves/last-move/review cards to `<font color>` (no inline `style=`); `test_eval_bar_is_card_mod_not_inline_html` (added with the eval-bar fix) locks the eval bar to card-mod CSS. Full minimal-env suite: 161 passed.

## [0.4.0-alpha31] — 2026-05-30

Suppress duplicate board during learning-view play (Lichess / Stockfish / AI-vs-AI).

### Fixed

- The "board busy / mid-move" conditional section in `dashboard_template.yaml` (line 859) was rendering a full-width `picture-entity` board alongside the learning view's own embedded board, producing two boards stacked on top of each other during any mode that activates the learning view. Added `binary_sensor.phantom_*_learning_view_active = off` to its condition list so it only fires when the learning view ISN'T already showing the board.
- The bare-board section is preserved for cases that don't activate the learning view (sculpture playback, snapping-pieces phase, etc.) — that was its original purpose.

### Tests

- 222 pure tests still passing. Dashboard tile/button counts unchanged (this is a one-line conditional addition, not a card change).

## [0.4.0-alpha30] — 2026-05-28

5th mode tile — Watch AI vs AI. Stockfish plays both sides on the physical board so you can watch as a spectator.

### Added (entities)

- `number.<DEVICE>_white_ai_level` — Stockfish skill 1–8 for white (default 3).
- `number.<DEVICE>_black_ai_level` — Stockfish skill 1–8 for black (default 3).
- `number.<DEVICE>_ai_vs_ai_move_delay` — seconds between moves, 0.5–10s slider, 0.5s step (default 1.5s).
- All three are `EntityCategory.CONFIG`, persist via `RestoreEntity`, and don't require BLE (always-available).

### Added (dashboard)

- 5th mode tile **Watch AI vs AI** (pink, mdi:robot-happy) in the mode picker.
- Conditional section rendering when the mode is selected: white/black level sliders with ELO-mapping markdown, move-delay slider with pacing label (Frantic/Brisk/Watchable/Leisurely), and a Start button.
- Start button calls `phantom_chess.start_ai_vs_ai_game` with empty data; the service handler reads the persisted slider values from the coordinator. No JS templating required (vanilla `tile` works).

### Changed (service)

- `phantom_chess.start_ai_vs_ai_game` now falls back to coordinator-stored values when `white_ai_level` / `black_ai_level` / `move_delay_seconds` are omitted. Previously hardcoded 1.5s default. Developer-Tools yaml calls + automations benefit from this default-from-state behavior too.

### Tests

- Tile→button baseline bumped 21 → 24 (3 new action-shaped tiles: 5th mode tile, in-section back-to-modes, Start button). 222 pure tests passing.

## [0.4.0-alpha29] — 2026-05-27

Fix stale BLE connection when BlueZ tears down GATT objects underneath us.

### Fixed

- **`_handle_gatt_staleness`** now also matches BlueZ's raw `org.freedesktop.DBus.Error.UnknownObject` / `... doesn't exist` family on `org.bluez.GattCharacteristic1` (plus the `ReadValue` / `StartNotify` / `StopNotify` method variants), not just bleak's translated "Characteristic ... not found".
- **Live symptom this fixes:** an AI-vs-AI repro attempt today sat with `_ble_connected = True` for ~6h while every BLE write returned `UnknownObject` from BlueZ. The integration's `binary_sensor.*_connected` reported "on" but the underlying link was dead at the OS layer. Service calls surfaced in the UI as "Unknown error".
- Detector now flips `self._ble_connected = False` immediately on detection (doesn't wait for bleak's disconnect callback) so the `connected` binary sensor reflects reality even before the reconnect loop runs.
- The BlueZ-gone path bypasses the `_discovered_uuids` gate — if the OS says the characteristic object is gone, there is nothing meaningful to gate on.

### Added (tests)

- 7 new `_handle_gatt_staleness` cases — BlueZ `UnknownObject` on a known UUID, BlueZ `UnknownObject` on a never-discovered UUID, bleak "not found" on a known UUID, bleak "not found" on a never-discovered UUID (regression: must NOT detect), an unrelated error (must NOT detect), `ReadValue` / `StartNotify` / `StopNotify` BlueZ variants, and disconnect itself raising. 222 pure tests total.

### Cleanups (test-suite)

- 4 unused-import ruff warnings cleaned up in `tests/` (pre-existing, not in CI's gate path — opportunistic).

## [0.4.0-alpha28] — 2026-05-27

12 more coordinator tests targeting async BLE-write methods.

### Added (tests)

- `async_set_sound_level` (4 cases) — clamp above 32, clamp below 0, preserve in-range, sounds_bitmask + tutorial defaults preserved in payload.
- `async_set_mechanism_speed` (1 case) — writes ASCII integer to `UUID_MECHANISM_SPEED`.
- `async_set_pause` (2 cases) — paused=True → `SELECT_MODE 3` + `game_status = "paused"`; paused=False → `MODE_CHESS_PLAY` + `game_status = "playing"`.
- `async_play_sound` (5 cases) — `"check"` → opcode 1, `"checkmate"` → opcode 2, case-insensitive normalization, invalid value raises `ValueError`, BLE-not-connected raises `RuntimeError`.

Tests use `asyncio.run()` to avoid the `pytest-asyncio` dep in the matrix-tests minimal env. `_ble_write` is `AsyncMock`-stubbed so payload + UUID can be asserted without touching real BLE.

Coverage: coordinator.py 13.0% → 13.8% local (combined CI higher). 215 pure tests total.

## [0.4.0-alpha27] — 2026-05-27

README **Dependencies & What Gets Installed** section + 10 more lichess_analysis tests.

### Added (README)

- **Dependencies & What Gets Installed** section. Three tables: (1) automatic — `python-chess` pip install, HA's `bluetooth` component, Stockfish binary auto-download to `/config/phantom_chess/bin/`; (2) highly recommended — Mushroom + layout-card + card-mod HACS frontend plugins for the auto-provisioned dashboard; (3) what the user provides — HACS, BLE-capable Bluetooth surface, firmware-0.3.0+ board, optional Lichess account, optional TTS service.
- Also documented the **on-disk files** the integration writes: `/config/phantom_chess/bin/` (Stockfish cache), `/config/phantom_chess/debug/` (only when debug_dump option enabled), `.storage/lovelace.phantom_chess`, `.storage/lovelace_dashboards`.
- Fixed stale HA-floor claim in Hardware Requirements (was 2024.4.0, actually 2024.11.0 since alpha10).

### Added (tests)

- **10 more lichess_analysis tests**:
  - `_detect_libc` (4 cases) — no musl linker → glibc; each of the three musl linker paths → musl.
  - `StockfishFallback` init + class defaults (4 cases) — str vs Path bin_dir, initial state flags, conservative class constants.
  - `LichessAnalysisClient` + Stockfish integration (2 cases) — bin_dir creates fallback, no bin_dir skips it.

Coverage: `lichess_analysis.py` 40% → 43% in minimal env; 203 pure tests total.

## [0.4.0-alpha26] — 2026-05-27

ruff clean + gating CI step.

### Fixed (ruff baseline → zero)

- 17 auto-fixed: unused imports across multiple files, redundant `f""` strings, simple style issues.
- 6 hand-fixed: unused locals (`white_id`, `our_username`, `address_options` historical leftovers), one intentional late import in `coordinator.py` marked `# noqa: E402` with rationale, unused `TYPE_CHECKING` + `Platform` imports in `__init__.py` cleaned out.

### Changed (CI)

- **`ruff` CI job** added (gating — zero warnings required). Complements the existing `lint` job (which handles compileall + manifest.json / hacs.json shape) and the `typecheck` job (mypy) from alpha25.

### Result

The integration now passes a four-way clean baseline:
- **pytest**: 193 passing
- **mypy**: zero errors
- **ruff**: zero warnings
- **coverage**: 44.5% (well above the 43% fail_under gate)

## [0.4.0-alpha25] — 2026-05-27

mypy clean. Gating typecheck job in CI. Platinum `strict-typing` done.

### Fixed

- **All 17 mypy errors driven to zero.** Five real ones fixed (rest were resolved by the TypeAlias annotation in alpha24):
  - `_analysis_client` annotated `"LichessAnalysisClient | None"` with `TYPE_CHECKING` import (avoids runtime circular).
  - `_transport: Any` (concrete type is `asyncio.SubprocessTransport` but the integration only calls `.close()`).
  - `_entry` checks restructured as two-step null-narrow so mypy can track the guard.
  - `diagnostics.py` analysis_client deref restructured for the same reason.
  - `PhantomChessConfigFlow(... domain=DOMAIN)` suppressed with `# type: ignore[call-arg]` — HA's metaclass-registration kwarg pattern.

### Changed (CI)

- **`typecheck` job is now gating** (was `continue-on-error: true` in alpha24). Zero mypy errors required for the build to pass. The build broke if a future PR introduces a type bug.

### Quality scale: Platinum

- `strict-typing` → `done`.

**Platinum tier complete in code** — all 3 Platinum rules done. The integration officially meets every code-side quality scale rule across Bronze, Silver, Gold, and Platinum. Only blockers to claiming the tiers in the manifest: `brands` (Bronze, needs PR to home-assistant/brands) and `test-coverage` (Silver, needs 95% — currently ~44.5%).

## [0.4.0-alpha24] — 2026-05-27

Platinum prep — mypy CI baseline + Platinum rule audit.

### Added

- **`typecheck` CI job** — runs `mypy --ignore-missing-imports` on the integration package. Non-blocking (`continue-on-error: true`); logs the error count to the run summary so PRs that make things worse get flagged in review without failing the build.
- **`PhantomChessConfigEntry: TypeAlias = ConfigEntry[PhantomChessCoordinator]`** — adds the explicit `TypeAlias` annotation so mypy recognises the parameterised ConfigEntry as a type alias rather than a runtime value. Resolves the 3 most common alias errors.

### Quality scale (Platinum prep)

- `async-dependency` → `done`. All runtime deps are async-native or async-compatible (`chess` is sync but CPU-bound and fast; `aiohttp` and `bleak` are HA-bundled async).
- `inject-websession` → `done`. Both Lichess HTTP endpoints use `homeassistant.helpers.aiohttp_client.async_get_clientsession` — HA's shared session, not a new one.
- `strict-typing` → `todo`. mypy baseline is ~17 errors at alpha24; reaching zero with `--strict` is a multi-alpha effort. The biggest cleanups would be migrating coordinator's `dict[str, Any]` state to a TypedDict and tightening signatures across coordinator-side methods.

## [0.4.0-alpha23] — 2026-05-27

Fix: entity display names now use our translation strings instead of falling through to device_class names.

### Fixed

- **`translations/en.json`** added. `strings.json` is HA's dev-source file; HA at runtime reads `translations/<locale>.json` (typically generated by HA's dev tooling). Without the runtime file, every entity's `_attr_translation_key` was failing translation lookup, so display names fell through to either the device_class default (e.g. "Connectivity" instead of our "Connected") or empty. Fix: mirror `strings.json` to `translations/en.json` so the runtime lookups succeed.

### Why this matters

Verified post-alpha21 deploy: `binary_sensor.<id>_connected`'s `friendly_name` was rendering as "Connectivity" instead of "Connected" because HA was using the `BinarySensorDeviceClass.CONNECTIVITY` default name. The entity-translations migration in alpha20 only added strings.json entries; HA needs the file under `translations/` to actually read them. alpha23 ships that file.

## [0.4.0-alpha22] — 2026-05-27

Incremental coverage push — 12 more coordinator tests.

### Added (tests)

- `_build_move_speech` (5 cases): pawn-to-square, capture-uses-takes, kingside-castle, invalid-from-square → empty, black-move prefix.
- `_post_move_event_speech` (4 cases): check, checkmate, stalemate, nothing-to-announce.
- `_on_battery` parse path (3 cases): well-formed payload marshals correct (percent, charging) onto loop, malformed payloads are silent no-ops, charging=False when field 2 is "0".

Coordinator coverage: ~11% → ~13% in matrix-tests minimal env; ha-tests combined will be higher.

## [0.4.0-alpha21] — 2026-05-27

Gold `exception-translations` + `icon-translations` rules — all 22 Gold rules now `done` or `exempt`.

### Added

- **Exception translations.** `_get_coordinator`'s three `ServiceValidationError` raises now use `translation_domain` + `translation_key` + `translation_placeholders`. Messages live under `exceptions.no_board_configured` / `unknown_entry_id` / `ambiguous_target_entry` in `strings.json`. HA shows the translated message verbatim in the UI when a service call fails on a misconfigured target.
- **`icons.json`** — state-based icon translations for the entities where state-derived icons help (binary sensors swap on on/off; selects swap per option; sensors swap per firmware_mode / eval_source / matrix_status / last_move_classification value). Static `_attr_icon` left as fallback for older HA versions that don't read `icons.json`.

### Gold rules → `done`

`exception-translations`, `icon-translations`.

**Gold tier code-side compliance now complete.** Only `test-coverage` (95% threshold) and `brands` (PR to home-assistant/brands) remain across both Silver and Gold.

## [0.4.0-alpha20] — 2026-05-27

Gold `entity-translations` rule implemented.

### Changed

- **All 46 entities across 7 platforms** migrated from `_attr_name = "X"` literals to `_attr_translation_key = "x_snake_case"`. Display names now live in `strings.json` under `entity.<platform>.<key>.name`. English is the only shipped translation; translators can add new locales by dropping `translations/<locale>.json` alongside `strings.json` per HA's standard mechanism.

### Gold rules → `done`

`entity-translations`.

### Still todo on Gold

`exception-translations`, `icon-translations`.

## [0.4.0-alpha19] — 2026-05-27

Gold `reconfiguration-flow` rule implemented.

### Added

- **Reconfigure flow** in `config_flow.py`. Settings → Devices & Services → Phantom Chess Board → ⋮ → Reconfigure now opens a form pre-filled with the existing BLE address; the user can paste a fresh Lichess token to rotate it preemptively (without waiting for the reauth flow's 401 trigger). The BLE address field is read-only in intent — submitting a different MAC aborts with `ble_address_mismatch` (that's a different physical board, should be a new entry not a reconfigure).
- **4 new tests** for the reconfigure flow: happy-path token rotation, blank-token no-op, BLE-address-mismatch reject, invalid-token reject.
- **5 new abort/strings entries**: `reconfigure_successful`, `reconfigure_entry_not_found`, `ble_address_mismatch`, plus the `reconfigure` step's title/description/data labels.

### Gold rules → `done`

`reconfiguration-flow`.

### Still todo on Gold

`entity-translations`, `exception-translations`, `icon-translations` (3 translation-polish rules; the integration ships in English only).

## [0.4.0-alpha18] — 2026-05-27

Test-coverage push: lichess_analysis client parser + cache + AI-level-table.

### Added (tests)

- **10 new tests** in `tests/test_lichess_analysis.py` for `LichessAnalysisClient`:
  - `_parse_eval_payload` (6 cases) — empty PVs, white-to-move sign preserved, black-to-move flipped, mate flipped, single-move-no-space PV, no-moves-string → best_uci is None.
  - LRU cache eviction for both `_eval_cache` (256 entries) and `_opening_cache` (64 entries).
  - `_AI_LEVEL_TABLE` covers levels 1–8 with monotonically-non-decreasing Stockfish skill values.

Coverage: `lichess_analysis.py` 35% → 40% in the minimal env (combined coverage in CI will be higher since ha-tests exercises more paths).

## [0.4.0-alpha17] — 2026-05-27

Gold `entity-category` + `entity-device-class` audit pass.

### Added

- **`EntityCategory.CONFIG`** on every select (AI level, player color, setup mode, sculpture game) via `PhantomBaseSelect`, every number (mechanism speed, sound level, Lichess clock minutes/increment) via `PhantomBaseNumber`, and both switches (paused, training wheels). HA's UI now groups these under "Configuration" in the device page instead of mixing them with state sensors.
- **`SensorDeviceClass.DURATION`** + `SensorStateClass.MEASUREMENT` on `Lichess White Clock` and `Lichess Black Clock` sensors. Lets HA's frontend display them with the right unit-conversion options and chart formatting.

### Gold quality scale: 2 more rules → `done`

`entity-category` and `entity-device-class`.

### Still todo on Gold

`entity-translations`, `exception-translations`, `icon-translations` (translation polish), `reconfiguration-flow` (alpha19).

## [0.4.0-alpha16] — 2026-05-27

Gold-tier documentation pass. README expanded with six new sections; `quality_scale.yaml` updated to reflect 8 additional Gold rules as `done`.

### Added (README)

- **Use Cases** — six real workflows the integration is designed around: solo training, voice-driven game start, AI-vs-AI demo, correspondence play with notifications, sculpture mode for guests, accuracy tracking over time.
- **Supported Devices** — firmware 0.3.0+ supported, 0.3.2+ forward-compatible, pre-0.3.0 explicitly unsupported. Bluetooth surfaces compatibility matrix (Yellow / Green / Pi / proxy / USB dongle / Classic-only ✗).
- **Supported Functions** — every entity enumerated with description (sensors, binary_sensors, image, switches, numbers, selects, buttons).
- **Data Update Flow** — diagrams the two state-update paths: BLE notify on `UUID_SEND_MATRIX` → coordinator → entities, and Lichess Board API stream → coordinator. Explicitly notes the integration is `local_push`, with the 30s `update_interval` being a safety net.
- **Automation Examples** — four ready-to-paste blueprints: TTS announcement forwarder, resume-after-AI-failure, sculpture-on-arrival, accuracy `statistics-graph` card.
- **Known Limitations** — eight design / platform constraints documented up front (multi-board not heavily tested, `move_piece` bypasses validation, sculpture playback stub, AI-vs-AI hang at move ~45, cloud-eval rate limits, Stockfish download dependency, GATT cache staleness, no anonymous Lichess play).

### Gold quality scale: 8 additional rules → `done`

`devices`, `diagnostics`, `discovery`, `docs-data-update`, `docs-examples`, `docs-known-limitations`, `docs-supported-devices`, `docs-supported-functions`, `docs-troubleshooting`, `docs-use-cases`, `entity-disabled-by-default`. Plus `discovery-update-info`, `dynamic-devices`, `stale-devices` declared `exempt` with rationale.

### Still todo on Gold

`entity-category` (alpha17), `entity-device-class` (alpha17), `entity-translations`, `exception-translations`, `icon-translations`, `reconfiguration-flow` (alpha19).

## [0.4.0-alpha15] — 2026-05-27

HA Repair issue for missing HACS frontend dependencies. Satisfies Gold quality scale rule `repair-issues`.

### Added

- **Settings → Repairs entry** when the auto-provisioned `/phantom-chess` dashboard's HACS plugins (`mushroom-template-card`, `layout-card`, `card-mod`) aren't installed. Pre-alpha15 the dashboard rendered visually broken with no indication of why; now the user gets a clickable Repair entry naming the missing plugins. Issue is non-fixable (the fix is "install the HACS plugin"), severity WARNING, with a `learn_more_url` linking to the README's dashboard-deps section.
- **`_missing_frontend_deps(hass)`** helper in `dashboard_provision.py`. Substring-matches plugin names against the user's Lovelace resource URLs; accepts both dash and underscore variants since older HACS versions used them inconsistently. Tolerant of early-boot races (returns empty list rather than raising).
- **`_sync_frontend_deps_issue(hass)`** called at the end of `async_provision_dashboard`. Creates the issue if any deps are missing, deletes it if all present — so the issue auto-clears when the user installs the missing plugin and reloads the integration.

### Added (tests)

- **6 new tests** in `test_dashboard_provision.py` for the detection helper: all-present → empty, all-missing → all flagged, partial, underscore-variant accepted, no-LOVELACE_DATA → empty, iteration-error → empty.

### Internal notes

- Translation strings added under `issues.missing_frontend_deps` in `strings.json` (title + description, plus `{missing}` placeholder).
- README got an `id="chess-dashboard-frontend-dependencies"` anchor so the Repair's `learn_more_url` lands in the right section.

## [0.4.0-alpha14] — 2026-05-27

Incremental coverage push: 43.7% → 44.5%.

### Added (tests)

- **`tests/test_diagnostics.py`** — 19 tests covering the diagnostics module:
  - `_mask_ble_address` (8 cases) — happy-path, None passthrough, empty passthrough, three malformed-input cases.
  - `_mask_username` (6 cases) — happy-path, edge-length, too-short fallback, None.
  - `async_get_config_entry_diagnostics` (5 cases) — no coordinator branch, token redaction, address+username masking, loaded-coordinator state passthrough with noisy-key filtering, environment + stockfish blocks.

  Tests use `asyncio.run()` to avoid the `pytest-asyncio` dependency in the matrix-tests minimal env. `conftest.py` stubs `homeassistant.components.diagnostics.async_redact_data` so `diagnostics.py` loads in the minimal env.

### Changed (CI)

- **`fail_under` bumped 42 → 43** in `pyproject.toml`. Locks in the alpha14 baseline (44.5%) with a 1.5-point buffer.

### Test totals

- matrix-tests Py 3.12: 165 passed
- matrix-tests Py 3.13: 165 passed
- ha-tests: 176 passed
- **Total: 506 tests across all CI jobs.**

## [0.4.0-alpha13] — 2026-05-27

Incremental coverage push: 42.7% → 43.7%.

### Added (tests)

- **`tests/test_coordinator_state.py`** — 13 tests for the synchronous state mutators inside `PhantomChessCoordinator`:
  - `_apply_firmware_mode_state`: 6 cases covering mode-label storage, dedup of repeated values, separation of move events from mode labels (both `-` and `x` separators), fallthrough of unknown mode labels.
  - `_apply_battery_state`: 2 cases (percent + charging flag).
  - `_apply_matrix_state`: 3 cases (CLEAN payload populates state + dedup + piece_count recompute on empty board).
  - `_blank_state`: 2 cases (starting FEN populated + each call returns an independent dict).

  Tests bypass `__init__` via `__new__` and stub only the instance attributes the mutators actually need. This avoids `DataUpdateCoordinator.__init__`'s HA scheduling requirements.

### Changed (CI)

- **`fail_under` bumped from 40 → 42** in `pyproject.toml`. Locks in the alpha13 baseline; the 1.7-point buffer below 43.7% absorbs noise from future test additions that don't change coverage.

### Coverage snapshot (alpha13)

```
TOTAL                      43.7%   (was 42.7%)
coordinator.py             ~11%    (was 9% — +1.6pp from the 13 new tests)
```

Test totals across CI: 146 matrix-tests on Py 3.12, 146 on Py 3.13, 157 ha-tests = **449 passing tests**.

## [0.4.0-alpha12] — 2026-05-27

Test-coverage push: 9% → 42.7% combined coverage. CI now has a coverage.py gate at 40% fail_under as a regression guard.

### Added (tests)

- **`tests/test_lichess_analysis.py`** — 45 tests covering the module's pure-function surface: `_safe_fen_for_eval` (5 cases), `EvalResult.white_win_pct` / `from_mover_view` (10 cases), `classify_move` (10 cases across every classification band + edge cases), `classification_color_glyph` (8 parametrized + fallback), `detect_fork` (4 scenarios), `_win_pct_loss_to_accuracy` (4 cases), `compute_game_accuracy` (3 cases). Module coverage: 0% → 39%.
- **`tests/test_coordinator_helpers.py`** — 29 tests covering pure functions in `coordinator.py`: `_phantom_to_uci` parser (firmware notation → UCI, 10 cases), `_rotate_uci_180` 180° transform (involution property, promotion preservation, short-input handling, 8 cases), `PhantomChessCoordinator._coarse_accuracy` staticmethod (5 cases), `PhantomChessCoordinator._describe_mistake` staticmethod (6 cases). Coordinator coverage: 0% → 9%.
- **5 new config-flow edge-case tests** in `tests/test_config_flow.py`: invalid MAC format, Lichess network error, reauth with invalid replacement token, v1→v3 migration canonicalises MAC, v3→v4 migration bumps version.

### Added (CI)

- **coverage.py wired into the ha-tests CI job.** `coverage run -m pytest` records hits across the whole package; `coverage report` enforces the `fail_under = 40` threshold set in `pyproject.toml`. Build fails if combined coverage drops below 40%.
- **`pyproject.toml`** got `[tool.coverage.run]` + `[tool.coverage.report]` sections. Source scoped to `custom_components/phantom_chess`. Excluded patterns: `pragma: no cover`, `raise NotImplementedError`, `if TYPE_CHECKING:`, `if __name__ == "__main__":`, defensive `except ImportError:` branches.
- **Conftest extended** to stub HA modules `bluetooth`, `aiohttp_client`, `issue_registry`, `update_coordinator` so coordinator.py can be loaded for unit testing in the minimal matrix-tests environment. `DataUpdateCoordinator` stub supports `__class_getitem__` for the generic-subscript syntax at class-definition time.

### Coverage snapshot (alpha12)

```
matrix.py                  93%   (was 93%)
number.py                  87%   (was 0%, ha-tests entity setup covered it)
select.py                  88%   (was 0%)
switch.py                  86%   (was 0%)
sensor.py                  79%   (was 0%)
dashboard_provision.py     covered via offline + ha-tests
image.py                   47%   (was 0%)
lichess_analysis.py        39%   (was 0%)
coordinator.py             9%    (was 0%)
__init__.py                covered by ha-tests setup
diagnostics.py             0%    (no tests)
TOTAL                      42.7%
```

### Internal notes

- Silver's `test-coverage` rule asks for >95%; we're at 42.7%. Reaching 95% needs BLE-mocked coordinator integration tests (most of the remaining 1680 uncovered lines are in coordinator.py's async BLE paths). Tracked as future work; the rest of Silver is otherwise compliant.
- Test totals: 133 in matrix-tests (Py 3.12 + 3.13), 144 in ha-tests, 7 lint checks. Run time: ~10s matrix, ~1m30s ha-tests.

## [0.4.0-alpha11] — 2026-05-26

Silver quality-scale compliance pass — code-side. 9 of 10 Silver rules now done; only `test-coverage` (95% threshold) remains.

### Added / Changed

- **`entity-unavailable`.** Each platform's base entity class overrides `available` to also check `self.coordinator.is_ble_connected` — entities go unavailable when the board drops off BLE. `PhantomConnectedSensor` overrides back to always-available so users keep a visible connectivity indicator. Pure-local config entities (Lichess clock minutes/increment, the entire select platform, training_wheels switch) also stay always-available so users can pre-configure while the board is off.
- **`parallel-updates`** declared per platform. `0` for read-only platforms (binary_sensor, sensor, image) and pure-local select; `1` for action-issuing platforms (button, number, switch) so concurrent BLE writes on the single GATT client are serialised.
- **`action-exceptions`.** `_get_coordinator` now raises `ServiceValidationError` (was: `vol.Invalid`) for "no board configured" / "wrong entry_id" / "ambiguous target" cases. ServiceValidationError is the documented type for input-validation failures and HA surfaces the message verbatim in the UI.
- **`log-when-unavailable`.** `_ble_loop` only logs the "BLE connection lost" WARNING on the first retry of an outage cluster (`first_failure_of_cluster` flag). Subsequent retry attempts during the same outage stay DEBUG-level. Pre-alpha11 every retry logged WARNING, which spammed the system log during multi-hour board-off periods.
- **`docs-configuration-parameters`** + **`docs-installation-parameters`.** README "Configuration Options" rewritten to enumerate every options-flow field (incl. the missing `auto_provision_dashboard` from alpha4). New "Setup parameters" subsection documents the two config-flow fields (BLE address, Lichess token) with format + where-to-find guidance. Also added a "Rotating the Lichess token" subsection because the token lives in entry data and is rotated via the reauth flow, not options.
- **Six missing service descriptions** filled in (`services.yaml`): `move_piece`, `start_sculpture`, `reset_position`, `play_sound`, `request_hint`, `dismiss_review`. Pre-alpha11 these appeared with empty descriptions in HA's developer-tools/services UI.

### Internal notes

- `quality_scale.yaml` now declares 14 Bronze + 9 Silver rules `done`, 2 Bronze `exempt`, and 2 rules `todo` (`brands` for Bronze submission; `test-coverage` for Silver submission).
- Test-coverage push toward Silver's 95% is the next multi-alpha workstream.

## [0.4.0-alpha10] — 2026-05-26

Audit + close-out pass against HA's Bronze quality scale checklist. Code-side work is complete; only the brand-assets PR to home-assistant/brands remains before the integration can officially claim Bronze.

### Changed

- **`runtime-data` migration.** Per-entry coordinator state moved from `hass.data[DOMAIN][entry.entry_id]` to `entry.runtime_data`. HA now garbage-collects the coordinator on unload automatically, and the typed alias `PhantomChessConfigEntry = ConfigEntry[PhantomChessCoordinator]` gives type-checkers visibility into the stored value. Touched: `__init__.py` (setup/unload/remove paths + service-handler routing), all seven platform `setup_entry` functions (`binary_sensor.py`, `button.py`, `image.py`, `number.py`, `select.py`, `sensor.py`, `switch.py`), and `diagnostics.py`.
- **`action-setup` migration.** Service actions (`phantom_chess.*`) now register in `async_setup` instead of `async_setup_entry`. They're discoverable before any config entry loads and survive entry reloads without re-registration. `async_unload_entry`'s service-removal block is gone for the same reason. `_get_coordinator` resolves the target entry at call time by enumerating `hass.config_entries.async_entries(DOMAIN)`.
- **Bronze HA floor.** Minimum HA version bumped from 2024.4 to 2024.11 (`hacs.json`). `entry.runtime_data` landed in 2024.11; the alpha9 options-flow fix also already implicitly required this floor.

### Added

- **`quality_scale.yaml`.** Every Bronze rule explicitly tracked with `done` / `exempt` / `todo` status and a rationale. Three rules are non-`done`: `brands` (todo — needs separate PR to home-assistant/brands), `appropriate-polling` (exempt — local-push integration), `test-before-setup` (exempt — BLE local-push; eager-fail would be hostile during board reboots).
- **`data_description` blocks** added to the `user`, `lichess_token`, and `reauth_confirm` config-flow steps in `strings.json`. Tells the user what each field means inline in the form, satisfying the Bronze `config-flow` rule's sub-requirement.
- **README "Removal" section.** Step-by-step instructions for cleanly removing the integration, including what files at `/config/phantom_chess/` are NOT removed automatically (Stockfish binary cache, debug dumps). Satisfies the Bronze `docs-removal-instructions` rule.
- **Bluetooth-discovery config-flow tests.** `test_bluetooth_discovery_creates_entry` and `test_bluetooth_discovery_aborts_when_already_configured` cover the discovery path that the existing 4 tests didn't reach. Brings `config-flow-test-coverage` to "all entry paths covered".
- **`CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)`** in `__init__.py` — explicit declaration that the integration accepts no YAML configuration. Convention since HA 2024.4.

### Known Bronze gap

- **`brands`** — the integration uses HA's default placeholder icon in Settings → Devices & Services. Properly fixing this requires submitting `icon.png` + `icon@2x.png` + `logo.png` + `logo@2x.png` to the [home-assistant/brands](https://github.com/home-assistant/brands) repo. Tracked as a v0.4 follow-up; the integration is functionally Bronze-compliant in code but cannot claim the tier until brands lands.

## [0.4.0-alpha9] — 2026-05-26

CI workflow added; surfaces a real options-flow bug that's been latent since HA 2025.12.

### Fixed

- **Options flow no longer crashes on HA 2025.12+.** `PhantomChessOptionsFlow.__init__` was assigning `self.config_entry = config_entry`. HA 2024.11 made `OptionsFlow.config_entry` a property; HA 2025.12 removed its setter. On HA 2025.12 and later (Luke's HA is 2026.5.4), opening Settings → Devices & Services → Phantom Chess → Configure raised `AttributeError: property 'config_entry' of 'PhantomChessOptionsFlow' object has no setter` and the options dialog never rendered. Fix: drop the `__init__`; HA auto-wires `config_entry` on the parent class. The `async_get_options_flow` factory now returns a no-argument instance.

### Added

- **GitHub Actions CI** (`.github/workflows/test.yml`). Three jobs:
  - `matrix-tests`: pure-function tests for `matrix.py` on Python 3.12 and 3.13. Fast (~10s).
  - `ha-tests`: HA-integration tests via `pytest-homeassistant-custom-component`. Marked `continue-on-error: true` since the HA test plugin tracks core releases tightly.
  - `lint`: `compileall` + `manifest.json` / `hacs.json` shape validation.
- **`tests/conftest.py` stub** for matrix-tests: pre-stages pure-function submodules into `sys.modules` so `from custom_components.phantom_chess.matrix import ...` works in a minimal environment without running the package's HA-heavy `__init__.py`. Detects "minimal environment" by whether `voluptuous` is importable.
- **`tests/conftest.py` autouse fixture** `auto_enable_custom_integrations` so every HA test gets `custom_components/phantom_chess` registered with the test framework without per-test boilerplate.
- **`pyproject.toml`** with `asyncio_mode = "auto"` so HA async tests don't need per-test `@pytest.mark.asyncio` decorators.

### Internal notes

- CI's first run after `gh release create v0.4.0-alpha8` was the catalyst: the new ha-tests caught the options-flow bug at line 320 of `config_flow.py`. Without CI, the bug would've stayed latent until the next time Luke (or any other user) tried to open the integration's options dialog.
- After this alpha lands, 24/25 ha-tests should pass green; the previously-failing `test_options_flow_round_trip` becomes one of the passing ones.

## [0.4.0-alpha8] — 2026-05-26

Action tiles converted to `type: button` cards — the icon-popup bug class is now eliminated at the root rather than worked around.

### Changed

- **Auto-provisioned dashboard's action tiles are now `type: button` cards.** alpha7 patched the "icon click opens the connected-sensor history popup" bug by mirroring `tap_action` into `icon_tap_action` on each `type: tile`. The button card has no separate icon action — the whole card shares one `tap_action` — so converting eliminates the bug class entirely instead of working around it. The icon_tap_action mirror is gone.
- **21 action tiles converted, 17 left as tiles.** Conversions cover:
  - 15 script-entity button tiles (Back to modes, Start Lichess game, Play selected sculpture, Takeback, Resign, Request hint, all back-to-modes variants).
  - 1 alpha7-shape native-service tile (Start game vs Stockfish).
  - 4 mode-picker tiles (Play with Lichess, Play with Stockfish, Sculpture Library, 2-Player Game) — these also had the icon-popup bug, since clicking the icon opened the setup_mode entity history. alpha7's mirror skipped them because the tap_action service was `select.select_option`, not `phantom_chess.*`.
  - 1 "New game" post-review button.

  Tiles left untouched: Bluetooth-connection info, Firmware-state info (×7), Last move / Pieces / State info tiles, AI level / My color / Choose-a-game / training-wheels controls (5 tiles with `features:` or `tap_action: toggle`). These have legitimate state-display semantics and shouldn't be buttons.

### Changed (internal)

- **`_fixup_script_tiles` replaced by `_convert_action_tiles_to_buttons`.** Detection rules expanded to recognise four button shapes (bare script-entity, script-entity with text-rewritten tap_action, alpha7 connected-sensor shape, mode-picker `select.select_option`). Each converted card drops tile-only fields (`entity`, `hide_state`, `vertical`, `icon_tap_action`) and gains an explicit `show_state: false` for intent.
- **`build_dashboard_config` no longer resolves a `connected_entity` for the dict pass.** The button card has no entity binding, so the variable is unused.
- **`confirmation` blocks on `tap_action` preserved** through conversion (Resign button retains its "Resign this game?" prompt).

### Internal notes

- The bundled template stays unchanged — it's still close to v0.3's `examples/dashboard-rich.yaml` so future merges from the v0.3 reference stay diffable. The button conversion lives entirely in the renderer.
- Validated offline via `outputs/validate_renderer.py`: 21 buttons, 17 surviving tiles, no residual `script.phantom_*`, no `icon_tap_action`, no `script.turn_on`, no `input_*` helpers, no `YOUR_BOARD_MAC` placeholders.

## [0.4.0-alpha7] — 2026-05-26

Visual polish for the auto-provisioned dashboard.

### Fixed

- **Removed the red "Configuration error" badge** that appeared at the top of `/phantom-chess`. Caused by a single empty-content markdown card the original v0.3 template used as a layout spacer in a 2-column grid. Changed `content: ''` to `content: ' '` so HA's card-validation passes.
- **Tile-as-button icon clicks now trigger the action**, not the entity-history popup. The dashboard's tile cards (Back to modes, Resign, Takeback, Request Hint, etc.) were rewritten from v0.3 `script.phantom_X` entities to `binary_sensor.X_connected` (purely for icon display), and the rewritten tap_action correctly invoked the native `phantom_chess.X` service — but clicks on the **icon** still defaulted to HA's "show more info" popup. Renderer now mirrors the tap_action into `icon_tap_action`, so icon and body clicks behave the same.
- **Tiles that lacked an explicit tap_action** (a few "Back to modes" buttons in the original template relied on the script-entity to be the action) now get a proper tap_action injected during render, instead of becoming silent no-op buttons.

### Changed (internal)

- **Renderer split into text-level + dict-level passes.** Helper rewrites, service-domain rewrites, and entity-id resolution remain text-level. Script-tile entity rewriting moved to dict-level (`_fixup_script_tiles`), which walks the parsed config and rewrites every `entity: script.phantom_X` tile in one pass — injecting `entity`, `hide_state`, `tap_action`, AND `icon_tap_action` atomically. This made it possible to distinguish button-tiles from informational tiles (e.g. the legitimate "Bluetooth connection" tile that should still open the entity popup on icon click).

## [0.4.0-alpha6] — 2026-05-26

Second blank-dashboard fix. The alpha5 substitution covered the v0.3 `board_idle` template helper but didn't address a deeper issue: **HA assigns entity_ids differently depending on when each entity was first registered**. Old entities (v0.3) ended up with the MAC slug (`binary_sensor.phantom_c8_c9_a3_f2_7c_0a_connected`) while the alpha1–3 entities got the device-name slug (`select.phantom_6552_setup_mode`). The dashboard renderer was predicting all entity_ids from the MAC, so the alpha entities (mode picker, sculpture picker, training-wheels toggle, clock numbers, board-idle sensor) all came out as broken references — every conditional that gated them stayed false, and the dashboard rendered blank.

### Fixed

- **Dashboard renderer now resolves entity_ids from the entity registry** rather than predicting them from the MAC slug. At provision time, the renderer walks `entity_registry.async_get(hass).entities`, builds a `(domain, unique_id_suffix) → entity_id` map for the board's phantom_chess entities, and substitutes real entity_ids into every templated reference. Works regardless of which slug pattern HA picked for each entity, and survives device renames.
- **`board_idle` binary_sensor's periodic refresh callback now `@callback`-decorated.** In HA 2026+, plain lambdas passed to `async_track_time_interval` are scheduled as executor jobs (worker-thread) and calling `async_write_ha_state` from there raises a thread-safety RuntimeError. v0.4-alpha5 hit this 66 times in a single restart. Fix: replace the lambda with a `@callback`-decorated method.
- **Template file load no longer blocks the event loop.** `Path.read_text()` moved into `asyncio.to_thread`, removing the HA "blocking call" warning that alpha5 spammed at every config-entry setup.

### Internal notes

- The entity-id-divergence problem surfaced after Luke's board was renamed from "Phantom <MAC>" to "Phantom 6552" mid-development. HA preserves an entity's original entity_id even after the device name changes, so the v0.3 entities stayed on `phantom_<MAC>_*` slugs while the alpha1-3 entities were registered fresh against the new device name. Predicting slugs from a single source string is therefore unreliable — registry lookup is the only correct approach.
- The renderer test in `outputs/test_template.py` (development-time check) still validates the YOUR_BOARD_MAC slug fallback; a future addition will mock the registry to validate end-to-end resolution.

## [0.4.0-alpha5] — 2026-05-26

Fixes the alpha4 blank-dashboard bug.

The auto-provisioned `/phantom-chess` dashboard in alpha4 rendered blank: every conditional card was hidden because every conditional had a clause requiring `binary_sensor.phantom_chess_board_idle == "on"`, the v0.3 template-helper entity name. alpha3 replaced that template helper with a per-device native `binary_sensor.phantom_<MAC>_board_idle`, but the dashboard template renderer's substitution map missed the rewrite.

### Fixed

- **Dashboard auto-provision now substitutes `binary_sensor.phantom_chess_board_idle` → `binary_sensor.phantom_<MAC>_board_idle`** during template rendering. Fresh installs and reloads will re-provision the dashboard with the correct entity reference; existing alpha4 dashboards will be overwritten on the next setup_entry.
- Standalone renderer test now asserts there are zero residual references to the v0.3 entity name in the rendered output.

### Internal notes

- Caught while debugging Luke's HA — the alpha4 dashboard installed correctly (sidebar entry, 49KB config persisted) but rendered blank because every top-level conditional required the missing entity. Reminder for the test suite roadmap: add a "rendered dashboard parses + every condition references an entity that the integration creates" assertion next alpha.

## [0.4.0-alpha4] — 2026-05-25

The dashboard auto-installs.

After this alpha, a fresh HACS install of the integration drops a fully-themed **Chess** dashboard at `/phantom-chess` in the sidebar — no copy-paste, no find-and-replace on `YOUR_BOARD_MAC`, no `examples/helpers.yaml`, no companion scripts. Just install the integration, pair your board, and the dashboard appears immediately (no HA restart required).

### Added

- **Auto-provisioned `/phantom-chess` dashboard.** The 1083-line "rich" dashboard from v0.3's `examples/dashboard-rich.yaml` is now bundled inside the integration package (`custom_components/phantom_chess/dashboard_template.yaml`) and rendered on every config-entry setup against your board's MAC slug. The rendered config is written to Lovelace storage (`.storage/lovelace.phantom_chess`) and registered in `.storage/lovelace_dashboards`, so it persists across HA restarts and integration reloads. Idempotent — second setups update the existing dashboard rather than duplicating it.
- **Options-flow toggle `auto_provision_dashboard`** (default: on). Power users who want to hand-author their own dashboard can flip this off in Settings → Devices & Services → Phantom Chess → Configure; on the next reload, the integration removes the auto-provisioned `/phantom-chess` so you're working from a clean slate.

### Changed

- **`async_remove_entry`** now cleans up the auto-provisioned dashboard so a clean reinstall starts fresh. Unloading (e.g. for a HACS update) keeps the dashboard intact — only the explicit "Delete integration" action triggers removal.

### Migration notes for v0.3 users

If you have the v0.3 `examples/dashboard-rich.yaml` already pasted into a manual dashboard, the alpha4 auto-provisioned dashboard installs **alongside** it at the new `/phantom-chess` URL — your existing dashboard isn't touched. To switch over, delete your manual dashboard from Settings → Dashboards.

If you have the v0.3 setup pack scripts installed (`examples/scripts.yaml`), they continue to work but the auto-provisioned dashboard no longer references them — every tap calls a native `phantom_chess.*` service instead. The scripts are safe to leave installed or to delete.

### Internal notes

- The integration writes directly to `.storage/lovelace_dashboards` rather than calling `DashboardsCollection.async_create_item()` because the collection object is held only inside a closure in `homeassistant.components.lovelace.async_setup` with no public accessor. Direct storage writes match what the collection persists.
- Template substitutions are text-level rather than YAML-level, so the bundled template stays human-readable and diff-friendly. Substitutions cover the MAC slug, the v0.3 `input_*` helper entities → v0.4 native entities, helper service domains (`input_select.select_option` → `select.select_option`), and four shapes of script tile/tap-action that the rich dashboard uses interchangeably.
- The dashboard depends on three HACS frontend resources for full visual fidelity: `custom:mushroom-template-card`, `custom:layout-card`, and `card-mod`. Without them the dashboard renders functional cards with broken styling. A future alpha will surface this via the HA repair issue system.

## [0.4.0-alpha3] — 2026-05-26

Drops the last remaining hand-rolled helper (the template binary sensor) AND the 6 of 7 companion scripts the v0.3 dashboard depended on. The dashboard YAML still needs to ship in `examples/` and be pasted by the user, but it no longer requires ANY companion helpers/scripts — every entity it references is created by the integration.

The next alpha (alpha4) lands the auto-provisioning of the dashboard itself.

### Added

- **`binary_sensor.<device>_board_idle`** — replaces the template `binary_sensor.phantom_chess_board_idle` from v0.3's `examples/helpers.yaml`. Returns `on` only when the firmware has been stable for ≥60 seconds (drives the dashboard's mode-picker-vs-live-game gating; without it, the dashboard flickers during sculpture playback). Implementation tracks `firmware_last_move_updated` and re-evaluates every 5 seconds via `async_track_time_interval` so the True transition fires within 5s of the threshold, even without coordinator events.
- **Service `phantom_chess.back_to_modes`** — resets the mode picker to "Choose a mode" and clears the post-game review flag. Replaces `script.phantom_back_to_modes`.
- **Service `phantom_chess.start_lichess_configured`** — starts a Lichess game using the integration's currently-set `select` + `number` entities for color, AI level, and clock controls. Replaces `script.phantom_start_lichess_configured`.
- **Service `phantom_chess.play_selected_sculpture`** — v0.4-alpha3 STUB. Enters sculpture mode and fires a persistent notification naming the selected game. Per-game move sequences will land in a later alpha; for now, users with v0.3's per-game `script.phantom_sculpture_*` scripts can call them directly.

### Migration notes for v0.3 users

After updating to alpha3, the bundled dashboard YAML can call services instead of routing through scripts. The mapping:

- `script.turn_on script.phantom_back_to_modes` → `phantom_chess.back_to_modes`
- `script.turn_on script.phantom_start_lichess_configured` → `phantom_chess.start_lichess_configured`
- `script.turn_on script.phantom_play_selected_sculpture` → `phantom_chess.play_selected_sculpture` (stub for now)
- `script.turn_on script.phantom_takeback` → `phantom_chess.takeback`
- `script.turn_on script.phantom_resign` → `phantom_chess.resign`
- `script.turn_on script.phantom_request_hint` → `phantom_chess.request_hint`

The voice-Assist script (`script.phantom_chess_play`) stays — it's a script for Assist integration, not a dashboard control.

For the `binary_sensor` migration, update dashboard references from `binary_sensor.phantom_chess_board_idle` (template helper) → `binary_sensor.<your_device>_board_idle` (integration entity). Then delete the template helper.

## [0.4.0-alpha2] — 2026-05-26

Drops 3 more hand-rolled helpers from the v0.3 setup pack. After this alpha, only one helper (the template binary sensor for the 60s-idle gate) and the 7 companion scripts remain manual. v0.4-alpha3 will land the binary sensor; alpha4 onward will start retiring the scripts.

### Added

- **`switch.<device>_training_wheels`** — replaces `input_boolean.phantom_chess_training_wheels`. Persists across HA restarts.
- **`number.<device>_lichess_clock_minutes`** — replaces `input_number.phantom_chess_lichess_clock_minutes`. Range 1–180 (covers everything from bullet-ish to classical), default 30, box-mode UI.
- **`number.<device>_lichess_clock_increment`** — replaces `input_number.phantom_chess_lichess_clock_increment`. Range 0–180 seconds per move, default 0.

### Migration notes for v0.3 users

The old `input_boolean` / `input_number` helpers and the new `switch` / `number` entities coexist. To migrate the dashboard:

- `input_boolean.phantom_chess_training_wheels` → `switch.<your_device>_training_wheels`
- `input_number.phantom_chess_lichess_clock_minutes` → `number.<your_device>_lichess_clock_minutes`
- `input_number.phantom_chess_lichess_clock_increment` → `number.<your_device>_lichess_clock_increment`

Then delete the three old helpers and restart HA.

## [0.4.0-alpha1] — 2026-05-25

First step of the Option C work (auto-provisioning everything the rich dashboard needs). Drops 2 of the 5 hand-rolled helpers `examples/helpers.yaml` previously required: the mode picker and the sculpture-game picker are now integration-owned `select` entities, created automatically when the integration sets up a config entry.

### Added

- **`select.<device>_setup_mode`** — replaces the hand-rolled `input_select.phantom_chess_setup_mode` helper. Options: "Choose a mode", "Play with Lichess", "Play with Stockfish", "Sculpture Library", "2-Player Game". State persists across HA restarts via `RestoreEntity`.
- **`select.<device>_sculpture_game`** — replaces the hand-rolled `input_select.phantom_chess_sculpture_game` helper. 18 famous historical games as options (catalog in `const.py:SCULPTURE_GAMES`; future games can be added there without a breaking change). State persists across HA restarts.

### Changed

- **README — "The Chess Dashboard" section reframed.** Previously called the rich dashboard "optional"; that framing was wrong. The Phantom Chess Board is a single-purpose device with one sensible UI; the dashboard is the user-facing surface of the integration, not a bonus. Updated wording. Auto-provisioning roadmap noted explicitly: v0.4 will reduce the setup to "install Mushroom + layout-card, done."

### Migration notes for v0.3 users

Both v0.3's `input_select` helpers and v0.4's `select` entities will coexist on existing installs. The dashboard still references `input_select.phantom_chess_setup_mode` and will keep working. To migrate to the integration-owned versions:

1. Update `examples/dashboard-rich.yaml` (or your dashboard YAML) to reference `select.<your_device>_setup_mode` instead of `input_select.phantom_chess_setup_mode`.
2. Same for `input_select.phantom_chess_sculpture_game` → `select.<your_device>_sculpture_game`.
3. Delete the two old `input_select` helpers from `configuration.yaml` (or from Settings → Helpers if you created them via UI).
4. Restart HA.

A future alpha will ship an updated dashboard YAML that uses the new entity names by default.

## [0.3.0-beta3] — 2026-05-25

### Added

- **Rich-dashboard setup pack (Option B+).** Three new files in `examples/` plus a "Rich Dashboard Setup" section in the README walk users through ~5 minutes of copy/paste to get the maintainer's full daily-driver dashboard: mode-picker → contextual sub-mode cards (Lichess / Stockfish / Sculpture / 2-Player) → live-game cards with eval bar and move classifications → post-game review with top mistakes → embedded drag-drop interactive board.
  - `examples/helpers.yaml` — input_selects (mode-picker, sculpture-game chooser), input_boolean (training-wheels toggle), input_numbers (Lichess clock controls), template binary sensor (60s-idle gate). Paste into `configuration.yaml`.
  - `examples/scripts.yaml` — 7 companion scripts (back_to_modes, takeback, resign, request_hint, play_selected_sculpture, start_lichess_configured, chess_play). Paste into `scripts.yaml`.
  - `examples/dashboard-rich.yaml` — full ~1000-line Lovelace YAML. Paste into the dashboard's raw-config editor.
  - All three files use `YOUR_BOARD_MAC` as a placeholder; users find/replace with their own MAC slug (visible in any phantom_chess entity_id).
  - **External requirements:** Mushroom + layout-card HACS frontend plugins. Documented in README.
  - **Roadmap:** v0.4 will auto-provision all of this via the integration's setup flow (no copy/paste, no find/replace) — see Option C in the project's task tracker.

## [0.3.0-beta2] — 2026-05-25

### Fixed

- **Config flow's Bluetooth-discovery path raised "Unknown error occurred" on the confirm step.** When the Bluetooth-confirm form was submitted, the empty `user_input` dict was being forwarded straight into the Lichess-token step, which then tried `user_input[CONF_LICHESS_TOKEN]` → `KeyError` → HA surfaced as "Unknown error occurred" in the UI with no useful detail. Existing installs that originally went through the manual-MAC path never saw this because they advanced via `async_step_user → async_step_lichess_token()` (no user_input forwarded). The bug only manifested on a true fresh install from BT discovery — exposed by the v0.3.0-beta1 clean-install validation pass. Fix: `async_step_bluetooth_confirm` now calls `async_step_lichess_token()` with no argument, so the token step renders its own form first.

## [0.3.0-beta1] — 2026-05-25

First public beta. Focus areas: protocol correctness against firmware 0.3.0, multi-game stability, release infrastructure (HACS-compatible repo layout, CI). Builds on the internal v0.2.0 release-readiness pass with several round-of-fixes after Efraín's 2026-05-24 protocol clarifications.

### Added

- **AI-vs-AI play mode (`phantom_chess.start_ai_vs_ai_game` service).** Stockfish plays both sides on the physical board, with the magnet driving every move. Useful as a "watch the AI play itself" demo (the dashboard's learning view, eval bar, classifications, and post-game review all render in real time as the game progresses) and as an autonomous-testing harness — runs a full game without anyone at the board, exercising castling, captures, promotions, and BLE timing edges that single-game testing misses. Per-side `white_ai_level` / `black_ai_level` (default to the integration's current ai_level select) and `move_delay_seconds` (default 1.5s; lower runs faster but stresses the magnet harder) are settable. Game runs until checkmate / stalemate / draw or `phantom_chess.stop_local_game` is called.
- **Training-wheels move-quality glyph overlay.** When `input_boolean.phantom_chess_training_wheels` is ON, the board image overlays the most-recent move's classification glyph (`!!`, `!`, `?!`, `?`, `??`) on the destination square. Color matches the move-history panel (e.g. red for blunders, green for brilliants). Glyph is suppressed for `best`, `book`, and `unknown` classes to avoid clutter. Cached image invalidates on classification change or toggle flip.
- **Interactive dashboard board (Task #27).** Bundled self-contained HTML page at `/phantom_chess_static/board.html` — drag-drop pieces from any HA dashboard to physically move them on the board (the magnet executes the move just like a Lichess AI move). Embed via Lovelace `iframe` card; example added to `examples/dashboard.yaml`. Uses cm-chessboard ES module from jsDelivr CDN. Auth via HA long-lived access token (one-time setup, stored per-browser in localStorage). Promotion-piece selector built in. Locks input while opponent is thinking or no game is active. Backed by new `phantom_chess.execute_move` service (UCI + optional promotion + optional entry_id) that validates legality and side-to-move before driving the magnet.
- **Dashboard-input metadata on the Live Position sensor.** New attributes `our_color`, `side_to_move`, `last_move`, `lichess_active`, `local_game_active`, `game_status`. Used by the interactive board to gate input and orientation; useful for other dashboard cards too.

### Changed

- **Diagnostic buttons in `button.py` are now disabled-by-default in the entity registry.** `PhantomStartGameButton` ("Start Game") drives just the firmware-side BLE snapshot protocol — it does NOT wire up a Lichess or local-Stockfish game, so pressing it during normal use leaves the firmware in "BLE Playing" with no game backend and the dashboard goes blank because the in-game cards have nothing to render. Marked as `EntityCategory.DIAGNOSTIC` and `_attr_entity_registry_enabled_default = False`. `PhantomMovementVerifyButton` already had the disable flag; added the diagnostic category to match. Existing installs that enabled the button via the device page will continue to see it; new installs won't. The user-facing gameplay surface is the `phantom_chess.start_local_game` and `phantom_chess.start_game` services (surfaced as tiles in `examples/dashboard.yaml`). Press-handler log lines also demoted from WARNING to INFO for the same reason — these buttons are no longer part of normal operation.
- **Renamed `UUID_GAME_CONFIG` → `UUID_SCULPTURE` (7eeaef37) per protocol clarification from Efraín 2026-05-24.** That characteristic is named `UUID_SCULPTURE` in firmware source; the integration's previous name reflected a reverse-engineering guess from app blutter output, not firmware reality. The firmware does NOT interpret the byte the integration historically wrote here as `(ai_level<<1)|color_bit` — that encoding was app-side only. The Lichess-game-start write of that byte was therefore a no-op and has been removed; AI level + color are signaled authoritatively via the GAME_START opcode 0 snapshot matrix+side flag and (for Lichess) the POST /api/challenge/ai payload. The DISCOVERY probe-read of the characteristic is retained because it still surfaces GATT staleness via the shared `_handle_gatt_staleness` helper, but the byte value is no longer interpreted. `UUID_GAME_CONFIG` remains as a deprecated backward-compat alias for one release cycle.
- **Added `UUID_SLIDE_DELAY` (b5a650ea-…-0004; NEW in firmware 0.3.2 per Efraín 2026-05-24).** Distinct from `UUID_BOARD_ROTATION` (`…-0002`) by a single octet. Integer-as-string ms, range 0–1500, default 250.
- **Documented `UUID_BOARD_ROTATION` payload as integer-string degrees `"0"/"90"/"180"/"270"`** (correcting earlier note that called it a 0/1 boolean). Writing triggers a board restart.
- **Documented sculpture-mode chunked transfer protocol** (per Efraín 2026-05-24, in `phantom-chess-protocol.md`). Chunking is used only on `UUID_SCULPTURE` for opcode 9 (PLAYLIST) and opcode 11 (DATABASE); `UUID_GAME` never chunks. The integration's current game-channel payloads always fit in a single packet so chunking isn't implemented yet, but the format is recorded for future sculpture-mode work.

### Fixed

- **"Board stops responding after a castle" (Luke's diagnosis, 2026-05-25).** Castling drives the magnet through TWO piece movements (king e1→g1 + rook h1→f1 for white kingside). The firmware fires one `\x03M` sensor notification for each piece. The previous AI-echo suppression only registered the primary UCI (`e1g1`), so the rook's `\x03M 1 h1-f1` notification was treated as a phantom human move. The legality check correctly rejected it (rook from white isn't legal for black to move) so `self._board` wasn't corrupted, BUT the discovery callback's unconditional `movementVerify` ack still fired, telling the firmware "I accept the rook move" — which the firmware had already counted as part of the castle. That phantom ack confused the firmware's internal state machine and it ignored the next legitimate human move. Fix: `_set_last_ai_move` now accepts the pre-move board and the `chess.Move` object; for castling moves it expands the echo set to include the rook's UCI (and 180°-rotated form) in addition to the king's. `_is_ai_echo` now checks set membership instead of comparing against a single UCI. Both castling directions covered. (En passant and promotion may need similar treatment if user reports analogous symptoms; deferred until confirmed.)
- **Spurious human-move detection during the post-GAME_START activation window.** The discovery callback's `_is_move` branch could fire on a `\x03M ...` notification that arrived AFTER the firmware transitioned to "Board Playing" but BEFORE the magnet had finished settling — typically a sensor recalibration event during the brief gap before firmware enters "Setting Up". The existing `_reset_modes` filter only catches "Managing Mismatch / Setting Up / Snapping Pieces", so this earlier window was unprotected. Result: the integration interpreted the spurious notification as a human move, pushed it to `self._board`, and the next AI turn responded to a fabricated position. Reproduced 2026-05-25 with `M 1 e8-g8` (phantom black kingside castle) right after a `start_local_game`. Added a `_activation_settle_until` timestamp set to `loop.time() + 45s` by every `_phantom_execute_position` snapshot write, checked in the move-detection branch ahead of the firmware_mode filter. CLEAN: Match notification clears the window early so legitimate post-activation human moves resume immediately once the firmware confirms the target position.
- **Cross-thread state-write races in BLE notify callbacks (audit §1.5).** `_handle_matrix_bytes`, `_handle_firmware_mode_bytes`, and `_on_battery` ran on a non-loop thread (notify callback context) but mutated `self._state` directly before scheduling a loop-thread update. The dict mutation raced against loop-thread readers (entity property gets, analysis-pipeline tasks, dashboard JSON serialization), and the `dict(self._state)` snapshot taken on the calling thread could interleave with a concurrent loop-thread write. Refactored so each handler parses on whatever thread invoked it, then marshals the state mutation into a `call_soon_threadsafe` closure that runs the new `_apply_*` helpers on the loop. All `self._state` reads and writes for these payloads now happen on the same thread.
- **`_local_ai_turn` swallow-and-continue when both apply and fallback failed (audit §1.3).** When `async_phantom_apply_ai_move` raised AND the fallback `self._board.push(move)` couldn't run (move not legal in current `_board` — typically a race with the discovery callback that already advanced past the AI's expected turn), the function still ran the analysis pipeline and derived `game_status` from a board state that didn't contain the AI move. Garbage classifications + wrong game-end detection followed. Added a `move_landed` flag tracking whether the move actually got onto `self._board`; if neither path landed it, the post-push bookkeeping is skipped and the function bails after pushing a state refresh so the UI doesn't go stale.
- **`_local_game_task` cancellation race (audit §1.4).** Five sites previously assigned `self._local_game_task = create_task(_local_ai_turn(), ...)` without cancelling+awaiting any in-flight task first. If a human move arrived mid-AI-think (discovery callback or dashboard move), the new task overwrote the reference but the old task kept running → two AI turns computed and dispatched concurrently. Funneled all replacement through a new `_replace_local_game_task(name=…)` helper that holds `self._local_game_task_lock`, cancels-and-awaits any prior task, then creates the new one. Only one AI turn can be in flight at a time regardless of how many paths request one.
- **Rewrote `async_takeback` for the firmware-0.3.0 wire format.** Pre-rewrite the integration wrote `b"1"` to `UUID_TAKEBACK` (`89185e7a-…`), a characteristic that does not exist on firmware 0.3.0 — the write silently failed and, for Lichess games, the local board state drifted from Lichess. Per Efraín's 2026-05-24 reply, opcode 5 on `UUID_GAME` takes `"count,FEN,side"` where `side` is who-plays-next (`"1"` = board side, `"0"` = BLE side) and FEN is the position AFTER the takeback. The new `async_takeback(count=1)` rolls back `count` plies on `self._board`, derives `side` from `self._our_color` and the post-pop `board.turn`, and writes opcode 5 with the correct payload. For Lichess games it requests takeback through the Board API first (`POST /api/board/game/{id}/takeback/yes`) and aborts the BLE write on Lichess refusal so the physical board stays in sync with the active online game. The `count` is exposed as an optional service field (default 1) so users can undo a full move pair with one call.
- **Bulk-demoted 48 of 81 `_LOGGER.warning` calls in `coordinator.py`** to `debug` (per-event protocol traces, DISCOVERY-block leftovers, success-path traces) or `info` (lifecycle events like sculpture-mode entry, local game start, recovery completion). The remaining 33 stay at WARNING and are genuinely user-actionable: BLE connection lost, subscribe failures, GATT staleness recovery, BLE_MOVE_DONE timeouts, takeback ack failures, Lichess auth and stream errors, board-error notifications, AI-move-not-legal desync indicators. Pre-fix, a typical Lichess game generated hundreds of WARNING log lines per minute, dominating the HA system log; post-fix the integration is silent during normal operation and noisy only when something actually requires attention. Triage rationale captured per-line in `outputs/log_demotion.py`.
- **Board kept showing up as "Discovered" even when already configured.** Pre-v0.3.0 installs that set the board up via the manual-MAC path stored the unique_id in whatever case the user typed (e.g. `c8:C9:A3:F2:7C:0A`). HA's Bluetooth integration always delivers uppercase addresses, so `_abort_if_unique_id_configured()` did a string-equality comparison against the mixed-case stored value, the abort never fired, and the board was perpetually re-offered as a new discovery in Settings → Devices & Services. Config-flow schema bumped to v3; the bluetooth and manual-entry paths both now canonicalize to uppercase colon-separated, and `async_migrate_entry` rewrites pre-existing entries (unique_id, `CONF_BLE_ADDRESS`, `CONF_DEVICE_NAME`, entry title) on first load. Manual entry now also validates the input as a 6-octet MAC and surfaces an inline error instead of accepting arbitrary strings.
- **Duplicate device + `_2`-suffixed entities after v1→v2 upgrade.** The first cut of the v2 migration normalized only the config entry; the entity registry (whose `unique_id` is `{ble_address}_{suffix}`) and device registry (whose identifier is `(DOMAIN, ble_address)`) carry their own ID spaces, and HA's get-or-create logic does case-sensitive string equality. Result on first post-migration boot: the integration's platform setup couldn't find the existing entities by their now-uppercase expected unique_ids, created a parallel set of entities (auto-suffixed `_2`), attached them to a new uppercase-identifier device, and the original entities went `unavailable`. v3 migration consolidates: rewrites lowercase entity `unique_id`s to uppercase (preserving entity_id, name, area, customizations), prefers the original (non-canonical) entry when both forms exist for the same suffix, deletes the duplicate canonical-form entries, updates the original device's identifier to uppercase, and removes the orphaned uppercase device. v4 follow-up renames any `_2`-suffix entity_ids back to their base form for users who already passed through the initial broken v3 (the first cut had the priority inverted and kept the auto-suffixed entries). Dashboards and automations referencing the original entity_ids are preserved end-to-end.
- **Static-path registration polluted the entry-id map.** A marker key written to `hass.data[DOMAIN]` was being counted by `_get_coordinator` as a second board, breaking every service call on single-board installs with a phantom "2 Phantom Chess Boards configured" error. Fixed by storing the marker under a separate `hass.data` slot.
- **GATT staleness recovery now covers discovery-phase reads (Task #28).** The Task #12 fix (2026-05-16) for "Characteristic not found" on writes is now also applied to the discovery-phase `UUID_GAME_CONFIG` read. Previously, a stale GATT handle surfaced during discovery silently logged a warning and the integration sat in a half-open state — `connected = on` but no notifications, requiring manual reload. Symptom observed 2026-05-19 after board power-cycle. Refactored the staleness detection into a shared `_handle_gatt_staleness` helper used by both `_ble_write` and the discovery read.

### Planned

- Coordinator god-object split into `ble_client.py`, `lichess_client.py`, `board_state.py`, `analysis_pipeline.py` modules. Reduces coordinator.py from ~4000 lines to <1000 and unblocks HA Bronze quality scale.
- Unit + integration tests under `tests/`.
- HA `diagnostics.py` for one-click debug-bundle download.
- Brand assets (logo/icon) PR to `home-assistant/brands`.

---

## [0.2.0] — 2026-05-16

First release-readiness pass. Reworks the integration to run on any user's Home Assistant install, not just the developer's. Also closes several reliability gaps surfaced during multi-game stress testing.

### Added

- **Voice play via Assist.** New `script.phantom_chess_play` exposed to the Conversation assistant. Triggered by phrases like "Okay Nabu, let's play chess"; agent elicits color, difficulty, and mode (Lichess / local) before starting.
- **Mode-agnostic learning view.** New `binary_sensor.phantom_*_learning_view_active` returns true for either Lichess OR local-Stockfish games. Dashboard rich view (eval bar, move history, classifications, post-game review) now renders for both modes. Local-game move events fire the analysis pipeline.
- **Sensor-mismatch notifications.** Persistent notifications walk the user through which pieces to adjust when the firmware's matrix and sensors disagree ("Missing: a white pawn on e4"). Notification updates only when the disagreement set changes; auto-dismisses on CLEAN: Match.
- **Continue-on-phone recovery.** New `phantom_chess.resume_from_phone` service. When the integration's apply-AI-move retry exhausts (BLE write failures, ATT 0x0e errors, GATT staleness), fires a persistent notification with instructions to continue the game on phone, then resync the board on tap. Game state is preserved; no resignation.
- **Lichess stream supervisor.** New `phantom_chess.reconcile_lichess_state` service + auto-trigger callback. When the stream task dies during a BLE storm and misses the terminal gameState event, the reconcile queries Lichess directly and syncs local state. Prevents `lichess_active` from getting stuck on.
- **GATT-cache-staleness recovery.** `_ble_write` now detects "Characteristic not found" errors that surface after firmware power-cycles or post-reload subscription reuse, and forces a BLE disconnect to trigger fresh service discovery via the existing reconnect loop.
- **AI-move BLE retry.** `apply_ai_move` retries once with 250ms backoff on transient transport errors (TypeError, BleakError ATT 0x0e, mid-write disconnect). Snapshot mechanism is idempotent — retry is safe.
- **Transient firmware-state dashboard cards.** Six new dashboard conditionals show state-specific info banners for Snapping Pieces, Snap to Center, Calibrating, Setting Up, Ending Game, Initializing — replacing the bare-board fallback.
- **`phantom_chess_announce` event.** Integration now fires this event for all announcements (game start, AI moves, classifications, check/mate). Users wire their TTS stack via a simple automation. Integration is no longer coupled to a specific TTS service.
- **Options flow.** Settings → Devices & Services → Phantom Chess → ⋮ → Configure exposes `tts_service`, `tts_media_player_entity_id`, `debug_dump`. Users can rotate the Lichess token without delete-and-recreate.
- **Reauth flow.** Lichess HTTP 401/403 from the stream now triggers a token-entry dialog automatically (`async_step_reauth_confirm` in config_flow). Preserves entity history.
- **ARM Stockfish support.** `STOCKFISH_ASSET_MAP` now includes aarch64/arm64 entries for both musl (Alpine apk) and glibc (official ARM release). HA on Raspberry Pi 4/5, HA Yellow, HA Green — all aarch64 — now get local AI.
- **Public method `LichessAnalysisClient.best_move_for_ai_level()`.** Replaces the duplicate `_find_stockfish` / `_stockfish_best_move` pair previously in coordinator.py. Routes local-game AI moves through the shared `StockfishFallback` engine.

### Changed

- **Multi-board service routing.** `_get_coordinator(call)` now properly handles multi-board installs: requires `entry_id` in service data when more than one board is configured. Error message lists known entry IDs.
- **Manifest cleanup.** `codeowners` populated, `issue_tracker` set, `integration_type` set to "device", `documentation` now points at the integration's intended GitHub repo (not the firmware repo). Bluetooth matcher tightened from greedy `local_name: "Phantom*"` to specific `service_uuid: "fd31a840-22e7-11eb-adc1-0242ac120002"`. `chess` dependency loosened from `==1.10.0` to `>=1.10.0,<2.0`.
- **Debug-log paths portable.** All `/config/phantom_chess_*.txt` writes now use `hass.config.path("phantom_chess", "debug", ...)`. Writes are gated behind the new `debug_dump` option (default off).
- **Stockfish discovery unified.** Removed the duplicate `_find_stockfish` (hardcoded `/config` paths, silent auto-chmod 0755) and `_stockfish_best_move` (raw subprocess). Both go through `StockfishFallback` now.
- **Strings cleanup.** Removed dead `firmware_too_old` issue translation; added `reauth_confirm` step and `options.step.init` section.

### Fixed

- **NoneType-not-awaitable bug in AI-move application.** Added `_LOGGER.exception()` for full traceback diagnostics, plus one-shot retry that mitigates the symptom. Root cause still unidentified — next reproduction will surface the exact await site.
- **Stale `firmware_too_old` repair issue.** Cleanup removes the orphan issue on the next BLE connect. The originating check was a false-positive on all firmware-0.3.0 boards.
- **Dashboard stale entity references.** Replaced `button.phantom_*_resign` / `_takeback` with `script.phantom_resign` / `script.phantom_takeback` in `lovelace.chess_board` — clears the Spook unknown-entity-references repair.
- **Lichess game stuck `lichess_active` after game ends elsewhere.** Reconcile mechanism (above) closes this.

### Removed

- **Hardcoded TTS entity IDs** (`tts.google_ai_tts`, `media_player.home_assistant_voice_0a8b1d_media_player`) from coordinator. Replaced with event-based announcements + optional configured TTS.
- **Hardcoded `/config/phantom_chess_*.txt` paths** in coordinator. Replaced with portable `hass.config.path()` calls.
- **`_find_stockfish` self-chmod-0755 behavior.** Security smell; removed entirely.
- **Direct `subprocess` and `shutil` imports** in coordinator — no longer used after Stockfish unification.
- **Seven deprecated / redundant entities** that had been disabled-by-default for so long they were just dead code. Existing orphans in users' entity registries can be cleaned via the device page in HA's UI:
  - `sensor.phantom_*_fen` — deprecated; never updated on 0.3.0 firmware. Use `sensor.phantom_*_live_position` (Diagnostic).
  - `sensor.phantom_*_last_move` — duplicate of `sensor.phantom_*_firmware_last_move` (which is firmware-authoritative).
  - `sensor.phantom_*_game_status` — internal state-machine field; services use it directly.
  - `sensor.phantom_*_turn` — derivable from the board image and the FEN.
  - `sensor.phantom_*_best_move_uci` — duplicate of `sensor.phantom_*_best_move_san` (the human-readable form).
  - `binary_sensor.phantom_*_game_active` — superseded by `lichess_active` and `learning_view_active`.
  - `binary_sensor.phantom_*_position_consistent` — the bitmap interpretation turned out to be unreliable.

### Recategorized

- **`Lichess Game ID` and `Eval Source` sensors** moved from disabled-by-default to `EntityCategory.DIAGNOSTIC`. They're legitimately useful for debugging — Lichess Game ID for cross-referencing with the user's Lichess account, Eval Source for diagnosing "why isn't the eval bar updating". Now visible by default in the Diagnostic sub-section of the device page rather than fully hidden.
- **`Live Position` sensor** moved to `EntityCategory.DIAGNOSTIC` because its 43-character FEN state was overflowing HA's More Info column layout. Still useful for advanced dashboards; no longer cluttering the main entity list.

### Dead-code prune (independent audit follow-up)

After the entity removals an independent audit agent surfaced 12 categories of orphaned code. All removed in the same release window:

- **Duplicate `_phantom_send_ai_move` method definition** — Python kept the second (line 1606); the first (line 1296) was unreachable. Real correctness risk if someone "fixed" the wrong copy.
- **`_send_move_to_board` method** (~40 lines) — only referenced in two historical comments; the actual call sites had all migrated to `_phantom_execute_position` / `async_phantom_apply_ai_move` long ago.
- **`_uci_to_phantom` helper** — only consumer was `_send_move_to_board`; orphaned by its deletion.
- **7 unused entity constants** in const.py: `ENTITY_FEN`, `ENTITY_LAST_MOVE`, `ENTITY_TURN`, `ENTITY_GAME_STATUS`, `ENTITY_BEST_MOVE_UCI`, `ENTITY_GAME_ACTIVE`, `ENTITY_POSITION_CONSISTENT` — left behind when the corresponding entities were removed.
- **18 orphaned state-dict writes** across coordinator.py (12 `_state["fen"]`, 5 `_state["best_move_uci"]`, 5 `_state["turn"]` + 1 in button.py) — sensors that read them were removed earlier in this same release.
- **3 stale seed entries** in `_blank_state` (fen, best_move_uci, turn).
- **5 stale strings.json entity entries** (fen, last_move, game_status, turn, game_active).
- **1 stale reference in diagnostics.py** to `turn` in `keys_of_interest` tuple.
- **3 stale comments + docstrings** referencing removed methods/state fields.

coordinator.py shrunk from ~190 KB to ~175 KB (8% smaller) after this prune. No behavior change.

### Internal

- coordinator.py: now 190KB / ~4000 lines. Marked for split in Task #21.
- New file `examples/dashboard.yaml` — published reference dashboard.
- New files `LICENSE` (MIT), `README.md` (full user-facing docs), `hacs.json`.

---

## [0.1.0] — 2026-05-08

Initial integration scaffolding (developer pre-release; never publicly distributed).

- BLE config flow with auto-discovery.
- Lichess Board API integration (challenge AI, stream gameState, send moves, resign, takeback).
- Local Stockfish fallback for cloud-eval misses.
- In-game learning dashboard for Lichess games (eval bar, move classification, threat warnings, opening name, post-game review).
- Sculpture mode for displaying historical games.
- Entities: connection state, firmware mode, last move, piece count, eval cp/mate/depth/source, best move UCI/SAN, move history, last-game accuracy/result/review, threat SAN, opening name/ECO.

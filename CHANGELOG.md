# Changelog

All notable changes to the Phantom Chess Board Home Assistant integration are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

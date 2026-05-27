# Phantom Chess Board — Home Assistant Integration

Turn your [Phantom Chess Board](https://www.thephantomchess.com/) into a Home Assistant device. Play against [Lichess](https://lichess.org/)'s AI or a locally-bundled Stockfish, watch eval bar and move classifications in real time, get post-game review with your top mistakes, and trigger games by voice via Home Assistant's Assist.

This is an unofficial community integration — not produced by Phantom Technology.

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![Status: beta](https://img.shields.io/badge/status-beta-yellow)

---

## Features

- **Two play modes:**
  - **Lichess** — challenge Lichess's Stockfish AI, full Board API integration, games appear in your Lichess profile.
  - **Local Stockfish** — bundled offline engine, no internet required after first run, no Lichess account needed.
- **In-game learning dashboard:**
  - Vertical eval bar (lichess.org style)
  - Per-move classification: best / good / inaccuracy / mistake / blunder, with centipawn loss
  - Opening name (via Lichess masters explorer) until you leave book
  - Threat warnings — surfaced when the AI has a capture or mate-in-N ready
  - Tactical motif detection (forks; pin/skewer in progress)
  - Post-game review with top mistakes per side and Lichess-style accuracy scores
- **Sculpture mode** — play famous historical games on the physical board for display / education.
- **Voice control via Assist** — "Okay Nabu, let's play chess" triggers the configured game start.
- **Per-side TTS announcements** — fired as Home Assistant events; wire to any TTS service.
- **Hardware-error recovery:**
  - Transparent retry on transient BLE errors
  - Auto-recovery from GATT cache staleness after firmware power-cycles
  - "Continue on phone" notification flow that preserves your Lichess game
  - Sensor-mismatch notifications that walk you through which piece to adjust

---

## Hardware Requirements

- **Phantom Chess Board** running firmware **v0.3.0 or later** (the integration uses the firmware-0.3.0 BLE protocol exclusively — earlier firmware versions are not supported).
- **Home Assistant** 2024.4.0 or later.
- A Bluetooth adapter or BLE proxy reachable by HA (the board uses BLE, not classic Bluetooth). HA Yellow, HA Green, and most Raspberry Pi setups work out of the box. For HA running in a container or VM without local Bluetooth, an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) within radio range of the board solves this.
- **Optional**: a [Lichess account](https://lichess.org/) with a Board API token (free) — required only for online play.

---

## Installation

### Via HACS (recommended)

1. Open HACS → Integrations → ⋮ → Custom repositories.
2. Add `https://github.com/luketadams/phantom-chess-ha` as an Integration type repository.
3. Search for **Phantom Chess Board** in HACS, click **Download**.
4. Restart Home Assistant.

### Manual

1. Copy the entire `custom_components/phantom_chess/` directory into your HA `config/` folder so the path is `config/custom_components/phantom_chess/`.
2. Restart Home Assistant.

---

## Setup

1. **Power on the board.** It will advertise itself over BLE as `Phantom XXXX` (where XXXX is a random 4-digit number).
2. In Home Assistant, go to **Settings → Devices & Services → Add Integration**.
3. Search for **Phantom Chess Board**. If your board is already advertising, HA's Bluetooth integration will have discovered it — you'll see it listed.
4. Confirm the board to set up.
5. **(Optional)** Provide a Lichess Board API token. Create one at <https://lichess.org/account/oauth/token> with the **Play games with the Board API** (`board:play`) scope enabled.
   - You can skip this step if you only want to play local Stockfish games.
   - The token can be added or rotated later via the integration's reauth flow.

That's it. The integration creates a single device with all entities under it.

### Setup parameters

The setup flow asks for the following fields. Both are optional in the bluetooth-discovery path (the board's MAC is pre-filled from the discovery info); the manual path requires the BLE address.

| Field | Required | Description |
|---|---|---|
| **Board Bluetooth Address** | yes (manual flow) | Six hex pairs separated by colons or dashes, e.g. `C8:C9:A3:F2:7C:0A`. You can find it on the board's serial sticker, the official Phantom app's settings screen, or any nearby BLE scanner. Case-insensitive — the integration canonicalises to upper-case. |
| **Lichess Board API Token** | no | A personal-access token created at <https://lichess.org/account/oauth/token> with the **Play games with the Board API** (`board:play`) scope enabled. Used only for streaming your active board games and posting moves on your behalf — the integration never reads chat / preferences / friends data. Leave blank to use only local Stockfish for AI games. |

---

## First Game

### Via the dashboard

The integration provides entities you can wire into your own Lovelace dashboard. The recommended starting point: a single panel-mode view with these key entities:

- `image.phantom_*_board` — live SVG of the position
- `binary_sensor.phantom_*_learning_view_active` — true while a game is in progress
- `select.phantom_*_ai_level` — 1 to 8
- `select.phantom_*_player_color` — white / black / random
- `sensor.phantom_*_eval_cp` — current centipawn evaluation
- `sensor.phantom_*_move_history_moves` — JSON list of moves with classifications

A starter dashboard YAML is shipped as `examples/dashboard.yaml` in the repo.

### Via voice

If you exposed the included `script.phantom_chess_play` script to Assist (it ships pre-exposed), say:

> "Okay Nabu, let's play chess."

The Conversation assistant will ask you for color and difficulty if you haven't specified them.

---

<a id="chess-dashboard-frontend-dependencies"></a>
## The Chess Dashboard

The Phantom Chess Board is a single-purpose device with one sensible UI: a mode-picker that walks you through Lichess / Stockfish / Sculpture / 2-Player, contextual cards for each, live-game view with eval bar and move classifications, post-game review with top mistakes, and an embedded drag-drop interactive board. **The dashboard isn't a nice-to-have add-on — it's the user-facing surface of the integration.** A bare entity-list view doesn't show what this thing can do.

This beta ships the full dashboard as YAML in `examples/` that you copy/paste into HA. A future release will auto-create everything during integration setup — see the "Roadmap" note at the end of this section.

**Setup is ~5 minutes of copy/paste, in four steps:**

1. **Install two HACS frontend plugins.** In HACS → Frontend:
   - **Mushroom** (provides `custom:mushroom-template-card`)
   - **layout-card** (provides `custom:layout-card`)
2. **Helpers.** Paste `examples/helpers.yaml` into your `configuration.yaml` (input_selects for the mode + sculpture-game pickers, input_boolean for training-wheels, input_numbers for Lichess clock controls, template binary sensor for the 60s-idle gate that prevents UI flicker during sculpture playback). Restart Home Assistant.
3. **Scripts.** Paste `examples/scripts.yaml` into your `scripts.yaml` (7 control scripts the dashboard's tiles invoke). Reload Scripts from Developer Tools → YAML → Scripts, or restart again.
4. **The dashboard itself.** Settings → Dashboards → **+ Add Dashboard** → "New dashboard from scratch" → name it "Chess" → open it → Edit Dashboard → **⋮ → Raw configuration editor** → paste `examples/dashboard-rich.yaml`. Save.

**Find/replace required.** All three example files reference `YOUR_BOARD_MAC` as a placeholder for your board's MAC slug. After pasting each, find/replace `YOUR_BOARD_MAC` with your actual MAC slug — find it by looking at any phantom_chess entity_id (e.g. `sensor.phantom_c8_c9_a3_f2_7c_0a_battery` → your slug is `c8_c9_a3_f2_7c_0a`).

**Roadmap.** v0.4 will replace this section with "install Mushroom + layout-card, done." The integration will auto-provision the helpers (or replace them with integration-owned `select`/`switch` entities), expose the scripts as native services, and create the dashboard via HA's frontend API during initial setup. The current copy/paste flow is the stepping-stone, not the destination.

---

## Services

All services accept an optional `entry_id` field. **Required if you have more than one Phantom Chess Board configured**; optional if you have exactly one.

| Service | Description |
|---|---|
| `phantom_chess.start_game` | Start a Lichess Board API game. Fields: `ai_level` (1-8), `color` (white/black/random), `clock_limit_seconds`, `clock_increment_seconds`. |
| `phantom_chess.start_local_game` | Start an offline local-Stockfish game. Fields: `color`. |
| `phantom_chess.stop_local_game` | End the current local game. |
| `phantom_chess.resign` | Resign the current Lichess game. **Affects rated games** — only call when the user really wants to end the game. |
| `phantom_chess.takeback` | Request a takeback (Lichess decides whether to honor it). |
| `phantom_chess.reset_position` | Drive the magnet to return all pieces to the starting position. |
| `phantom_chess.reconcile_lichess_state` | Query Lichess for game status and sync local state. Use when `lichess_active` gets stuck on after a hardware error. |
| `phantom_chess.resume_from_phone` | After an "AI move not delivered" notification, call this to push the current position to the board so you can resume physical play. |
| `phantom_chess.start_sculpture` | Enter sculpture mode for displaying historical games. |
| `phantom_chess.play_sound` | Play firmware-native check or checkmate sound. |
| `phantom_chess.request_hint` | Refresh the engine's recommendation for the current position. |

See `services.yaml` for full field definitions and selector types.

---

## Events

The integration fires these HA events you can listen to in automations:

| Event | Data |
|---|---|
| `phantom_chess_announce` | `{message: str, board_address: str}` — announcements for game start, AI moves, check/mate, and move classifications. Wire to your TTS stack of choice. |

**Example automation** — forward announcements to a TTS service:

```yaml
trigger:
  - platform: event
    event_type: phantom_chess_announce
action:
  - service: tts.speak
    data:
      entity_id: tts.your_tts_engine
      media_player_entity_id: media_player.your_speaker
      message: "{{ trigger.event.data.message }}"
      cache: true
```

---

## Configuration Options

After setup, click the integration's **Configure** button (Settings → Devices & Services → Phantom Chess Board → ⋮ → Configure) to access these options. Leave any field blank to use its default.

| Option | Default | Description |
|---|---|---|
| `tts_service` | _empty_ | Optional. If set, the integration calls this TTS service for announcements (move classifications, check / checkmate, game outcomes) in addition to firing the `phantom_chess_announce` event. Format: `<domain>.<service>`, e.g. `tts.google_ai_tts`, `tts.cloud_say`. |
| `tts_media_player_entity_id` | _empty_ | **Required** when `tts_service` is set — the media player to play TTS audio through. Format: an entity ID, e.g. `media_player.living_room_speaker`. |
| `debug_dump` | off | Write developer-debug artifacts (GATT layout, BLE matrix log, characteristic values) to `<config>/phantom_chess/debug/` on every restart. **Off by default.** Only enable when troubleshooting an issue with the maintainers — the directory can grow quickly during long sessions. |
| `auto_provision_dashboard` | on | Auto-install the rich **Chess** dashboard at `/phantom-chess` on every config-entry setup. Turn off if you want to author your own dashboard from scratch; the auto-provisioned one will be removed on the next reload. The shared `/phantom-chess` is recreated on next reload if you flip this back on. |

### Rotating the Lichess token

The Lichess Board API token is stored in the config entry's `data` (not in the options flow above), encrypted by HA's standard credential storage. If the token expires or you revoke it on Lichess:

1. Home Assistant will auto-detect the rejection during the next API call and show a **Re-authentication required** notification.
2. Click the notification (or go to the integration page) and choose **Reconfigure**.
3. Generate a fresh token at <https://lichess.org/account/oauth/token> with the **Board API** (`board:play`) scope enabled, paste it in.
4. The integration reloads and resumes Lichess play.

---

## Use Cases

Real workflows the integration is designed around. These aren't just feature highlights — they're the reasons the integration exists, in priority order.

### Solo training without the phone app

Play against Lichess or local Stockfish using nothing but the physical board and a dashboard on your wall-mounted iPad. Eval bar, opening name, threat warnings, and move classifications all surface as you play; the post-game review pane shows your three biggest mistakes with the engine's preferred move alongside. The official Phantom app is never started.

### Voice-driven game start

Walk past the board, say "Okay Nabu, let's play chess." Assist asks for color and difficulty if you haven't pre-set them and starts the game. Useful for keeping your hands free while setting up pieces; also the only way to start a game without a screen.

### Watch the AI play itself

The `phantom_chess.start_ai_vs_ai_game` service runs a Stockfish-vs-Stockfish game on the physical board, with the magnet driving moves for both colors. Helpful as a demo (the eval bar swings in real time) and as a stress test (catches BLE timing issues that a human-paced game would mask).

### Correspondence-style play with notifications

Set up a Lichess correspondence game. The integration's `phantom_chess_announce` event fires when your opponent moves, even when you're not in the room — wire it to your TTS stack to get spoken alerts ("Your move — opponent played Nf6").

### Sculpture mode for guests

Trigger sculpture mode via voice or automation when guests arrive. The board plays out a famous historical game (Kasparov vs Deep Blue, the Immortal Game, the Game of the Century, etc.) as kinetic display. The integration ships an 18-game catalog; the dashboard's Sculpture Library tile picks which to play.

### Post-game accuracy tracking

Every Lichess game's accuracy scores get retained in the integration's `last_game_accuracy_white` / `last_game_accuracy_black` sensors. Pair with the HA history database to track your accuracy over time — a `statistics_graph` card showing a 30-day average gives you a calmer signal than the per-game number Lichess shows.

---

## Supported Devices

| Hardware | Status | Notes |
|---|---|---|
| Phantom Chess Board (firmware **v0.3.0+**) | ✅ Supported | Primary target. The integration uses firmware-0.3.0 BLE protocol exclusively. |
| Phantom Chess Board (firmware v0.2.x or earlier) | ❌ Not supported | Pre-0.3.0 firmware used a different BLE characteristic layout. Update via the official Phantom app. |
| Phantom Chess Board (firmware **v0.3.2+**) | ✅ Supported (forward-compatible) | The integration tolerates the 0.3.2 protocol additions (slide-detection flag, slide-delay characteristic) but doesn't yet drive them as entities. Pure additions, never regressions. |

Compatible Bluetooth surfaces:

| Bluetooth setup | Works? |
|---|---|
| Home Assistant Yellow built-in BT | ✅ |
| Home Assistant Green / Connect ZBT-1 | ✅ |
| Raspberry Pi 4 / 5 onboard BT | ✅ |
| ESPHome Bluetooth proxy (within radio range) | ✅ — useful for HA-in-VM and HA-in-container deployments |
| USB BT 4.0+ dongle | ✅ (any HA-supported dongle) |
| Classic Bluetooth-only adapter | ❌ — the Phantom Chess Board is BLE-only |

---

## Supported Functions

The integration creates one device per board with the following entities (canonical names; the `<id>` placeholder is replaced with your board's slug at registration time).

### Sensors (read-only state)

| Entity | Description |
|---|---|
| `sensor.<id>_battery` | Battery percentage (0–100). |
| `sensor.<id>_firmware_mode` | Firmware state machine label: HOME / Setting Up / Snap to Center / Running / Paused / Sculpture Playback / etc. |
| `sensor.<id>_firmware_last_move` | Last move event the firmware emitted on the firmware-state channel (e.g. `K e1-e2`). Distinct from `last_move` which is normalised UCI. |
| `sensor.<id>_live_position` | Current FEN-format position; attributes include `our_color`, `side_to_move`, `last_move`, `lichess_active`, `local_game_active`, `game_status`. |
| `sensor.<id>_piece_count` | Number of pieces currently on the board (computed from the sensor matrix). |
| `sensor.<id>_matrix_status` | Sensor-matrix consistency state: `Clean` or `Error`. |
| `sensor.<id>_eval_cp` / `sensor.<id>_eval_mate` | Current position centipawn / mate evaluation. |
| `sensor.<id>_eval_source` | Where the eval came from: `lichess-cloud` / `stockfish-local` / `stub`. |
| `sensor.<id>_eval_depth` | Engine search depth for the current eval. |
| `sensor.<id>_opening_name` | Lichess masters opening name (e.g. "Sicilian Defense: Najdorf, English Attack"). Goes to `unknown` after you leave book. |
| `sensor.<id>_best_move_san` | Engine's preferred move in SAN notation. |
| `sensor.<id>_last_move_classification` | Last move's classification: best / good / inaccuracy / mistake / blunder. |
| `sensor.<id>_last_move_cpl` | Last move's centipawn loss. |
| `sensor.<id>_threat_san` | If side-to-move has a capture or mate ready, the SAN form of that move. |
| `sensor.<id>_move_history` | JSON list of moves with classifications + cpls; attributes include `top_mistakes` once review is ready. |
| `sensor.<id>_last_game_result` / `_accuracy_white` / `_accuracy_black` | Post-game stats from the most-recent completed game. |
| `sensor.<id>_lichess_white_clock` / `_black_clock` | Lichess clock readings in seconds. |
| `sensor.<id>_lichess_white_name` / `_black_name` | Lichess player names. |

### Binary sensors

| Entity | Description |
|---|---|
| `binary_sensor.<id>_connected` | True while the integration has an active BLE session with the board. **Always available even when other entities go unavailable** — this is the connectivity indicator. |
| `binary_sensor.<id>_lichess_active` | True while a Lichess Board API game is streaming. |
| `binary_sensor.<id>_lichess_review_ready` | True when the post-game review pane has data to show. |
| `binary_sensor.<id>_learning_view_active` | Composite "the learning view should render" gate used by the dashboard. |
| `binary_sensor.<id>_board_idle` | True after 60 seconds of firmware inactivity — drives the dashboard's mode-picker-vs-live-game gate. |

### Image, switches, numbers, selects, buttons

| Entity | Description |
|---|---|
| `image.<id>_board` | SVG render of the current position with last-move highlight; in training-wheels mode also overlays the classification glyph on the destination square. |
| `switch.<id>_paused` | Pause / resume the mechanism (drives UUID_PAUSE). |
| `switch.<id>_training_wheels` | Toggle the dashboard's classification-glyph overlay. Pure-local, no BLE write. |
| `number.<id>_mechanism_speed` | 1–5 firmware speed scale. |
| `number.<id>_sound_level` | 0–32 firmware sound-volume scale. |
| `number.<id>_lichess_clock_minutes` / `_increment` | Pre-game-start clock settings (minutes + seconds-per-move increment). Pure-local. |
| `select.<id>_ai_level` | Lichess / Stockfish AI level 1–8. |
| `select.<id>_player_color` | white / black / random. |
| `select.<id>_setup_mode` | Dashboard mode picker (Choose a mode / Lichess / Stockfish / Sculpture / 2-Player). Pure-local. |
| `select.<id>_sculpture_game` | Sculpture catalog picker (18 historical games). |
| `button.<id>_start_game` / `_movement_verify` | Diagnostic-grade buttons. Disabled by default in the entity registry — use the `phantom_chess.*` services for normal gameplay. |

See the **Services** section above for the action surface (`phantom_chess.start_game`, `phantom_chess.takeback`, `phantom_chess.execute_move`, etc.).

---

## Data Update Flow

How state gets from the board into HA, and the moments when each piece of state changes.

```
   Physical hand moves a piece on the board
   ↓
   Hall-effect sensor matrix updates
   ↓
   Firmware emits BLE notification on UUID_SEND_MATRIX (1b034927)
     "CLEAN: Match.,<piece-grid>,<sensor-bitmap>"
   ↓
   Coordinator._handle_matrix_bytes parses + marshals to event loop
   ↓
   Coordinator._apply_matrix_state mutates self._state:
     piece_grid, sensor_bitmap, live_fen, piece_count,
     matrix_status, position_consistent, matrix_mismatches
   ↓
   coordinator.async_set_updated_data fans the new state to all entities
   ↓
   Entities re-evaluate their state/property; HA pushes WebSocket updates
   ↓
   Dashboard re-renders; image entity regenerates SVG only if a render
   input changed (FEN / last_move / orientation / classification glyph)
```

For Lichess games, a parallel stream runs:

```
   Lichess Board API /api/board/game/stream/{gameId}
   ↓
   Coordinator._lichess_task consumes events:
     gameFull (initial state) → gameState (per-move updates)
   ↓
   For opponent moves: push to self._board, drive magnet via
     phantom_chess.async_phantom_apply_ai_move
   ↓
   For game-end events: stop stream, populate last_game_* sensors
```

There's **no polling interval** — the integration is `iot_class: local_push`. The coordinator's `update_interval` is set to 30 seconds purely as a safety-net heartbeat; meaningful state changes always arrive via BLE notify or the Lichess stream, not the poll.

---

## Automation Examples

Drop-in starter automations using the integration's events + services.

### Announce moves over TTS

```yaml
alias: Phantom Chess — TTS announcements
trigger:
  - platform: event
    event_type: phantom_chess_announce
action:
  - service: tts.cloud_say
    data:
      entity_id: media_player.living_room_speaker
      message: "{{ trigger.event.data.message }}"
mode: parallel
```

### Resume after "AI move not delivered" notification

When the magnet fails to drive a move (BLE storm, piece-too-close-to-magnet, etc.), the integration fires a persistent notification and the game continues on Lichess's web/mobile UI. Once you're back at the board, this automation resumes physical play:

```yaml
alias: Phantom Chess — resume after AI failure
trigger:
  - platform: state
    entity_id: input_boolean.phantom_chess_back_at_board
    to: 'on'
action:
  - service: phantom_chess.resume_from_phone
  - service: input_boolean.turn_off
    target:
      entity_id: input_boolean.phantom_chess_back_at_board
```

### Sculpture-on-arrival routine

Trigger the sculpture mode when guests are detected (via presence sensors, calendar event, etc.):

```yaml
alias: Phantom Chess — sculpture for guests
trigger:
  - platform: state
    entity_id: binary_sensor.guests_present
    to: 'on'
action:
  - service: select.select_option
    target:
      entity_id: select.phantom_c8_c9_a3_f2_7c_0a_sculpture_game
    data:
      option: "Kasparov vs Deep Blue, 1996"
  - service: phantom_chess.play_selected_sculpture
```

### Statistics-graph card for accuracy over time

```yaml
type: statistics-graph
title: Chess accuracy (30-day average)
entities:
  - sensor.phantom_c8_c9_a3_f2_7c_0a_last_game_accuracy_white
  - sensor.phantom_c8_c9_a3_f2_7c_0a_last_game_accuracy_black
stat_types:
  - mean
days_to_show: 30
chart_type: line
```

---

## Known Limitations

Not bugs — design or platform constraints to set the right expectations.

- **One physical board per HA install at a time** is the primary tested configuration. Multi-board is supported architecturally (`entry_id` parameter on every service, per-board coordinators) but Luke only owns one board so the multi-board paths haven't been stress-tested. File an issue if you hit problems.
- **The `phantom_chess.move_piece` service bypasses chess.Board validation.** It drives the magnet directly without checking move legality. Useful for setting up arbitrary positions and recovering from state-mismatch — but you can put the physical board into states python-chess can't represent. Use `phantom_chess.execute_move` for normal gameplay.
- **Sculpture playback isn't fully driven by the integration yet** (alpha10). The dashboard's Sculpture Library tile enters firmware sculpture mode and fires a notification naming the selected game, but per-game move-sequence playback currently relies on the v0.3 setup-pack scripts. Auto-driving the playback from the integration is tracked as a v0.4 follow-up.
- **AI-vs-AI long games occasionally hang around move ~45** (Task #40, observed during an overnight stress test 2026-05-26). Root cause not yet diagnosed — reproduces only with sustained BLE traffic on the magnet. The integration retries on transient errors but a deeper hang requires a `phantom_chess.stop_local_game` + restart.
- **The Lichess cloud-eval endpoint is unauthenticated and rate-limited.** During long games the eval can briefly become stale (the integration falls back to local Stockfish when available, otherwise to a `stub` source that just preserves the last reading).
- **Stockfish download is one-shot, ~3 MB.** First evaluate() call after a fresh install pulls the appropriate binary from official Stockfish releases (glibc) or Alpine apk (musl). Subsequent restarts reuse the cached binary at `/config/phantom_chess/bin/`. If the download fails (e.g. firewall blocking GitHub), the integration falls back to Lichess cloud-eval only.
- **The board's BLE GATT cache can go stale** after firmware-side power cycles. The integration detects this (writes fail with a specific bleak error pattern) and forces a fresh-discovery reconnect; you may briefly see entities go unavailable during the recovery window.
- **The Lichess Board API doesn't support all game types.** Most notably, it can't be used as an anonymous (no-account) play target; you need a Lichess account with the Board API scope enabled. Local Stockfish is the offline fallback.

---

## Troubleshooting

**Board doesn't show up during integration setup.** Confirm the board is powered on and within BLE range of your HA host. Use HA's Bluetooth integration page to verify it sees the board's advertisements.

**TTS announcements aren't playing.** The integration fires a `phantom_chess_announce` event but doesn't call TTS by default. Either: (a) add a forwarding automation (see Events section above), or (b) configure `tts_service` + `tts_media_player_entity_id` in integration options.

**Lichess game appears live on phone but dashboard shows no activity.** The Lichess stream task may have died during a BLE storm. Call the `phantom_chess.reconcile_lichess_state` service to re-query Lichess and sync local state.

**AI moves aren't being driven to the physical board.** The integration retries automatically on transient errors. If a move persistently fails, you'll see a "Phantom Chess: AI move not delivered" notification. Continue the game on Lichess's web or phone app; when you're ready to resume physical play, call `phantom_chess.resume_from_phone`.

**Board firmware shows "Managing Mismatch" repeatedly.** Sensor matrix mismatch — see the dashboard's transient-state info card for which pieces to adjust. The integration's `phantom_chess_sensor_mismatch` persistent notification will tell you exactly which squares need attention.

**Local Stockfish not working on ARM (Raspberry Pi).** Make sure you're on integration v0.2.0 or later — earlier versions were x86_64-only.

**ATT error 0x0e — "Operation failed with ATT error: 0x0e (Unlikely Error)".** Transient BLE write failure. The integration's BLE retry handles single instances. If it recurs across multiple games, try moving the BLE proxy closer to the board or switch to an ESPHome BT proxy.

---

## Removal

To remove the integration:

1. Go to **Settings → Devices & Services → Phantom Chess Board**.
2. Click the ⋮ (three-dot) menu next to the integration and choose **Delete**.
3. Confirm. HA will:
   - Disconnect from the board over Bluetooth.
   - Remove all `phantom_chess.*` entities (sensors, switches, buttons, etc.).
   - Remove the auto-provisioned `/phantom-chess` dashboard from the sidebar (if this was the last configured board).
   - Drop the device from the device registry.
4. If you installed via HACS and want to remove the integration's code as well: go to **HACS → Integrations → Phantom Chess Board → ⋮ → Remove**. Restart Home Assistant after the HACS removal.
5. **Files that are NOT removed automatically** (these are intentional — they may contain Stockfish binaries and debug artifacts you might want to keep or move):
   - `/config/phantom_chess/bin/` — the downloaded Stockfish binary cache (~3 MB).
   - `/config/phantom_chess/debug/` — debug-dump artifacts, only present if `debug_dump` was enabled in integration options.

   Delete the `/config/phantom_chess/` directory manually if you want a fully clean uninstall.
6. **Lichess token** — the integration only stores the token in HA's config-entry storage, so deleting the integration removes it. No revocation is performed against Lichess. If you want to invalidate the token, visit https://lichess.org/account/oauth/token and revoke it manually.

---

## Privacy & Data Flow

- **Lichess token** is stored in your HA config entry (HA's standard encrypted credential storage). It is sent only to `lichess.org` over HTTPS, never to third parties.
- **Anonymized cloud-eval requests** are made to `lichess.org/api/cloud-eval` (no auth) for position evaluation. These contain only the board FEN — no game-id, no account info.
- **Opening name lookups** hit `explorer.lichess.ovh/masters` similarly anonymously.
- **Stockfish binary** is downloaded once from either `dl-cdn.alpinelinux.org` (Alpine apk) or `github.com/official-stockfish/Stockfish/releases` (official release) depending on your libc. After the initial download, all engine activity is local.
- **No telemetry**, no analytics, no third-party endpoints.

---

## Acknowledgments

- [Phantom Technology](https://www.thephantomchess.com/) for designing and building the board, and Efraín on the Phantom team for the protocol documentation that made this integration possible.
- [Lichess](https://lichess.org/) for the Board API, Cloud Eval, and Masters Explorer — all free for community use.
- [@xouxou](https://github.com/xouxou) for the initial protocol reverse-engineering work that informed the early decode of the BLE messages.
- [python-chess](https://github.com/niklasf/python-chess) by Niklas Fiekas for the chess engine, FEN handling, and SVG rendering.
- [Official Stockfish team](https://stockfishchess.org/) for the engine.

---

## Contributing

Issues and pull requests welcome. Please:
- Open issues with the firmware version, HA version, and integration version reported.
- Attach a `home-assistant.log` excerpt covering the timeframe of the issue.
- For BLE issues specifically: enable `debug_dump` in options, reproduce, and attach `<config>/phantom_chess/debug/matrix_log.txt`.

For protocol or firmware issues — those are out of scope for this integration; they belong on Phantom's official channels.

---

## License

[MIT](LICENSE) — Copyright © 2026 Luke Adams.

---

## Disclaimer

This integration is not affiliated with, endorsed by, or sponsored by Phantom Technology. "Phantom Chess Board" and any related trademarks are property of their respective owners.

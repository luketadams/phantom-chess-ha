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

# Phantom Chess for Home Assistant

Play chess on a Phantom robotic board from Home Assistant, with a bundled dashboard and voice announcements. This is an unofficial community integration.

The current build is **0.5.0b7**. It adds a native Play / Learn / Review / Board & settings interface, automatic local-game saves, explicit recovery, PGN import/export and replay, and HomePod speech that follows the preferred Assist voice. Start with the [release handoff](docs/RELEASE_HANDOFF.md). Software validation and physical qualification are tracked separately in the [engineering reference](docs/ENGINEERING_REFERENCE.md).

## Install

1. Use HACS with this repository as a custom integration, or copy `custom_components/phantom_chess` into your HA configuration's `custom_components` directory.
2. Restart Home Assistant. Add **Phantom Chess Board** under Settings → Devices & services.
3. Select the board's Bluetooth address. A Lichess token is optional for local play.
4. Open **Phantom Chess** in the sidebar. No additional dashboard cards, browser tokens, or CDN assets are required.

Validated Home Assistant baselines are **2026.2.3** and **2026.8.3**. Tested board firmware: **0.3.3**. Bluetooth behavior depends on adapter, proxy, firmware and host stack; a nearby ESPHome Bluetooth proxy is one supported transport. A universal claim that this firmware rejects every Linux Bluetooth adapter is not established. No firmware update is included in this build.

## Play

Choose **Play my usual game**. Your selected color and difficulty survive reloads. Move physical pieces, or select a legal piece and destination on the dashboard; dashboard moves drive the board. Promotion offers queen, rook, bishop or knight.

Pause, take back, save and end controls are available during play. Local games save their legal move history automatically. After a restart, use **Resume saved game**: recovery explicitly synchronizes the physical position and waits for completion before continuing. A failed or uncertain physical move pauses play rather than recording an unconfirmed local move or retrying robot motion blindly.

**Choose a game** offers local two-player recording, Lichess, and computer-versus-computer viewing. Historic-game playback remains available through the integration's sculpture selection and service. Online state remains authoritative at Lichess; a physical failure does not resign your online game.

## Learn and review

Play and Learn offer a **Show advantage bar** toggle, remembered in this browser. Scores are White-positive pawn units; mate scores name the winning side. Stale analysis stays visibly unavailable. With coaching enabled, recent moves and their destination squares carry quality badges. Live evaluation aids are hidden during online games.

In **Review**, select a saved or imported game and choose **Analyze game**. Local Stockfish evaluates every position, shows progress, identifies mistakes and blunders, and supplies legal suggested continuations. Select a moment to revisit, then **Practice before this move** to save a separate practice position. Resume that position when the physical board is ready. **Reanalyze game** refreshes an existing report.

Analysis continues when you leave the page. Cancel preserves progress; Continue analysis resumes it. HA restart leaves incomplete analysis paused until requested again. Active play pauses review between positions. Reviews support up to 400 half-moves and use one engine job per board, with bounded search times. Chance-loss labels are Phantom's transparent grading model, not proprietary Chess.com accuracy or brilliance ratings; live coaching shares these grades with live coaching. Full-strength analysis does not inherit the opponent's selected skill.

Review also searches saved games, replays each position, imports one PGN at a time and exports PGN. **Practice from here** creates an independent saved position without moving the board.

The library holds up to 200 games and accepts PGNs up to 256 KB. Export and delete games to make room. Unreadable records are protected against overwriting. Saved games use Home Assistant Store under `.storage/phantom_chess_games_<board-address-without-colons>` and are part of your HA configuration backup. Review caches use `.storage/phantom_chess_reviews_<board-address-without-colons>`; cache failure does not overwrite saved moves. Two-player recordings also use `phantom_chess/recordings/`.

## Engine readiness

Use **Board & settings → Check chess engine** to prepare local analysis or recover a failed engine. The check never moves pieces or plays speech. Engine archives and executables must match pinned SHA-256 digests; downloads are streamed and installed atomically. New glibc x86 downloads use the baseline build without assuming AVX2. Automatic verified packages cover Linux x86 glibc/musl and ARM64 musl; glibc ARM64 currently reports unavailable. See [artifact verification](docs/ENGINE_ARTIFACTS.md).

## Voice and HomePod

For “I want to play chess,” install the [Assist package](examples/voice-assist.yaml) as a Home Assistant package and enable local handling in the preferred Assist pipeline. The integration services can also be called by other Assist agents.

In integration options, select a native **Apple TV integration HomePod**, enable **Speak through HomePod using my preferred voice assistant**, and choose a speech volume from 0 to 1 (0.8 is 80%). Every announcement resolves the preferred Assist pipeline's TTS engine, language and exact voice. Complete audio is buffered before playback; this path does not manipulate Music Assistant queues. Speech preference changes do not reload a running game.

Disable a separate speech-forwarding automation when enabling managed HomePod speech, or make it ignore events with `delivery_managed: true`. Other speakers can use the existing TTS entity/target options. The `phantom_chess_announce` event includes `message`, `board_address`, `voice_enabled` and `delivery_managed`.

## Services and operation

See [services.yaml](custom_components/phantom_chess/services.yaml) for exact fields. Main services include `start_local_game`, `start_game`, `start_two_player_game`, `start_ai_vs_ai_game`, `execute_move`, `takeback`, `save_game`, `resume_game`, `game_library`, `back_to_modes`, `reset_position`, and `speak_homepod`. Supply `entry_id` when configuring multiple boards. The generated dashboard binds its calls to its configured entry; the sidebar dashboard currently represents one board at a time.

The engine is downloaded and cached when needed. Network access is required for its first installation and optional online analysis; there is no random-move fallback when engine move generation fails. Automated artifact checksum verification and broader hardware acceptance remain open engineering work.

If movement is uncertain, check the board and connection, then explicitly resume the saved game or reset. A command acknowledgement is not proof that all pieces reached their destination. Avoid overlapping physical actions. Reset can take minutes.

## Engineering

[Engineering reference](docs/ENGINEERING_REFERENCE.md) · [release roadmap](docs/RELEASE_ROADMAP.md) · [tests](tests/README.md).

The Vault contains current working knowledge. Correct or remove stale claims in place; do not keep superseded conclusions as alternate guidance.

# Phantom Chess for Home Assistant

Play chess on a Phantom robotic chessboard from Home Assistant. The board moves the computer's pieces for you, and a bundled dashboard lets you play, review games with a local engine, and replay famous games. This is an unofficial community integration, not affiliated with Phantom.

## What you can do

- **Play** Stockfish on the physical board at eight difficulty levels, as either colour. Games save automatically and resume after a restart.
- **Play online** on Lichess with your account, or record a two-player game between people at the board.
- **Learn** with an optional advantage bar, move-quality badges and hints.
- **Review** any saved or imported game with local Stockfish: mistakes, blunders, better moves, and practice positions.
- **Watch** the computer play itself, or replay 18 historic games on the board.
- **Hear** moves, checks and results on any Home Assistant speaker.

## Requirements

- A Phantom board. Tested on firmware 0.3.0 and 0.3.3.
- Home Assistant **2026.2.3 or newer**. Tested on 2026.2.3 and 2026.9.3.
- Bluetooth that reaches the board. For firmware **0.3.2 and later**, use an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) near the board (any ESP32 works). In testing, direct connections through the Home Assistant host's own Bluetooth adapter failed with these firmware versions.
- Internet access the first time you play locally, to download the chess engine (75–115 MB).

The local engine installs automatically on Home Assistant OS and the official container (x86-64 and 64-bit ARM, such as a Raspberry Pi 4 or 5) and on x86-64 Linux installs. It is not yet available on 64-bit ARM systems that are not Alpine-based; there, local play and review say so clearly, and online play still works.

## Install

1. In HACS, open the menu → **Custom repositories**, add `https://github.com/luketadams/phantom-chess-ha` as an **Integration**, then download **Phantom Chess Board**.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services**. Home Assistant usually discovers the board; otherwise choose **Add integration → Phantom Chess Board** and enter its Bluetooth address.
4. Optionally paste a Lichess token (see below). Leave it blank to play the local engine only.
5. Open **Phantom Chess** in the sidebar.

Manual install: copy `custom_components/phantom_chess` into your configuration's `custom_components` folder and restart.

### Lichess token (optional)

Needed only for online games. Create a [personal access token](https://lichess.org/account/oauth/token) with the **Board API** scope and paste it during setup, or add it later with **Reconfigure** on the integration.

## The dashboard

The **Phantom Chess** sidebar dashboard has four pages and needs no extra cards or plugins.

- **Play**: start your usual game, or choose colour, level, opponent (Stockfish, Lichess, two players, computer against computer) and time control. During a game, move on the board or on screen, pause, take back, save or end.
- **Learn**: the same game with coaching. Turn on the advantage bar and move badges. Hints come from full-strength analysis, not the opponent's level.
- **Review**: search, replay, import and export PGN. **Analyze game** grades every move and lists key moments; **Practice before this move** saves a new position to play from.
- **Board & settings**: board sound and speed, speech, **Check chess engine**, and **Reset board**, which returns every piece to its starting square.

Move grades use the Lichess winning-chance model with published thresholds for inaccuracies, mistakes and blunders. They are not Chess.com accuracy scores.

## Speech

In the integration's **Configure** options, choose a **TTS engine** and a **media player**. Announcements cover moves, checks and results, and can be muted from the dashboard. If speech fails, the reason appears on the dashboard.

Apple HomePods added through the Apple TV integration can use **managed speech** instead. It follows your preferred Assist pipeline's voice and buffers each message before playing it.

Every announcement also fires a `phantom_chess_announce` event (`message`, `board_address`, `voice_enabled`, `delivery_managed`) for your own automations. To start games by voice ("I want to play chess"), install the [Assist example](examples/voice-assist.yaml) as a package.

## Services

Every action is also a service under `phantom_chess.*`, for example `start_local_game`, `start_game` (Lichess), `start_two_player_game`, `start_ai_vs_ai_game`, `execute_move`, `takeback`, `save_game`, `resume_game`, `game_library`, `reset_position` and `check_engine`. See [services.yaml](custom_components/phantom_chess/services.yaml) for fields. With more than one board, pass `entry_id`.

## Privacy and network use

- **Lichess games**: your token is used only to create, stream and play your own board games. It is stored in Home Assistant's configuration and redacted from diagnostics.
- **Analysis**: to show evaluations and opening names quickly, the integration may send board positions (not your identity or token) to Lichess's public cloud-evaluation and opening-explorer services, including during local games. Game review runs entirely on the local engine.
- **Engine download**: Stockfish comes from the official Stockfish releases or, on Alpine-based installs, from [this project's mirror](https://github.com/luketadams/phantom-chess-engines) of Alpine's packages. Every download is checked against pinned SHA-256 digests before use.

## Troubleshooting

- **Board shows disconnected**: check that it is powered and within range of a Bluetooth proxy. If you use a proxy, make sure the host's own Bluetooth adapter is not taking the connection first; disabling that adapter in Home Assistant forces the proxy path.
- **"The board did not confirm…" or play paused as uncertain**: a piece may have been lifted or left between squares. Straighten the pieces, then **Resume saved game** or **Reset board**. The integration never assumes a robot move succeeded.
- **Missing pieces after a reset**: captured pieces left in the side tray cannot be moved back by the magnet. You will be asked to place them by hand.
- **Local play says the engine is unavailable**: open **Board & settings → Check chess engine** to retry the download and see the error.
- **No speech**: check the TTS engine and media player under **Configure**. The dashboard shows the last speech error.

For bug reports, download diagnostics from the integration's device page and attach them to an [issue](https://github.com/luketadams/phantom-chess-ha/issues).

## Removing the integration

Deleting the integration removes its dashboard, the downloaded engine and cached analysis. Your saved games (`.storage/phantom_chess_games_*`) and two-player recordings (`phantom_chess/recordings/`) are kept so a reinstall finds them; delete them by hand if you no longer want them.

## Known limits

- Hardware behaviour is qualified on the maintainer's board. Other firmware versions and Bluetooth setups may behave differently.
- The dashboard shows one board at a time.
- There is no setting yet that turns off the Lichess analysis lookups described above.

## Development

[Engineering reference](docs/ENGINEERING_REFERENCE.md) · [engine artifacts](docs/ENGINE_ARTIFACTS.md) · [release roadmap](docs/RELEASE_ROADMAP.md) · [tests](tests/README.md) · [changelog](CHANGELOG.md)

MIT License. Stockfish is GPLv3 software, downloaded separately at runtime.

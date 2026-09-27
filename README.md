# Phantom Chess for Home Assistant

Play chess on a Phantom robotic chessboard from Home Assistant. The board moves the computer's pieces for you, and a bundled dashboard lets you play, review games with a local engine, and replay famous games. This is an unofficial community integration, not affiliated with Phantom.

## What you can do

- **Play** Stockfish on the physical board at eight difficulty levels, as either colour. Games save automatically and resume after a restart.
- **Play online** on Lichess with your account, or record a two-player game between people at the board.
- **Learn** with an optional advantage bar, move-quality badges and hints.
- **Review** any saved or imported game with local Stockfish: mistakes, blunders, better moves, and practice positions.
- **Solve puzzles**: the Lichess daily puzzle or a random one, set up on the board. You find the moves, the board plays the replies and takes back a wrong try.
- **Practise endgames**: five drills (two-rook, queen and rook mates, promoting a pawn, holding a pawn ending) against the engine at full strength, judged automatically.
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

- **Play**: start your usual game, or choose colour, level, opponent (Stockfish, Lichess, two players, computer against computer) and time control. During a game, move on the board or on screen, pause, take back, save or end. **Puzzles** start from the same place; during a puzzle you can ask for a hint (the piece to move) or have the board play the solution. Themes and a link to the puzzle on Lichess appear when you finish. **Endgame drills** are listed there too: each shows its goal and a move limit, and ends with a clear success or failure (for example, stalemate instead of mate).
- **Learn**: the same game with coaching. Turn on the advantage bar and move badges. Hints come from full-strength analysis, not the opponent's level.
- **Review**: search, replay, import and export PGN. **Analyze game** grades every move and lists key moments; **Practice before this move** saves a new position to play from.
- **Board & settings**: board sound and speed, speech, **Check chess engine**, and **Reset board**, which returns every piece to its starting square.

Move grades use the Lichess winning-chance model with published thresholds for inaccuracies, mistakes and blunders. They are not Chess.com accuracy scores.

## Speech

In the integration's **Configure** options, choose a **TTS engine** and a **media player**. Announcements cover moves, checks and results, and can be muted from the dashboard. If speech fails, the reason appears on the dashboard.

Apple HomePods added through the Apple TV integration can use **managed speech** instead. It follows your preferred Assist pipeline's voice and buffers each message before playing it.

Every announcement also fires a `phantom_chess_announce` event (`message`, `board_address`, `voice_enabled`, `delivery_managed`) for your own automations. To start games by voice ("I want to play chess"), install the [Assist example](examples/voice-assist.yaml) as a package.

## Configuration options

Set under **Settings → Devices & services → Phantom Chess Board → Configure**. All are optional.

| Option | What it does |
|---|---|
| Text-to-speech engine | TTS entity used for announcements. Blank means announcements only fire the `phantom_chess_announce` event. |
| Speaker for play-by-play | Media player that plays announcements. Required when a TTS engine is set. |
| Voice language, Voice | Override the engine's default language or voice. |
| Speak through HomePod | Managed HomePod speech (see above) instead of the generic TTS path. |
| Speech volume | 0–1 volume for managed HomePod speech. |
| Use Lichess cloud analysis | On by default. Turn off to keep every position on this device (see Privacy). |
| Auto-provision dashboard | On by default. Turn off to build your own dashboard; the bundled one is removed on the next reload. |
| Developer debug artifacts | Writes protocol traces under `phantom_chess/debug/`. Leave off unless troubleshooting. |

## Services

Every action is also a service under `phantom_chess.*`, for example `start_local_game`, `start_game` (Lichess), `start_two_player_game`, `start_ai_vs_ai_game`, `execute_move`, `takeback`, `save_game`, `resume_game`, `game_library`, `reset_position`, `check_engine`, and `start_puzzle` / `puzzle_hint` / `puzzle_show_solution`, and `start_drill`. See [services.yaml](custom_components/phantom_chess/services.yaml) for fields. With more than one board, pass `entry_id`.

## Entities

One device per board. The dashboard uses these entities, and they are available to your own automations and cards:

- **Board state**: Connected, Battery, Firmware Mode, Live Position (FEN, with legal moves and saved games as attributes), Piece Count, Matrix Status, Board Idle, Board image.
- **Game**: Move History, Opening Name, Last Game Result, Last Game Review, Lichess game ID, player names and clocks.
- **Analysis**: evaluation (centipawns, mate, depth, source), best move, threat, last-move grade, centipawn loss and tactical motif, last game accuracy per colour.
- **Controls**: Paused, Voice announcements, Training wheels (coaching), Study mode, AI level, Player colour, mechanism speed and sound level, Lichess clock, and the computer-vs-computer levels and move delay.
- **Diagnostics** (disabled by default): Start Game and Movement Verify buttons.

## How it updates

The integration is local push. The board streams its sensor matrix and firmware state over Bluetooth notifications, and online games stream from the Lichess Board API; entity states change as those events arrive. A 30-second refresh is only a safety net. The integration reconnects on its own when the board is switched off and on or drops out of range, and it logs the outage once rather than on every retry.

## Automation examples

Send a phone notification when a game ends:

```yaml
triggers:
  - trigger: state
    entity_id: sensor.phantom_chess_board_last_game_result
    not_to: [unknown, unavailable]
actions:
  - action: notify.mobile_app_your_phone
    data:
      message: "Chess game over: {{ trigger.to_state.state }}"
```

Warn when the board's battery runs low:

```yaml
triggers:
  - trigger: numeric_state
    entity_id: sensor.phantom_chess_board_battery
    below: 15
actions:
  - action: persistent_notification.create
    data:
      message: "The chess board battery is at {{ states('sensor.phantom_chess_board_battery') }}%."
```

Replace the entity IDs with your board's; they depend on the device name.

## Privacy and network use

- **Lichess games**: your token is used only to create, stream and play your own board games. It is stored in Home Assistant's configuration and redacted from diagnostics.
- **Puzzles**: fetched from Lichess's public puzzle API without your token or account. Attempts are not sent back to Lichess and are not saved in your game library.
- **Analysis**: to show evaluations and opening names quickly, the integration may send board positions (not your identity or token) to Lichess's public cloud-evaluation and opening-explorer services, including during local games. Turn off **Use Lichess cloud analysis** under Configure to keep analysis on this device: evaluations then come from the local engine and openings are not named. Game review always runs on the local engine.
- **Engine download**: Stockfish comes from the official Stockfish releases or, on Alpine-based installs, from [this project's mirror](https://github.com/luketadams/phantom-chess-engines) of Alpine's packages. Every download is checked against pinned SHA-256 digests before use.

## Troubleshooting

Problems the integration can detect itself (the chess engine is unavailable or failed, or the board rejects commands on the current Bluetooth route) also appear under **Settings → Repairs**, with the fix, and clear automatically once resolved.

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

## Development

[Engineering reference](docs/ENGINEERING_REFERENCE.md) · [engine artifacts](docs/ENGINE_ARTIFACTS.md) · [release roadmap](docs/RELEASE_ROADMAP.md) · [tests](tests/README.md) · [changelog](CHANGELOG.md)

MIT License. Stockfish is GPLv3 software, downloaded separately at runtime.

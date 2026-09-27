# Hardware qualification script (M4)

A supervised session at the board, about 60–90 minutes. Every step says what to do, what should happen, and what to write down. Record a pass, a fail with what you saw, or "skipped". Software is not re-verified here; this checks the physical behaviour that tests cannot.

## Before you start

- Install the build under test through HACS (not by copying files), restart Home Assistant, and open **Phantom Chess**.
- Board on, charged above 40 %, all 32 pieces on their starting squares. Bluetooth proxy powered and near the board.
- **Board & settings → Check chess engine** shows **Ready**.
- **Settings → Repairs** has no Phantom Chess entries.
- Integration options: **Developer debug artifacts** on for this session (turn it off afterwards).
- Keep this page open to note times. Times help match notes to the debug log.

## A. Core local game (about 25 minutes)

Play as White against level 1 so you control the game's shape.

| # | Do | Expect | Record |
|---|---|---|---|
| A1 | **Play my usual game** (White, level 1). | Pieces stay put; status "White to move"; the start is announced. | Time to ready. |
| A2 | Play 1. e4. | The board confirms your move; the computer replies within a few seconds; both moves appear in Moves and are spoken. | Any delay over 10 s. |
| A3 | Capture a piece. | The captured piece is moved to the side tray by the board after its reply, or by you, and the game continues. | Where the piece ended up. |
| A4 | Castle kingside (move the king two squares, then the rook). | Recorded as O-O; the computer replies normally. | Whether the rook move was needed before the reply. |
| A5 | Get an en passant capture if the game allows (for example play a pawn to the fifth rank and wait for an adjacent two-square advance). | Recorded as a capture; the captured pawn is removed from its square, not the destination. | Skip if it never arises. |
| A6 | Promote a pawn (it may take a while at level 1; use the on-screen board to speed things up). | The promotion choice appears on screen; the chosen piece is recorded. Note what the board does physically with the promoted piece. | The piece you used and what the board did. |
| A7 | While the computer's piece is moving, tap **Pause**. | The move finishes, then play is paused; nothing is lost. **Continue** resumes. | Whether the in-flight move completed. |
| A8 | **Take back** once on your turn. | Your last move and the computer's reply are both undone; the board moves both pieces back and it is your turn again. | Pieces that did not return. |
| A9 | **Save & pause**, then **Resume saved game**. | Same position, same side to move. | — |
| A10 | Finish the game by checkmate (either side) or **End game**. | Result shown with a review link; checkmate is spoken. | — |
| A11 | **Reset board**. | All pieces return to their starting squares; any tray pieces you must place by hand are named in a notification. | Pieces left out of place. |

## B. Recovery (about 20 minutes)

| # | Do | Expect | Record |
|---|---|---|---|
| B1 | Start a local game and play three moves. Unplug the Bluetooth proxy for 30 s, then plug it back in. | "Connected" drops and returns within about a minute; the game is intact; play continues on your next move. | Reconnect time; anything lost. |
| B2 | Unplug the proxy **while the computer's piece is moving**. | Play pauses as "uncertain" instead of guessing. After reconnecting, **Resume saved game** restores the last confirmed position. | What the pieces and the screen showed. |
| B3 | With a game in progress, restart Home Assistant (Settings → System → Restart). | After restart the dashboard offers **Resume saved game**; resuming sets the board to the saved position. | Time until the board reconnects. |
| B4 | **Reset from a finished game**: finish or end a game, then **Reset board**. | Pieces return to start. This is the case that failed on 5 September (firmware stuck after game end). | The Firmware Mode sensor values during the reset, and whether it completed. |
| B5 | **Reset mid-game**: start a game, play four moves, **Reset board**. | Pieces return to start. | Same as B4. |

## C. Voice and speech (about 10 minutes)

| # | Do | Expect | Record |
|---|---|---|---|
| C1 | Say "I want to play chess". | Assist replies right away; the game starts. | Words that were misheard. |
| C2 | Play until a check and a checkmate. | Moves, "check" and "checkmate, … wins" are spoken once each. | Missing or doubled speech. |
| C3 | Switch speech to a non-HomePod speaker (Configure → TTS engine + speaker, managed HomePod speech off) and make a move. | The move is spoken on that speaker; no error on the dashboard. | — |
| C4 | Turn **Spoken move announcements** off and make a move. | Silence. | — |

## D. Other modes (about 15 minutes)

| # | Do | Expect | Record |
|---|---|---|---|
| D1 | **Play online** (needs a Lichess token). Make two moves, then **Take back**. | Moves reach Lichess; the takeback is accepted by Lichess first, then the board rearranges. | Whether Lichess and the board agree. |
| D2 | **Two people at the board**: play four moves for both sides, then end. | Both sides recorded; the game appears in Review. | — |
| D3 | Start one historic game from the sculpture list and let it play ten moves, then end. | Moves play with commentary; ending stops cleanly. | — |

## E. Repairs (5 minutes, optional)

| # | Do | Expect |
|---|---|---|
| E1 | In **Settings → Devices & services → Bluetooth**, enable the host's own adapter, restart, and start a game. | If the board connects through the host adapter and rejects the start, **Repairs** shows "Chess board rejects game commands on this Bluetooth connection". Disable the adapter again afterwards; the entry clears on the next successful start. |

## F. Puzzles, drills and local-only analysis (about 20 minutes)

Qualifies the three features added for 0.5.0b9. Neither puzzles nor drills need a Lichess token. Fetching a puzzle always reaches `lichess.org/api/puzzle/…` regardless of the analysis option tested in F7–F9 — that option only gates evaluation and the opening explorer, not the puzzle fetch itself.

| # | Do | Expect | Record |
|---|---|---|---|
| F1 | Open **Choose a game → Puzzles → Daily puzzle**. | The board is driven to the puzzle's position; "Puzzle rated \<rating\>. \<White/Black\> to move" is spoken; the panel shows "Lichess puzzle · rated \<rating\>" and "Move 1 of N". | The rating and puzzle URL shown. |
| F2 | Play a legal move you know is not the solution. | Verdict is wrong: the piece is taken back physically without you doing anything else, "Not the move. Try again." is spoken, and it's your move again — the panel still reads "Move 1 of N" but now adds "· 1 wrong try". | Whether the piece returned to its square on its own. |
| F3 | Tap **Hint**. | A notice reading "Hint: move the piece on \<square\>" appears in the panel and is spoken as "Look at the piece on \<square\>." Only the origin square is named — the destination and piece are not. | The square named. |
| F4 | Tap **Show solution**. | "Here is the solution." is spoken, then the rest of the line plays on the board by itself, about 1.5 s between moves. The puzzle ends with the panel reading "The solution was shown." and an **Another puzzle** button — there is no further "solved" announcement, because showing the solution doesn't count as solving it. | Whether every remaining move played correctly on the board. |
| F5 | Open **Choose a game → Endgame drills → Queen and king mate · beginner**. | The board is driven to king + queen vs. lone king; "Queen and king mate. Checkmate with queen and king. Box the king in, then bring your king up. Avoid stalemate." is spoken; the panel shows "Move 0 of 15". | — |
| F6 | Play it out — checkmate the lone king, or let the 15-move limit pass without mating. | On mate: "Drill complete. Checkmate." is spoken and the panel shows it as complete. If the limit passes first: "Drill failed. The 15-move limit was reached." is spoken and the panel shows it as not this time. Either way a **Try again** button appears; the engine defends at full strength throughout (level 8, regardless of your usual AI level). | Which outcome, and how many moves it took. |
| F7 | With **Configure → Use Lichess cloud analysis** still on (the default), start **Play my usual game** and play a well-known opening, e.g. 1. e4 e5 2. Nf3. | Within a couple of moves, **Learn → Opening** names something other than "No opening identified". | The name shown, if any. |
| F8 | Open **Configure** and turn **Use Lichess cloud analysis** off, then submit. | Applies immediately — no restart, no error, whether or not a game is in progress. | — |
| F9 | **End game**, start a fresh **Play my usual game**, and play the same opening moves as F7. | **Learn → Opening** stays "No opening identified" for the whole game. The diagnostic **Eval Source** sensor (Settings → Devices & services → Phantom Chess board → entity list, or the board device's Download diagnostics) reads `stockfish-local` after each move — never `lichess-cloud`. For a stronger check, set the `custom_components.phantom_chess.lichess_analysis` logger to debug beforehand: with the option off there must be no `cloud-eval cache miss for …` or `cloud-eval HTTP … for …` line for any of these positions. Results cached while the option was on (F7) are not shown now either. | Whether Opening or Eval Source ever showed cloud data; whether any cloud-eval log line appeared. |

Turn **Use Lichess cloud analysis** back on afterward if that's how you found it.

## After the session

- Turn **Developer debug artifacts** off.
- Send the notes, with times. The debug captures under `phantom_chess/debug/` let failures be matched to Bluetooth traffic.
- Each failure is either fixed before 0.5.0rc1 or listed under Known limits in the README.

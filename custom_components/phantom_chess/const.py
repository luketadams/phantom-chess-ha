"""Constants for the Phantom Chess Board integration."""

DOMAIN = "phantom_chess"

# ── BLE ──────────────────────────────────────────────────────────────────────

# Primary GATT service
BLE_SERVICE_UUID = "fd31a840-22e7-11eb-adc1-0242ac120002"

# Characteristic UUIDs (from phantomchessboard/Firmware → include/BLE.h)
UUID_RECEIVE_MOVEMENT = "c60c786b-bf3f-49d8-bd9e-c268e0519a7b"  # Write AI moves to board
UUID_STATUS_BOARD     = "06034924-77e8-433e-ac4c-27302e5e853f"  # Notify: physical move made
UUID_SELECT_MODE      = "c08d3691-e60f-4467-b2d0-4a4b7c72777e"  # Write board mode (1-6)
UUID_PAUSE            = "cc4cbbe0-9b5a-11ee-b9d1-0242ac120002"  # Write pause/resume
UUID_BATTERY_INFO     = "7b204548-40c4-11eb-adc1-0242ac120002"  # Notify: battery state
UUID_SEND_MATRIX      = "1b034927-77e8-433e-ac4c-27302e5e853f"  # Write LED matrix
UUID_ERROR_MSG        = "7b204d4a-30c3-11eb-adc1-0242ac120002"  # Notify: error messages
UUID_PLAY_INFO        = "d7f0b4ea-9b52-11ee-b9d1-0242ac120002"  # Game parameters
UUID_VERSION          = "392d9e66-937a-11ee-b9d1-0242ac120002"  # Read firmware version
UUID_MECHANISM_SPEED  = "acb646cc-92ca-11ee-b9d1-0242ac120002"  # Write piece movement speed
UUID_SOUND_LEVEL      = "acb64a32-92ca-11ee-b9d1-0242ac120002"  # Write sound volume
UUID_TAKEBACK         = "89185e7a-78ef-4bb0-b48f-c0f53f21fc1b"  # Write takeback request
UUID_CHECK_MOVE       = "9cc3b57e-eee5-4d3e-8c1d-3fbd636d6780"  # Validate a move
UUID_VOICE            = "3e42feb6-7c91-4e17-a1ed-31b51840613f"  # Voice enable/disable
UUID_JUMP_TO_CENTER   = "5e316147-4550-4cf3-8e2b-edc098312a43"  # Move piece to center (confirmed on board)
# UUID_MATRIX_INIT_GAME e00b41ea was a speculative LED-init characteristic
# for firmware-0.1.6 era. Doesn't exist on 0.3.0. Removed 2026-05-17 after
# its write triggered a false-positive GATT-staleness force-reconnect that
# aborted Lichess game activation. Kept here as a comment so future audits
# don't re-add it.
UUID_SINGLE_MOVE      = "d9a6b488-1d61-423f-8713-f3b0eedc9904"  # Move single piece
UUID_OTA              = "93601602-bbc2-4e53-95bd-a3ba326bc04b"  # OTA firmware update
UUID_BOARD_ROTATION   = "b5a650ea-92ca-11ee-b9d1-0242ac120002"  # 0=normal, 1=rotated 180°
# ── Firmware 0.3.0 UUIDs (not present in 0.1.6) ──────────────────────────────
# UUID_GAME (cc68a66e) is the consolidated gameplay-flow channel. All opcodes
# 0–14 (GAME_START, GAME_END, MOVEMENT, MOVEMENT_VERIFY, VOICE_COMMAND,
# TAKE_BACK, BOARD_STATE, CALIBRATION, ERROR_MSG, CHECK_SOUND, SIDE,
# GAME_ASSISTANCE, BLE_MOVE_DONE, SNAPTOCENTERCOM, RESET_DETECTION) are
# routed through this single characteristic. Authoritative reference:
# EFRAIN_GAMEPLAY_DOC_2026-05-14.txt (Efraín's protocol doc).
UUID_GAME             = "cc68a66e-3bfa-4614-a77f-f46954a4c103"  # Consolidated gameplay channel (firmware 0.3.0+)
# UUID_SCULPTURE (7eeaef37) — Per Efraín 2026-05-24, this is named
# UUID_SCULPTURE in firmware source, NOT "UUID_GAME_CONFIG". The single-byte
# read/write the integration historically used here is meaningless to the
# firmware — the firmware does NOT interpret bytes on this characteristic
# as `(ai_level*2)+(1 if white else 0)`; that encoding is app-side only.
# Real use: sculpture-mode opcodes 9 (PLAYLIST), 11 (DATABASE), 12
# (CHECK_SOUNDS — write "0"/"1" to disable/enable sounds during playback;
# persisted as `checkSoundsSc` preference). This is also the only
# characteristic on which the firmware uses application-level chunking.
# Reference: Luke's protocol-questions reply, 2026-05-24, item 7.
UUID_SCULPTURE        = "7eeaef37-1078-4462-9fcc-1a2a1152da45"
# Backward-compat alias for the old (incorrect) name. Slated for removal
# in a future cleanup; keep one release cycle so external scripts/tests
# importing UUID_GAME_CONFIG from this module don't break immediately.
UUID_GAME_CONFIG      = UUID_SCULPTURE  # DEPRECATED: use UUID_SCULPTURE
# UUID_SLIDE_DELAY (NEW in firmware 0.3.2; one-byte difference from
# UUID_BOARD_ROTATION's `…0002`). Integer-as-string ms, range 0–1500,
# default 250. Stored in preferences; applied immediately.
UUID_SLIDE_DELAY      = "b5a650ea-92ca-11ee-b9d1-0242ac120004"
# UUID_RECEIVE_MOVE_V2 is the firmware's notify-only 1b034928 characteristic
# (the public Firmware repo calls it SEND_TESTMODE_ERROR). It is NOT the
# movement channel — movement goes through UUID_GAME opcode 2. Kept for
# subscription/observation only. Renamed in comment 2026-05-24; consider
# renaming the symbol itself in a future const.py refresh.
UUID_RECEIVE_MOVE_V2  = "1b034928-77e8-433e-ac4c-27302e5e853f"  # Notify-only firmware diagnostic (NOT the movement channel)
UUID_OFFSET_PIECES    = "acb650ea-92ca-11ee-b9d1-0242ac120002"  # Piece offset calibration
UUID_SET_STATE        = "acb6543c-92ca-11ee-b9d1-0242ac120002"  # Set board state string
UUID_CALIB_TYPE       = "c43f07d7-a64a-4776-a35f-6190a53c1c86"  # Calibration type
UUID_FACTORY_RESET    = "b583ff00-b77a-42f5-a53f-a9bf4c291d80"  # Factory reset
UUID_PLAYLIST         = "4f1c9720-939a-11ee-b9d1-0242ac120002"  # Playlist control
UUID_PLAYLIST_DB      = "a00125d2-cf9e-494a-b834-6dad6360729c"  # Playlist database
UUID_PLAYLIST_SYNC    = "ea41f202-d149-4a1d-80a7-09b4a613be7f"  # Playlist sync
UUID_PLAYLIST_DEL     = "855fce26-94df-4b3f-b5a8-735a85d220fe"  # Playlist delete

# Board modes (written to UUID_SELECT_MODE)
MODE_SCULPTURE  = 1
MODE_CHESS_PLAY = 2
MODE_PAUSE      = 3
MODE_TEST       = 4
MODE_TUTORIAL   = 5
MODE_CALIBRATION = 6

# Move format: "M 1 e2-e4" or "M 1 d5xe4"
# Strip "M 1 " prefix and replace "-" or "x" to get UCI: "e2e4" or "d5e4"
MOVE_PREFIX = "M 1 "
MOVE_MAX_LEN = 25  # board rejects moves longer than 25 chars

# Battery notification format: "percent,wallStatus,charging,doneCharging"
# Example: "85,1,0,0"

# ── Lichess Board API ─────────────────────────────────────────────────────────

LICHESS_API_BASE    = "https://lichess.org/api"
LICHESS_ACCOUNT_URL = f"{LICHESS_API_BASE}/account"
LICHESS_CHALLENGE_AI_URL = f"{LICHESS_API_BASE}/challenge/ai"
LICHESS_GAME_STREAM_URL  = f"{LICHESS_API_BASE}/board/game/stream/{{game_id}}"
LICHESS_MOVE_URL         = f"{LICHESS_API_BASE}/board/game/{{game_id}}/move/{{move}}"
LICHESS_RESIGN_URL       = f"{LICHESS_API_BASE}/board/game/{{game_id}}/resign"
LICHESS_ABORT_URL        = f"{LICHESS_API_BASE}/board/game/{{game_id}}/abort"
# Used by async_reconcile_lichess_state to query a single game's status as
# a one-shot REST call — workaround when the streaming endpoint has
# missed the terminal event (Task #14, 2026-05-16).
LICHESS_GAME_EXPORT_URL  = f"{LICHESS_API_BASE}/game/export/{{game_id}}"

# AI levels 1-8 correspond to Lichess stockfish levels
LICHESS_AI_LEVELS = [1, 2, 3, 4, 5, 6, 7, 8]

# ── Config entry keys ─────────────────────────────────────────────────────────

CONF_BLE_ADDRESS   = "ble_address"
CONF_DEVICE_NAME   = "device_name"
CONF_LICHESS_TOKEN = "lichess_token"
CONF_LICHESS_USER  = "lichess_username"

# ── Default values ────────────────────────────────────────────────────────────

DEFAULT_AI_LEVEL      = 3
DEFAULT_MECHANISM_SPEED = 50
DEFAULT_SOUND_LEVEL     = 70
DEFAULT_PLAYER_COLOR    = "random"

# ── Entity unique ID prefixes ─────────────────────────────────────────────────

ENTITY_BATTERY       = "battery"
ENTITY_LICHESS_ID    = "lichess_game_id"
ENTITY_CONNECTED     = "connected"
ENTITY_AI_LEVEL      = "ai_level"
ENTITY_PLAYER_COLOR  = "player_color"
# Integration-owned mode picker + sculpture-game picker (replaces the
# `input_select.phantom_chess_setup_mode` / `input_select.phantom_chess_sculpture_game`
# helpers that v0.3 required users to create by hand). Added 2026-05-25
# as v0.4-alpha1: first step of folding Option C work into the integration.
ENTITY_SETUP_MODE      = "setup_mode"
ENTITY_SCULPTURE_GAME  = "sculpture_game"
# v0.4-alpha2: integration-owned training-wheels toggle + Lichess clock controls.
# Replace the hand-rolled input_boolean.phantom_chess_training_wheels +
# input_number.phantom_chess_lichess_clock_minutes / _increment helpers
# that v0.3's examples/helpers.yaml required.
ENTITY_TRAINING_WHEELS         = "training_wheels"
# v0.4-beta3: integration-owned master mute for the HA-side play-by-play
# TTS (the spoken narration/coaching). Pure-local config storage (no BLE);
# state persists across HA restarts via RestoreEntity. Default ON so a
# fresh install keeps the existing announce behaviour.
ENTITY_VOICE_ANNOUNCEMENTS      = "voice_announcements"
DEFAULT_VOICE_ANNOUNCEMENTS     = True
ENTITY_LICHESS_CLOCK_MINUTES   = "lichess_clock_minutes"
ENTITY_LICHESS_CLOCK_INCREMENT = "lichess_clock_increment"
# v0.4-alpha30: AI-vs-AI spectator mode. Three integration-owned
# sliders that the dashboard's 5th mode tile reads when firing
# `phantom_chess.start_ai_vs_ai_game`. All three are pure-local config
# storage (no BLE writes); state persists across HA restarts via
# RestoreEntity. Defaults match the service's own defaults so a fresh
# install just works.
ENTITY_WHITE_AI_LEVEL          = "white_ai_level"
ENTITY_BLACK_AI_LEVEL          = "black_ai_level"
ENTITY_AI_VS_AI_MOVE_DELAY     = "ai_vs_ai_move_delay"
# v0.4-alpha3: integration-owned 60s-idle gate. Replaces the template
# binary_sensor.phantom_chess_board_idle that v0.3 required users to
# create via the template integration's helper UI. Used by the dashboard
# to know when to render the mode picker (firmware has been idle long
# enough) vs the live-game cards (firmware is mid-move).
ENTITY_BOARD_IDLE   = "board_idle"
BOARD_IDLE_THRESHOLD_SECONDS = 60.0  # match v0.3's template behavior

# C3 (deep-dive 2026-07-06): integration-owned "should the mode picker
# render?" gate. The dashboard previously copy-pasted a 10-state
# firmware_mode OR-block + idle/connected/no-active-game gate into six
# conditional cards; every new firmware label was a six-place edit and a
# silent blank-screen regression risk. `binary_sensor.<mac>_picker_available`
# centralises that logic (unit-tested against every label) so each YAML
# conditional collapses to a 2-line `picker_available == on` + setup-mode
# check.
ENTITY_PICKER_AVAILABLE = "picker_available"

# The firmware-mode labels during which the board is in a settled home /
# standby state and the mode picker (and per-mode setup views) should
# render. These are the six states the OLD dashboard's five *setup* views
# gated on. Deliberately EXCLUDES the four transient "the magnet is
# rearranging pieces" labels (Snapping Pieces / Snap to Center / Calibrating
# / Setting Up): those are owned by the dedicated stand-by interstitial
# cards, which fire on the specific firmware_mode. Splitting the two keeps
# the picker/setup views and the interstitials on DISJOINT firmware sets, so
# exactly one renders — no double-render, no blank screen.
#
# The old *main* mode picker additionally OR'd in the four transient labels
# (so it stayed visible if the board wedged mid-snap while on "Choose a
# mode"). That coverage moves to the interstitials: dropping their
# "Choose a mode" exclusion lets them own the transient states for every
# setup mode uniformly (see dashboard_template.yaml).
#
# "unknown"/"unavailable" cover a freshly-connected or wedged firmware_mode
# sensor (in the binary sensor a firmware_mode of None maps here); the
# connected gate keeps the picker hidden when the board is actually offline.
PICKER_FIRMWARE_MODES = frozenset(
    {
        "HOME",
        "Waiting Side",
        "unknown",
        "unavailable",
        "BLE Playing",
        "Board Playing",
    }
)
DEFAULT_TRAINING_WHEELS        = False
DEFAULT_LICHESS_CLOCK_MINUTES  = 30   # default rapid pace (matches Lichess's typical 30+0)
DEFAULT_LICHESS_CLOCK_INCREMENT = 0  # seconds per move; 0 = no increment
ENTITY_MECH_SPEED    = "mechanism_speed"
ENTITY_SOUND_LEVEL   = "sound_level"
ENTITY_PAUSE         = "pause"
# Live matrix-state sensor (firmware 0.3.0 — derived from UUID_SEND_MATRIX notifications)
ENTITY_LIVE_POSITION       = "live_position"
ENTITY_PIECE_COUNT         = "piece_count"
ENTITY_FIRMWARE_MODE       = "firmware_mode"
ENTITY_MATRIX_STATUS       = "matrix_status"
ENTITY_FIRMWARE_LAST_MOVE  = "firmware_last_move"

# ── In-game learning dashboard (added 2026-05-14) ────────────────────────────
# All exposed as sensors/binary_sensors driven by self._state on the coordinator.
# Backed by Lichess cloud-eval + python-chess analysis. See
# phantom_chess_research/IN_GAME_DASHBOARD_SPEC_2026-05-14.md for data model.

ENTITY_LICHESS_ACTIVE          = "lichess_active"           # binary
ENTITY_LICHESS_REVIEW_READY    = "lichess_review_ready"     # binary
# True when EITHER a Lichess game OR a local-Stockfish game is in progress.
# Drives the rich learning-dashboard view so it renders for both modes.
# Added 2026-05-16 (Task #9).
ENTITY_LEARNING_VIEW_ACTIVE    = "learning_view_active"     # binary
ENTITY_LICHESS_WHITE_NAME      = "lichess_white_name"
ENTITY_LICHESS_BLACK_NAME      = "lichess_black_name"
ENTITY_LICHESS_WHITE_CLOCK     = "lichess_white_clock"         # raw seconds
ENTITY_LICHESS_BLACK_CLOCK     = "lichess_black_clock"
ENTITY_LICHESS_WHITE_CLOCK_DISP = "lichess_white_clock_display"  # mm:ss string
ENTITY_LICHESS_BLACK_CLOCK_DISP = "lichess_black_clock_display"

ENTITY_OPENING_NAME            = "opening_name"

ENTITY_EVAL_CP                 = "eval_cp"
ENTITY_EVAL_MATE               = "eval_mate"
ENTITY_EVAL_SOURCE             = "eval_source"
ENTITY_EVAL_DEPTH              = "eval_depth"
ENTITY_BEST_MOVE_SAN           = "best_move_san"

ENTITY_LAST_MOVE_CLASSIFICATION = "last_move_classification"
ENTITY_LAST_MOVE_CPL            = "last_move_cpl"
ENTITY_LAST_MOVE_MOTIF          = "last_move_motif"

ENTITY_THREAT_SAN              = "threat_san"

ENTITY_MOVE_HISTORY            = "move_history"   # state = ply count; attribute "moves" = list

ENTITY_LAST_GAME_RESULT        = "last_game_result"
ENTITY_LAST_GAME_ACCURACY_W    = "last_game_accuracy_white"
ENTITY_LAST_GAME_ACCURACY_B    = "last_game_accuracy_black"
ENTITY_LAST_GAME_REVIEW        = "last_game_review"  # state = mistake count; attribute "top_mistakes" = list

# Classification labels (kept in sync with the spec)
CLASSIFICATION_BRILLIANT  = "brilliant"
CLASSIFICATION_BEST       = "best"
CLASSIFICATION_GOOD       = "good"
CLASSIFICATION_BOOK       = "book"
CLASSIFICATION_INACCURACY = "inaccuracy"
CLASSIFICATION_MISTAKE    = "mistake"
CLASSIFICATION_BLUNDER    = "blunder"
CLASSIFICATION_UNKNOWN    = "unknown"

# CPL thresholds (centipawns lost) — per the spec
CPL_GOOD_MAX        = 20    # < 20 = best/good
CPL_INACCURACY_MAX  = 100   # < 100 = inaccuracy
CPL_MISTAKE_MAX     = 300   # < 300 = mistake; ≥ 300 = blunder

# Cloud-eval / opening explorer URLs
LICHESS_CLOUD_EVAL_URL = "https://lichess.org/api/cloud-eval"
LICHESS_OPENING_URL    = "https://explorer.lichess.ovh/masters"

# UUID for the firmware's mode-state notification channel (Running/Paused/etc.)
# Same as UUID_SET_STATE (acb6543c). Reading returns current mode as ASCII string.
UUID_FIRMWARE_STATE = "acb6543c-92ca-11ee-b9d1-0242ac120002"

# ── Game status values ────────────────────────────────────────────────────────

STATUS_IDLE       = "idle"
STATUS_PLAYING    = "playing"
STATUS_CHECK      = "check"
STATUS_CHECKMATE  = "checkmate"
STATUS_STALEMATE  = "stalemate"
STATUS_DRAW       = "draw"
STATUS_PAUSED     = "paused"
STATUS_RESIGNED   = "resigned"

# ── Reconnection ──────────────────────────────────────────────────────────────

BLE_RETRY_SECONDS      = 10
BLE_MAX_RETRY_SECONDS  = 60
LICHESS_RETRY_SECONDS  = 5


# ── Move-detection stability (audit 2026-06-09 M2/M3/M9) ─────────────────────

# M2 — double-fire move dedup window. A slow physical piece-slide makes the
# firmware emit TWO `\x03M` movementVerify notifications for one move; the
# second can arrive as a distinct placement string that happens to be legal in
# the new position and would be applied as a phantom second move. The move-apply
# path records the (uci, loop-monotonic ts) of every APPLIED human move; a
# following move-frame that resolves to the same UCI (or its 180° rotation) and
# lands within this window is dropped as a refire. Distinct moves inside the
# window (legitimate blitz premoves) are NOT dropped. ~400 ms comfortably covers
# the observed intra-slide gap without swallowing real back-to-back moves.
MOVE_DEDUP_WINDOW_SECONDS: float = 0.4

# M3 — wedged-board circuit breaker. `_phantom_execute_position` returns False
# (BLE_MOVE_DONE never arrived) on every ply when the board is physically
# wedged; the AI-vs-AI and sculpture loops otherwise re-drive forever, grinding
# the magnet. After this many CONSECUTIVE move-delivery failures a loop stops
# cleanly and raises a persistent notification suggesting `resync_detection`.
# A single delivered move resets the counter.
PHANTOM_EXEC_FAILURE_LIMIT: int = 3

# M9 — AI-echo expiry backstop, aligned to the post-activation settle window
# (`_activation_settle_until`, 600 s in `_phantom_execute_position`). The echo
# set is normally cleared the moment BLE_MOVE_DONE fires (see
# AI_ECHO_MOVE_DONE_GRACE_SECONDS); this time backstop only matters when a
# BLE_MOVE_DONE is never delivered. The former fixed 60 s value was SHORTER than
# the 600 s settle window, so a slow castle echo landing 60–600 s after the AI
# move was mis-processed as a human move. Raised to match the settle semantics.
AI_ECHO_BACKSTOP_SECONDS: float = 600.0

# M9 — grace period after BLE_MOVE_DONE before the AI echo set is cleared.
# Castling fires TWO physical movements (king + rook); the rook's `\x03M` echo
# can land a beat AFTER BLE_MOVE_DONE (which signals the whole magnet sequence
# is done). Clearing the echo set immediately on move-done would let that
# trailing rook echo be mis-read as a human move, so we hold the set for this
# grace period first. A few seconds spans the king→rook echo gap.
AI_ECHO_MOVE_DONE_GRACE_SECONDS: float = 4.0

# Task 5 (live bug 2026-07-02) — minimum fraction of a color's plies that must
# be ANALYZED (real Lichess/engine eval, not the "unknown" stub) before the
# post-game review will report an accuracy for that color. During fast sculpture
# playback the cloud evals are fire-and-forget and mostly don't complete, so the
# per-ply CPLs stay at their stub 0 while a handful of resolved mate-swing plies
# read ~9999 — averaging that mix fabricated a 0.0. Below this fraction the
# review reports None (sensor shows "unknown") instead of a fabricated number.
POST_GAME_MIN_ANALYZED_FRACTION: float = 0.6


# ── Mode picker + sculpture catalog (v0.4-alpha1, Option C step 1) ───────────

# Mode-picker options. Match what v0.3's `examples/helpers.yaml` defines so
# dashboards built against either version see the same option strings.
SETUP_MODE_OPTIONS = [
    "Choose a mode",
    "Play with Lichess",
    "Play with Stockfish",
    "Sculpture Library",
    "2-Player Game",
    # v0.4-alpha30: AI-vs-AI spectator mode. Stockfish plays both sides
    # on the physical board so the user can watch. The mode-tile click
    # sets this option; the conditional-card section reads it to render
    # the level sliders + Start button.
    "Watch AI vs AI",
]
DEFAULT_SETUP_MODE = "Choose a mode"

# Sculpture-game catalog. Selecting one in the sculpture-library dropdown
# sets `coordinator.selected_sculpture`. The catalog matches v0.3's
# `examples/helpers.yaml`. New games can be added here without a breaking
# change since the picker's options are derived from this constant.
SCULPTURE_GAMES = [
    "1851 — Anderssen vs Kieseritzky (Immortal Game)",
    "1858 — Morphy's Opera Game",
    "1907 — Rotlewi vs Rubinstein (Immortal)",
    "1924 — Réti vs Capablanca (NY)",
    "1933 — Einstein vs Oppenheimer",
    "1938 — Botvinnik vs Capablanca (AVRO)",
    "1956 — Byrne vs Fischer (Game of the Century)",
    "1958 — Polugaevsky vs Nezhmetdinov (Immortal)",
    "1967 — Fischer vs Myagmarsuren",
    "1972 — Fischer vs Spassky (WCC Game 6)",
    "1985 — Karpov vs Kasparov (WCC)",
    "1997 — Deep Blue vs Kasparov (Game 6)",
    "1999 — Kasparov vs Topalov (Immortal)",
    "2013 — Aronian vs Anand (Anand's Immortal)",
    "2015 — Wei Yi vs Bruzon (King Hunt)",
    "2016 — Carlsen vs Karjakin (WCC Tiebreak)",
    "2021 — Carlsen vs Nepomniachtchi (WCC G6, 136 moves)",
    "2023 — Nepomniachtchi vs Ding (WCC TB G4)",
]
DEFAULT_SCULPTURE_GAME = SCULPTURE_GAMES[0]

"""Image entity for Phantom Chess Board — renders the live FEN as SVG.

The frontend re-fetches whenever `image_last_updated` changes. We only bump
that timestamp when the inputs that affect the rendered output actually
change (FEN, last move, orientation, classification glyph in training-wheels
mode), per HA's developer docs.

References:
- https://developers.home-assistant.io/docs/core/entity/image/
- https://python-chess.readthedocs.io/en/latest/svg.html
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from html import escape as _html_escape

import chess
import chess.svg

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BLE_ADDRESS, CONF_DEVICE_NAME, DOMAIN
from .coordinator import PhantomChessCoordinator
from .lichess_analysis import classification_color_glyph

_LOGGER = logging.getLogger(__name__)

ENTITY_BOARD_IMAGE = "board_image"

# Board-only starting FEN — what live_position emits when no game has run.
STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"

# Rendered image dimensions, in pixels (square).
# python-chess SVG with coordinates=True uses a 20-pixel coordinate border
# on each side, with the playable 8×8 grid filling the rest. With size=400:
#   - margin = 20 px each side
#   - square = (400 - 2*20) / 8 = 45 px
# We use those constants to compute pixel centers for SVG overlays.
RENDER_SIZE = 400
_SVG_MARGIN = 20
_SVG_SQUARE = (RENDER_SIZE - 2 * _SVG_MARGIN) // 8  # 45 px

# Training-wheels toggle. Set via the dashboard's training-wheels switch
# (input_boolean.phantom_chess_training_wheels). When ON, the board image
# overlays the most-recent move's classification glyph (!! / ! / ?! / ? / ??)
# directly on the destination square.
_TRAINING_WHEELS_ENTITY = "input_boolean.phantom_chess_training_wheels"

# Classifications that are worth surfacing visually on the board. "Best" and
# "book" produce a checkmark / book emoji which clutters the view without
# adding much teaching value — skip them. "Unknown" is just noise.
_SUPPRESSED_GLYPH_CLASSES = {"best", "book", "unknown"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PhantomChessCoordinator = hass.data[DOMAIN][entry.entry_id]
    address = entry.data[CONF_BLE_ADDRESS]
    name = entry.data.get(CONF_DEVICE_NAME, "Phantom Chess Board")
    async_add_entities(
        [PhantomChessBoardImage(hass, coordinator, entry, address, name)]
    )


class PhantomChessBoardImage(CoordinatorEntity[PhantomChessCoordinator], ImageEntity):
    """SVG render of the live board state.

    Inputs:
    - `coordinator.data["live_fen"]` — board-only FEN (8 ranks separated by /).
    - `coordinator.data["last_move"]` — UCI move string (e.g. "e2e4"), used to
      highlight the from/to squares. May be None.
    - `coordinator.player_color` — "white" | "black" | "random". White-at-bottom
      orientation for "white" or "random"; flipped for "black".
    """

    _attr_has_entity_name = True
    _attr_name = "Board"
    _attr_icon = "mdi:chess-board"
    _attr_content_type = "image/svg+xml"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: PhantomChessCoordinator,
        entry: ConfigEntry,
        address: str,
        device_name: str,
    ) -> None:
        # Explicit dual init — both parents call Entity.__init__ via their own
        # super() chains; doing both is the documented safe pattern.
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)

        self._address = address
        self._attr_unique_id = f"{address}_{ENTITY_BOARD_IMAGE}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, address)},
            "name": device_name,
            "manufacturer": "Phantom",
            "model": "Phantom Chess Board",
        }

        # Tracked between updates so we only bump image_last_updated when the
        # rendered output would actually change.
        self._last_rendered_fen: str | None = None
        self._last_rendered_move: str | None = None
        self._last_rendered_orientation: bool | None = None
        # Track the move-quality glyph overlay too — in training-wheels mode
        # the board image embeds the most-recent classification, so a change
        # in classification (or training-wheels mode toggling) needs to
        # invalidate the cached image. (Task #26, 2026-05-17.)
        self._last_rendered_glyph_state: tuple[str | None, str | None, bool] = (
            None, None, False
        )

        # Seed timestamp so the frontend fetches on first dashboard load.
        self._attr_image_last_updated = datetime.now(timezone.utc)

    # ── Render inputs ────────────────────────────────────────────────────────

    def _current_fen(self) -> str:
        return (self.coordinator.data or {}).get("live_fen") or STARTING_FEN

    def _current_last_move(self) -> str | None:
        return (self.coordinator.data or {}).get("last_move")

    def _current_orientation(self) -> bool:
        # chess.WHITE = True, chess.BLACK = False. White-at-bottom is the
        # default for both "white" and "random" selections.
        return chess.BLACK if self.coordinator.player_color == "black" else chess.WHITE

    def _current_classification(self) -> str | None:
        """Last-move classification (e.g. 'blunder'), or None if unset."""
        return (self.coordinator.data or {}).get("last_move_classification")

    def _training_wheels_on(self) -> bool:
        """Read the training-wheels input_boolean state.

        Defaults to False if the helper doesn't exist (older installs that
        haven't created the input_boolean yet). Always-fail-closed: if there
        is any doubt, don't show the glyph overlay.
        """
        try:
            st = self.hass.states.get(_TRAINING_WHEELS_ENTITY)
            return st is not None and st.state == "on"
        except Exception:
            return False

    # ── HA hooks ─────────────────────────────────────────────────────────────

    def _handle_coordinator_update(self) -> None:
        """Bump image_last_updated only when the rendered output would change."""
        fen = self._current_fen()
        move = self._current_last_move()
        orient = self._current_orientation()
        # In training-wheels mode the board overlays the classification
        # glyph, so a classification or toggle change also invalidates the
        # cached render.
        glyph_state = (move, self._current_classification(), self._training_wheels_on())
        if (
            fen != self._last_rendered_fen
            or move != self._last_rendered_move
            or orient != self._last_rendered_orientation
            or glyph_state != self._last_rendered_glyph_state
        ):
            self._attr_image_last_updated = datetime.now(timezone.utc)
            self._last_rendered_fen = fen
            self._last_rendered_move = move
            self._last_rendered_orientation = orient
            self._last_rendered_glyph_state = glyph_state
        super()._handle_coordinator_update()

    def image(self) -> bytes | None:
        """Render the live FEN as SVG bytes.

        Sync (not async) because it's pure CPU: parse FEN, build SVG string.
        Called by HA when the frontend re-fetches (after image_last_updated bumps).
        """
        fen = self._current_fen()
        move_uci = self._current_last_move()
        orient = self._current_orientation()

        try:
            board = chess.BaseBoard(fen)
        except ValueError:
            _LOGGER.warning(
                "Invalid board FEN %r — falling back to starting position", fen
            )
            board = chess.BaseBoard(STARTING_FEN)

        lastmove: chess.Move | None = None
        if move_uci:
            try:
                lastmove = chess.Move.from_uci(move_uci)
            except (ValueError, chess.InvalidMoveError):
                lastmove = None

        svg = chess.svg.board(
            board=board,
            orientation=orient,
            lastmove=lastmove,
            size=RENDER_SIZE,
            coordinates=True,
        )

        # Optional move-quality glyph overlay (training-wheels mode only).
        if lastmove is not None and self._training_wheels_on():
            classification = self._current_classification()
            if classification and classification not in _SUPPRESSED_GLYPH_CLASSES:
                svg = self._overlay_classification_glyph(
                    svg, lastmove, classification, orient,
                )

        return svg.encode("utf-8")

    # ── Glyph overlay (training-wheels mode) ─────────────────────────────────

    @staticmethod
    def _square_center_px(
        square: int, orientation: bool
    ) -> tuple[float, float]:
        """Pixel center of a chess square in the rendered SVG coordinate system.

        Mirrors python-chess's chess.svg.board() geometry:
          - Margin of `_SVG_MARGIN` on each side (for coordinates).
          - Each square is `_SVG_SQUARE` pixels wide.
          - When orientation is WHITE: a1 is bottom-left, h8 top-right.
          - When orientation is BLACK: a1 is top-right, h8 bottom-left.
        """
        file_idx = chess.square_file(square)  # 0=a, 7=h
        rank_idx = chess.square_rank(square)  # 0=rank 1, 7=rank 8

        if orientation == chess.WHITE:
            col = file_idx
            row_from_top = 7 - rank_idx  # rank 8 at top
        else:
            col = 7 - file_idx
            row_from_top = rank_idx       # rank 1 at top when flipped

        cx = _SVG_MARGIN + col * _SVG_SQUARE + _SVG_SQUARE / 2.0
        cy = _SVG_MARGIN + row_from_top * _SVG_SQUARE + _SVG_SQUARE / 2.0
        return (cx, cy)

    def _overlay_classification_glyph(
        self,
        svg: str,
        lastmove: chess.Move,
        classification: str,
        orientation: bool,
    ) -> str:
        """Inject a glyph (!!, !, ?!, ?, ??) on the destination square.

        Renders the glyph in the destination square's UPPER-RIGHT corner so
        it doesn't fully cover the piece. Uses the classification's color
        from CLASSIFICATION_DISPLAY for visual consistency with the move
        history panel. Has a white stroke around the glyph for legibility
        against any square background.
        """
        color, glyph = classification_color_glyph(classification)
        if not glyph:
            return svg

        cx, cy = self._square_center_px(lastmove.to_square, orientation)
        # Offset into the upper-right corner of the square so the glyph
        # doesn't sit on top of the piece icon. Square is _SVG_SQUARE wide;
        # we nudge ~30% right and 30% up from the center.
        gx = cx + _SVG_SQUARE * 0.30
        gy = cy - _SVG_SQUARE * 0.30
        # Font size: a bit smaller than half-square so it reads as a badge.
        font_px = int(_SVG_SQUARE * 0.55)

        # Build the SVG text element. paint-order=stroke draws the white
        # outline behind the fill — keeps the glyph readable on both light
        # and dark squares without compositing tricks.
        # html-escape the glyph in case future glyphs include `<` or `&`.
        glyph_safe = _html_escape(glyph)
        overlay = (
            f'<text x="{gx:.1f}" y="{gy:.1f}" '
            f'text-anchor="middle" dominant-baseline="central" '
            f'font-family="sans-serif" font-weight="900" '
            f'font-size="{font_px}" '
            f'fill="{color}" stroke="#ffffff" stroke-width="2" '
            f'paint-order="stroke" '
            f'pointer-events="none">{glyph_safe}</text>'
        )

        # Inject before the closing </svg>. chess.svg.board() returns a
        # well-formed document with exactly one </svg>; find that and
        # insert. Falling back to appending is safe but produces invalid
        # SVG, so prefer the in-place insertion.
        closing = "</svg>"
        idx = svg.rfind(closing)
        if idx == -1:
            _LOGGER.debug("Glyph overlay: no </svg> found, appending instead.")
            return svg + overlay
        return svg[:idx] + overlay + svg[idx:]

"""Gap-filler tests for the last uncovered branches.

Targets the diagnostics Stockfish-introspection block and the AI-vs-AI loop's
reconnect/re-drive tail + cancellation/error handlers — the residual branches
not reached by the area-focused suites.
"""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import chess
import pytest

from custom_components.phantom_chess.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .ble_mock import FakeBleakClient, make_coordinator


def _run(coro):
    return asyncio.run(coro)


def _patch_integration_version(mock):
    """Patch homeassistant.loader.async_get_integration if it exists.

    In the minimal matrix-tests env ``homeassistant`` is a bare stub with no
    ``loader`` submodule, so ``patch(...)`` raises at resolve time. There the
    diagnostics module's own ``except`` already yields version "unknown", so
    a nullcontext preserves the assertions in both environments.
    """
    try:
        import homeassistant.loader  # noqa: F401
    except Exception:
        return nullcontext()
    return patch("homeassistant.loader.async_get_integration", mock)


# ── diagnostics: Stockfish introspection + version-lookup failure ────────────


def _entry_with_coord(coord):
    entry = MagicMock()
    entry.entry_id = "01ENTRY"
    entry.title = "Phantom"
    entry.data = {
        "ble_address": "C8:C9:A3:F2:7C:0A",
        "lichess_token": "tok",
        "lichess_username": "TestUser",
    }
    entry.options = {}
    entry.runtime_data = coord
    return entry


def test_diagnostics_stockfish_block_populated():
    sf = MagicMock()
    sf.binary_path = "/config/phantom_chess/bin/stockfish"
    sf._available = True
    sf._engine = object()
    sf._unsupported_arch_warned = False
    sf.bin_dir = "/config/phantom_chess/bin"
    client = MagicMock()
    client._stockfish = sf
    coord = MagicMock()
    coord.is_ble_connected = True
    coord.data = {"firmware_version": "0.3.0"}
    coord._game_id = None
    coord._analysis_client = client
    entry = _entry_with_coord(coord)
    with patch("os.path.exists", return_value=True), _patch_integration_version(
        AsyncMock(return_value=MagicMock(version="0.4.0b3")),
    ):
        out = _run(async_get_config_entry_diagnostics(hass=MagicMock(), entry=entry))
    assert out["stockfish"]["client_initialized"] is True
    assert out["stockfish"]["available"] is True
    assert out["stockfish"]["engine_running"] is True


def test_diagnostics_stockfish_introspection_error_is_swallowed():
    sf = MagicMock()
    sf.binary_path = "/sentinel/stockfish"
    client = MagicMock()
    client._stockfish = sf
    coord = MagicMock()
    coord.is_ble_connected = False
    coord.data = {}
    coord._game_id = None
    coord._analysis_client = client
    entry = _entry_with_coord(coord)

    real_exists = __import__("os").path.exists

    def _exists(p):
        if p == "/sentinel/stockfish":
            raise RuntimeError("introspection boom")
        return real_exists(p)

    with patch("os.path.exists", side_effect=_exists), _patch_integration_version(
        AsyncMock(return_value=MagicMock(version="0.4.0b3")),
    ):
        out = _run(async_get_config_entry_diagnostics(hass=MagicMock(), entry=entry))
    assert out["stockfish"]["client_initialized"] is True
    assert "introspect_error" in out["stockfish"]


def test_diagnostics_version_lookup_failure_falls_back_to_unknown():
    entry = _entry_with_coord(None)
    # In the minimal env the loader is unimportable, so the lookup fails
    # naturally; in the HA env the RuntimeError side effect forces it.
    with _patch_integration_version(
        AsyncMock(side_effect=RuntimeError("no integration")),
    ):
        out = _run(async_get_config_entry_diagnostics(hass=MagicMock(), entry=entry))
    assert out["integration"]["version"] == "unknown"


# ── AI-vs-AI loop tail ──────────────────────────────────────────────────────


def _ai_coord():
    coord = make_coordinator(client=FakeBleakClient())
    coord._ai_vs_ai_active = True
    coord._ai_vs_ai_move_delay = 0  # no real waiting
    coord._our_color = chess.WHITE
    coord._board = chess.Board()
    coord.hass.async_create_task = lambda coro, *a, **k: coro.close()
    coord._record_and_analyze_local_move = MagicMock()
    return coord


async def test_ai_vs_ai_loop_cancelled_clears_flags():
    coord = _ai_coord()
    coord._get_ai_move = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await coord._ai_vs_ai_loop()
    assert coord._ai_vs_ai_active is False
    assert coord._local_game_active is False


async def test_ai_vs_ai_loop_unexpected_error_clears_flags():
    coord = _ai_coord()
    coord._get_ai_move = AsyncMock(side_effect=RuntimeError("boom"))
    await coord._ai_vs_ai_loop()  # swallowed by the outer except Exception
    assert coord._ai_vs_ai_active is False
    assert coord._state["local_game_active"] is False


async def test_ai_vs_ai_loop_redrive_after_reconnect_timeout_continues():
    coord = _ai_coord()
    # one move, then None to end the loop
    coord._get_ai_move = AsyncMock(side_effect=["e2e4", None])
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=Exception("ble drop"))
    coord._ai_vs_ai_await_reconnect = AsyncMock(return_value=True)
    coord._phantom_execute_position = AsyncMock(return_value=False)  # re-drive times out
    await coord._ai_vs_ai_loop()
    coord._phantom_execute_position.assert_awaited()
    assert coord._ai_vs_ai_active is False


async def test_ai_vs_ai_loop_redrive_after_reconnect_fails_breaks():
    coord = _ai_coord()
    coord._get_ai_move = AsyncMock(return_value="e2e4")
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=Exception("ble drop"))
    coord._ai_vs_ai_await_reconnect = AsyncMock(return_value=True)
    coord._phantom_execute_position = AsyncMock(side_effect=Exception("re-drive fail"))
    await coord._ai_vs_ai_loop()
    assert coord._ai_vs_ai_active is False


async def test_ai_vs_ai_loop_no_reconnect_breaks():
    coord = _ai_coord()
    coord._get_ai_move = AsyncMock(return_value="e2e4")
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=Exception("ble drop"))
    coord._ai_vs_ai_await_reconnect = AsyncMock(return_value=False)
    await coord._ai_vs_ai_loop()
    assert coord._ai_vs_ai_active is False


async def test_ai_vs_ai_loop_two_step_capture_settle():
    coord = _ai_coord()
    # a position where e4xd5 is a capture (two-step move → longer settle)
    coord._board = chess.Board(
        "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    )
    coord._get_ai_move = AsyncMock(side_effect=["e4d5", None])
    coord.async_phantom_apply_ai_move = AsyncMock()  # no-op (board unchanged)
    await coord._ai_vs_ai_loop()
    coord.async_phantom_apply_ai_move.assert_awaited()


# ── matrix.py residual branches ─────────────────────────────────────────────


def test_build_matrix_from_fen_rank_overflow():
    from custom_components.phantom_chess.matrix import build_matrix_from_fen
    # "8P" overflows: 8 empties fill the rank, then a piece exceeds width 8.
    with pytest.raises(ValueError):
        build_matrix_from_fen("8P/8/8/8/8/8/8/8")


def test_build_matrix_from_fen_rank_wrong_width():
    from custom_components.phantom_chess.matrix import build_matrix_from_fen
    # "7" is only 7 files wide.
    with pytest.raises(ValueError):
        build_matrix_from_fen("7/8/8/8/8/8/8/8")


def test_diff_grid_vs_sensor_bad_length_returns_empty():
    from custom_components.phantom_chess.matrix import diff_grid_vs_sensor
    assert diff_grid_vs_sensor("short", "alsoshort") == []


def test_format_mismatch_instructions_plural_extra():
    from custom_components.phantom_chess.matrix import format_mismatch_instructions
    diffs = [
        {"square": "e4", "type": "extra", "piece": ""},
        {"square": "d5", "type": "extra", "piece": ""},
    ]
    out = format_mismatch_instructions(diffs)
    assert "Extra pieces" in out
    assert "e4" in out and "d5" in out


def test_format_mismatch_instructions_empty_generic():
    from custom_components.phantom_chess.matrix import format_mismatch_instructions
    assert "disagree" in format_mismatch_instructions([])


# ── config_flow.py residual helper branches ─────────────────────────────────


def test_normalize_ble_address_empty_returns_none():
    pytest.importorskip("voluptuous")  # config_flow needs HA deps
    from custom_components.phantom_chess.config_flow import _normalize_ble_address
    assert _normalize_ble_address("") is None
    assert _normalize_ble_address(None) is None


def test_normalize_ble_address_dash_form_and_invalid():
    pytest.importorskip("voluptuous")  # config_flow needs HA deps
    from custom_components.phantom_chess.config_flow import _normalize_ble_address
    assert _normalize_ble_address("c8-c9-a3-f2-7c-0a") == "C8:C9:A3:F2:7C:0A"
    assert _normalize_ble_address("not-a-mac") is None


def test_reauth_and_reconfigure_entry_lookup_no_context():
    pytest.importorskip("voluptuous")  # config_flow needs HA deps
    from custom_components.phantom_chess.config_flow import PhantomChessConfigFlow
    flow = PhantomChessConfigFlow()
    flow.hass = MagicMock()
    flow.context = {}  # no entry_id in context
    assert flow._get_reauth_entry() is None
    assert flow._get_reconfigure_entry() is None


# ── sculpture loop tail (reconnect / cancel) ────────────────────────────────


def _sculpt_coord():
    coord = make_coordinator(client=FakeBleakClient())
    coord._sculpture_active = True
    coord._sculpture_move_delay = 0
    coord._board = chess.Board()
    coord.hass.async_create_task = lambda coro, *a, **k: coro.close()
    coord._record_and_analyze_local_move = MagicMock()
    return coord


async def test_sculpture_loop_cancelled_clears_flags():
    coord = _sculpt_coord()
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await coord._sculpture_loop(["e2e4"])
    assert coord._sculpture_active is False


async def test_sculpture_loop_redrive_fail_breaks():
    coord = _sculpt_coord()
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=Exception("ble drop"))
    coord._ai_vs_ai_await_reconnect = AsyncMock(return_value=True)
    coord._phantom_execute_position = AsyncMock(side_effect=Exception("redrive fail"))
    await coord._sculpture_loop(["e2e4"])
    assert coord._sculpture_active is False


async def test_sculpture_loop_no_reconnect_breaks():
    coord = _sculpt_coord()
    coord.async_phantom_apply_ai_move = AsyncMock(side_effect=Exception("ble drop"))
    coord._ai_vs_ai_await_reconnect = AsyncMock(return_value=False)
    await coord._sculpture_loop(["e2e4"])
    assert coord._sculpture_active is False


async def test_sculpture_loop_analysis_hook_failure_is_swallowed():
    coord = _sculpt_coord()
    coord.async_phantom_apply_ai_move = AsyncMock()  # applies cleanly
    coord._record_and_analyze_local_move = MagicMock(side_effect=RuntimeError("x"))
    # completes the (single-move) game, hitting the analysis-hook except branch
    await coord._sculpture_loop(["e2e4"])
    assert coord._sculpture_active is False


async def test_sculpture_loop_bad_uci_stops():
    coord = _sculpt_coord()
    coord.async_phantom_apply_ai_move = AsyncMock()
    await coord._sculpture_loop(["notauci"])
    coord.async_phantom_apply_ai_move.assert_not_awaited()

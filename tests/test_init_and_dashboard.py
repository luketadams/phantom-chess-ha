"""Tests for __init__.py (setup / unload / services / migration) and the
storage/panel side of dashboard_provision.py.

These require the full Home Assistant test dependency tree (they import
``custom_components.phantom_chess`` which pulls in ``homeassistant.*`` and
``voluptuous``), so the module is skipped in the minimal CI env.

The service-handler tests use a MagicMock ``hass`` and capture the
handlers registered by ``_register_services`` from
``hass.services.async_register.call_args_list``, then invoke each handler
directly with a fake ServiceCall so the coordinator method it forwards to
can be asserted. This exercises every ``handle_*`` closure plus the
``_get_coordinator`` routing logic without a real BLE task.

Run:
    PYTHONPATH=. pytest tests/test_init_and_dashboard.py -q
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# The whole module needs HA installed (imports pull in homeassistant.*).
pytest.importorskip("voluptuous")
pytest.importorskip("pytest_homeassistant_custom_component")


from homeassistant.exceptions import ServiceValidationError  # noqa: E402

import custom_components.phantom_chess as pc  # noqa: E402
from custom_components.phantom_chess.const import (  # noqa: E402
    CONF_BLE_ADDRESS,
    CONF_DEVICE_NAME,
    DOMAIN,
)


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────


def _register_and_collect() -> tuple[MagicMock, dict[str, object]]:
    """Run ``_register_services`` against a fresh MagicMock hass and return
    ``(hass, {service_name: handler})``.

    ``has_service`` returns False so the idempotency guard doesn't short-
    circuit registration.
    """
    hass = MagicMock()
    hass.services.has_service.return_value = False
    pc._register_services(hass)
    handlers: dict[str, object] = {}
    for call in hass.services.async_register.call_args_list:
        # signature: async_register(DOMAIN, name, handler, schema=...)
        _domain, name, handler = call.args[0], call.args[1], call.args[2]
        handlers[name] = handler
    return hass, handlers


def _make_call(data: dict | None = None) -> MagicMock:
    call = MagicMock()
    call.data = data or {}
    return call


def _hass_with_single_coordinator(hass: MagicMock, coordinator: object,
                                  entry_id: str = "entry1") -> None:
    """Wire ``hass.config_entries.async_entries(DOMAIN)`` to return a single
    entry whose runtime_data is ``coordinator``.
    """
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = coordinator
    hass.config_entries.async_entries.return_value = [entry]


# ─────────────────────────────────────────────────────────────────────────
# _register_services + _get_coordinator routing
# ─────────────────────────────────────────────────────────────────────────


def test_register_services_registers_all_expected_services() -> None:
    hass, handlers = _register_and_collect()
    expected = {
        pc.SERVICE_START_GAME,
        pc.SERVICE_START_LOCAL_GAME,
        pc.SERVICE_STOP_LOCAL_GAME,
        pc.SERVICE_START_AI_VS_AI_GAME,
        pc.SERVICE_START_TWO_PLAYER_GAME,
        pc.SERVICE_RESYNC_TWO_PLAYER,
        pc.SERVICE_BACK_TO_MODES,
        pc.SERVICE_RESYNC_DETECTION,
        pc.SERVICE_START_LICHESS_CONFIGURED,
        pc.SERVICE_PLAY_SELECTED_SCULPTURE,
        pc.SERVICE_SEND_MOVE,
        pc.SERVICE_RESIGN,
        pc.SERVICE_TAKEBACK,
        pc.SERVICE_DEBUG_BLE_WRITE,
        pc.SERVICE_DIAGNOSE_GAME_START,
        pc.SERVICE_PHANTOM_START_GAME,
        pc.SERVICE_PHANTOM_APPLY_AI_MOVE,
        pc.SERVICE_MOVE_PIECE,
        pc.SERVICE_START_SCULPTURE,
        pc.SERVICE_RESET_POSITION,
        pc.SERVICE_PLAY_SOUND,
        pc.SERVICE_REQUEST_HINT,
        pc.SERVICE_DISMISS_REVIEW,
        pc.SERVICE_RECONCILE_LICHESS_STATE,
        pc.SERVICE_RESUME_FROM_PHONE,
        pc.SERVICE_EXECUTE_MOVE,
    }
    assert expected <= set(handlers)


def test_register_services_idempotent_when_already_registered() -> None:
    """When the marker service is already present, registration is skipped."""
    hass = MagicMock()
    hass.services.has_service.return_value = True
    pc._register_services(hass)
    hass.services.async_register.assert_not_called()


async def test_get_coordinator_no_boards_raises() -> None:
    """Zero configured entries → ServiceValidationError from any handler."""
    hass, handlers = _register_and_collect()
    hass.config_entries.async_entries.return_value = []
    with pytest.raises(ServiceValidationError):
        await handlers[pc.SERVICE_RESIGN](_make_call())


async def test_get_coordinator_unknown_entry_id_raises() -> None:
    hass, handlers = _register_and_collect()
    coordinator = MagicMock()
    _hass_with_single_coordinator(hass, coordinator, entry_id="known")
    with pytest.raises(ServiceValidationError):
        await handlers[pc.SERVICE_RESIGN](_make_call({"entry_id": "does_not_exist"}))


async def test_get_coordinator_ambiguous_two_boards_raises() -> None:
    """Two boards + no entry_id → ambiguous → ServiceValidationError."""
    hass, handlers = _register_and_collect()
    e1 = MagicMock(); e1.entry_id = "a"; e1.runtime_data = MagicMock()
    e2 = MagicMock(); e2.entry_id = "b"; e2.runtime_data = MagicMock()
    hass.config_entries.async_entries.return_value = [e1, e2]
    with pytest.raises(ServiceValidationError):
        await handlers[pc.SERVICE_RESIGN](_make_call())


async def test_get_coordinator_two_boards_with_entry_id_routes() -> None:
    """Two boards + explicit entry_id → routes to that board."""
    hass, handlers = _register_and_collect()
    c1 = MagicMock(); c1.async_resign = AsyncMock()
    c2 = MagicMock(); c2.async_resign = AsyncMock()
    e1 = MagicMock(); e1.entry_id = "a"; e1.runtime_data = c1
    e2 = MagicMock(); e2.entry_id = "b"; e2.runtime_data = c2
    hass.config_entries.async_entries.return_value = [e1, e2]
    await handlers[pc.SERVICE_RESIGN](_make_call({"entry_id": "b"}))
    c2.async_resign.assert_awaited_once()
    c1.async_resign.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# Individual service handlers → coordinator method forwarding
# ─────────────────────────────────────────────────────────────────────────


def _coordinator_stub() -> MagicMock:
    """A coordinator MagicMock whose every ``async_*`` attr is an AsyncMock,
    with the state-read properties (ai_level etc.) writable ints.
    """
    c = MagicMock()
    # AsyncMock the coroutine methods the handlers await.
    for name in (
        "async_start_game", "async_send_move", "async_resign", "async_takeback",
        "async_start_local_game", "async_stop_local_game",
        "async_start_two_player_game", "async_resync_two_player",
        "async_start_ai_vs_ai_game", "async_back_to_modes",
        "async_resync_detection", "async_start_lichess_configured",
        "async_play_selected_sculpture", "async_debug_ble_write",
        "async_diagnose_game_start", "async_phantom_start_game",
        "async_phantom_apply_ai_move", "async_move_piece",
        "async_start_sculpture", "async_reset_position", "async_play_sound",
        "async_request_hint", "async_dismiss_review",
        "async_reconcile_lichess_state", "async_resume_from_phone",
        "async_execute_dashboard_move",
    ):
        setattr(c, name, AsyncMock())
    # default-from-state fields for AI-vs-AI
    c.white_ai_level = 2
    c.black_ai_level = 3
    c.ai_vs_ai_move_delay = 1.5
    return c


async def test_handle_start_game_forwards_ai_level_color_and_clock() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_START_GAME](_make_call({
        "ai_level": 5, "color": "white",
        "clock_limit_seconds": 300, "clock_increment_seconds": 3,
    }))
    assert c.ai_level == 5
    assert c.player_color == "white"
    c.async_start_game.assert_awaited_once_with(
        clock_limit_seconds=300, clock_increment_seconds=3
    )


async def test_handle_start_game_no_optionals() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_START_GAME](_make_call({}))
    c.async_start_game.assert_awaited_once_with()


async def test_handle_send_move() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_SEND_MOVE](_make_call({"move": "e2e4"}))
    c.async_send_move.assert_awaited_once_with("e2e4")


async def test_handle_resign() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_RESIGN](_make_call())
    c.async_resign.assert_awaited_once()


async def test_handle_takeback_count() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_TAKEBACK](_make_call({"count": 3}))
    c.async_takeback.assert_awaited_once_with(count=3)


async def test_handle_start_local_game_with_color() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_START_LOCAL_GAME](_make_call({"color": "black"}))
    assert c.player_color == "black"
    c.async_start_local_game.assert_awaited_once()


async def test_handle_stop_local_game() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_STOP_LOCAL_GAME](_make_call())
    c.async_stop_local_game.assert_awaited_once()


async def test_handle_start_two_player_game() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_START_TWO_PLAYER_GAME](_make_call())
    c.async_start_two_player_game.assert_awaited_once()


async def test_handle_resync_two_player() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_RESYNC_TWO_PLAYER](_make_call())
    c.async_resync_two_player.assert_awaited_once()


async def test_handle_start_ai_vs_ai_defaults_from_state() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_START_AI_VS_AI_GAME](_make_call({}))
    c.async_start_ai_vs_ai_game.assert_awaited_once_with(
        white_ai_level=2, black_ai_level=3, move_delay_seconds=1.5
    )


async def test_handle_start_ai_vs_ai_explicit_args() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_START_AI_VS_AI_GAME](_make_call({
        "white_ai_level": 6, "black_ai_level": 7, "move_delay_seconds": 0.5,
    }))
    c.async_start_ai_vs_ai_game.assert_awaited_once_with(
        white_ai_level=6, black_ai_level=7, move_delay_seconds=0.5
    )


async def test_handle_back_to_modes() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_BACK_TO_MODES](_make_call())
    c.async_back_to_modes.assert_awaited_once()


async def test_handle_resync_detection() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_RESYNC_DETECTION](_make_call())
    c.async_resync_detection.assert_awaited_once()


async def test_handle_start_lichess_configured() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_START_LICHESS_CONFIGURED](_make_call())
    c.async_start_lichess_configured.assert_awaited_once()


async def test_handle_play_selected_sculpture() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_PLAY_SELECTED_SCULPTURE](_make_call())
    c.async_play_selected_sculpture.assert_awaited_once()


async def test_handle_debug_ble_write() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_DEBUG_BLE_WRITE](
        _make_call({"uuid": "abc", "data": "01"})
    )
    c.async_debug_ble_write.assert_awaited_once_with("abc", "01")


async def test_handle_diagnose_game_start() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_DIAGNOSE_GAME_START](
        _make_call({"experimental": True})
    )
    c.async_diagnose_game_start.assert_awaited_once_with(experimental=True)


async def test_handle_phantom_start_game() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_PHANTOM_START_GAME](_make_call({
        "fen": "somefen", "side": "B", "timeout": 30,
    }))
    c.async_phantom_start_game.assert_awaited_once_with(
        fen="somefen", side="B", wait_for_running_timeout_s=30
    )


async def test_handle_phantom_apply_ai_move() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_PHANTOM_APPLY_AI_MOVE](_make_call({"move": "e7e5"}))
    c.async_phantom_apply_ai_move.assert_awaited_once_with("e7e5")


async def test_handle_move_piece_lowercases_squares() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_MOVE_PIECE](_make_call({
        "from_square": "E2", "to_square": "E4", "capture": True, "piece": "P",
    }))
    c.async_move_piece.assert_awaited_once_with(
        from_square="e2", to_square="e4", capture=True, piece="P"
    )


async def test_handle_start_sculpture() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_START_SCULPTURE](_make_call())
    c.async_start_sculpture.assert_awaited_once()


async def test_handle_reset_position() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_RESET_POSITION](_make_call())
    c.async_reset_position.assert_awaited_once()


async def test_handle_play_sound() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_PLAY_SOUND](_make_call({"sound": "checkmate"}))
    c.async_play_sound.assert_awaited_once_with("checkmate")


async def test_handle_play_sound_default() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_PLAY_SOUND](_make_call())
    c.async_play_sound.assert_awaited_once_with("check")


async def test_handle_request_hint() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_REQUEST_HINT](_make_call())
    c.async_request_hint.assert_awaited_once()


async def test_handle_dismiss_review() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_DISMISS_REVIEW](_make_call())
    c.async_dismiss_review.assert_awaited_once()


async def test_handle_reconcile_lichess_state() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_RECONCILE_LICHESS_STATE](_make_call())
    c.async_reconcile_lichess_state.assert_awaited_once()


async def test_handle_resume_from_phone() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_RESUME_FROM_PHONE](_make_call())
    c.async_resume_from_phone.assert_awaited_once()


async def test_handle_execute_move() -> None:
    hass, handlers = _register_and_collect()
    c = _coordinator_stub()
    _hass_with_single_coordinator(hass, c)
    await handlers[pc.SERVICE_EXECUTE_MOVE](
        _make_call({"move": "e7e8q", "promotion": "q"})
    )
    c.async_execute_dashboard_move.assert_awaited_once_with(
        uci="e7e8q", promotion="q"
    )


# ─────────────────────────────────────────────────────────────────────────
# async_setup
# ─────────────────────────────────────────────────────────────────────────


async def test_async_setup_registers_services_and_static_paths() -> None:
    hass = MagicMock()
    hass.services.has_service.return_value = False
    with patch.object(pc, "_register_static_paths", new=AsyncMock()) as mock_static:
        result = await pc.async_setup(hass, {})
    assert result is True
    mock_static.assert_awaited_once()
    # services registered
    assert hass.services.async_register.call_count > 0


# ─────────────────────────────────────────────────────────────────────────
# async_setup_entry / async_unload_entry
# ─────────────────────────────────────────────────────────────────────────


def _entry_for_setup(options: dict | None = None) -> MagicMock:
    entry = MagicMock()
    entry.data = {CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF"}
    entry.options = options if options is not None else {}
    entry.entry_id = "e1"
    return entry


async def test_async_setup_entry_provisions_dashboard_by_default() -> None:
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = _entry_for_setup(options={})

    coord = MagicMock()
    coord.async_setup = AsyncMock()
    coord._state = {"foo": "bar"}

    with patch.object(pc, "PhantomChessCoordinator", return_value=coord) as mk, \
         patch.object(pc, "async_provision_dashboard", new=AsyncMock()) as prov, \
         patch.object(pc, "async_unprovision_dashboard", new=AsyncMock()) as unprov:
        result = await pc.async_setup_entry(hass, entry)

    assert result is True
    mk.assert_called_once()
    coord.async_setup.assert_awaited_once()
    coord.async_set_updated_data.assert_called_once_with(coord._state)
    assert entry.runtime_data is coord
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, pc.PLATFORMS
    )
    prov.assert_awaited_once_with(hass, entry)
    unprov.assert_not_awaited()
    entry.async_on_unload.assert_called_once()
    entry.add_update_listener.assert_called_once()


async def test_async_setup_entry_unprovisions_when_toggle_off() -> None:
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = _entry_for_setup(options={pc.OPT_AUTO_PROVISION_DASHBOARD: False})

    coord = MagicMock()
    coord.async_setup = AsyncMock()
    coord._state = {}

    with patch.object(pc, "PhantomChessCoordinator", return_value=coord), \
         patch.object(pc, "async_provision_dashboard", new=AsyncMock()) as prov, \
         patch.object(pc, "async_unprovision_dashboard", new=AsyncMock()) as unprov:
        result = await pc.async_setup_entry(hass, entry)

    assert result is True
    prov.assert_not_awaited()
    unprov.assert_awaited_once_with(hass)


async def test_async_setup_entry_swallows_provision_exception() -> None:
    """Provisioning failure must not fail integration setup."""
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = _entry_for_setup(options={})

    coord = MagicMock()
    coord.async_setup = AsyncMock()
    coord._state = {}

    with patch.object(pc, "PhantomChessCoordinator", return_value=coord), \
         patch.object(pc, "async_provision_dashboard",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await pc.async_setup_entry(hass, entry)
    assert result is True


async def test_async_setup_entry_swallows_unprovision_exception() -> None:
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    entry = _entry_for_setup(options={pc.OPT_AUTO_PROVISION_DASHBOARD: False})

    coord = MagicMock()
    coord.async_setup = AsyncMock()
    coord._state = {}

    with patch.object(pc, "PhantomChessCoordinator", return_value=coord), \
         patch.object(pc, "async_unprovision_dashboard",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await pc.async_setup_entry(hass, entry)
    assert result is True


async def test_async_options_updated_reloads_entry() -> None:
    hass = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "e1"
    await pc._async_options_updated(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with("e1")


async def test_async_unload_entry_shuts_down_and_unloads() -> None:
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    coord = MagicMock()
    coord.async_shutdown = AsyncMock()
    entry = MagicMock()
    entry.runtime_data = coord

    result = await pc.async_unload_entry(hass, entry)
    assert result is True
    coord.async_shutdown.assert_awaited_once()
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry, pc.PLATFORMS
    )


async def test_async_unload_entry_returns_platform_result() -> None:
    hass = MagicMock()
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)
    coord = MagicMock()
    coord.async_shutdown = AsyncMock()
    entry = MagicMock()
    entry.runtime_data = coord
    assert await pc.async_unload_entry(hass, entry) is False


# ─────────────────────────────────────────────────────────────────────────
# async_remove_entry
# ─────────────────────────────────────────────────────────────────────────


async def test_async_remove_entry_keeps_dashboard_when_entries_remain() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = [MagicMock()]  # survivor
    entry = MagicMock()
    entry.options = {}
    with patch.object(pc, "async_unprovision_dashboard", new=AsyncMock()) as unprov:
        await pc.async_remove_entry(hass, entry)
    unprov.assert_not_awaited()


async def test_async_remove_entry_unprovisions_when_last() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []  # none left
    entry = MagicMock()
    entry.options = {}
    with patch.object(pc, "async_unprovision_dashboard", new=AsyncMock()) as unprov:
        await pc.async_remove_entry(hass, entry)
    unprov.assert_awaited_once_with(hass)


async def test_async_remove_entry_skips_when_toggle_off() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    entry = MagicMock()
    entry.options = {pc.OPT_AUTO_PROVISION_DASHBOARD: False}
    with patch.object(pc, "async_unprovision_dashboard", new=AsyncMock()) as unprov:
        await pc.async_remove_entry(hass, entry)
    unprov.assert_not_awaited()


async def test_async_remove_entry_swallows_unprovision_exception() -> None:
    hass = MagicMock()
    hass.config_entries.async_entries.return_value = []
    entry = MagicMock()
    entry.options = {}
    with patch.object(pc, "async_unprovision_dashboard",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        # Should not raise.
        await pc.async_remove_entry(hass, entry)


# ─────────────────────────────────────────────────────────────────────────
# _register_static_paths
# ─────────────────────────────────────────────────────────────────────────


async def test_register_static_paths_marker_shortcircuits() -> None:
    hass = MagicMock()
    hass.data = {pc.STATIC_PATH_MARKER: True}
    await pc._register_static_paths(hass)
    # http registration never touched
    hass.http.async_register_static_paths.assert_not_called()


async def test_register_static_paths_registers_when_dir_exists() -> None:
    hass = MagicMock()
    hass.data = {}
    hass.http.async_register_static_paths = AsyncMock()
    with patch("os.path.isdir", return_value=True):
        await pc._register_static_paths(hass)
    hass.http.async_register_static_paths.assert_awaited_once()
    assert hass.data.get(pc.STATIC_PATH_MARKER) is True


async def test_register_static_paths_skips_when_dir_missing() -> None:
    hass = MagicMock()
    hass.data = {}
    hass.http.async_register_static_paths = AsyncMock()
    with patch("os.path.isdir", return_value=False):
        await pc._register_static_paths(hass)
    hass.http.async_register_static_paths.assert_not_awaited()
    assert pc.STATIC_PATH_MARKER not in hass.data


async def test_register_static_paths_generic_exception_logged() -> None:
    """A non-RuntimeError from registration is caught (warning path), leaving
    the marker unset."""
    hass = MagicMock()
    hass.data = {}
    hass.http.async_register_static_paths = AsyncMock(
        side_effect=ValueError("weird")
    )
    with patch("os.path.isdir", return_value=True):
        await pc._register_static_paths(hass)
    assert pc.STATIC_PATH_MARKER not in hass.data


async def test_register_static_paths_runtimeerror_marks_registered() -> None:
    hass = MagicMock()
    hass.data = {}
    hass.http.async_register_static_paths = AsyncMock(
        side_effect=RuntimeError("already registered")
    )
    with patch("os.path.isdir", return_value=True):
        await pc._register_static_paths(hass)
    assert hass.data.get(pc.STATIC_PATH_MARKER) is True


# ─────────────────────────────────────────────────────────────────────────
# async_migrate_entry — migration chain v1 → v4
# ─────────────────────────────────────────────────────────────────────────


def _mig_entry(version: int, data: dict, *, title: str = "Phantom",
               minor_version: int = 1) -> MagicMock:
    entry = MagicMock()
    entry.version = version
    entry.minor_version = minor_version
    entry.data = data
    entry.title = title
    entry.entry_id = "mig1"
    return entry


def _mig_hass_with_update() -> MagicMock:
    """A MagicMock hass whose async_update_entry records the version it was
    told to set, mutating the entry so the migration chain falls through
    the ``if entry.version == N`` blocks correctly.
    """
    hass = MagicMock()

    def _update(entry, **kwargs):
        if "version" in kwargs:
            entry.version = kwargs["version"]
        if "data" in kwargs:
            entry.data = kwargs["data"]
        if "title" in kwargs:
            entry.title = kwargs["title"]

    hass.config_entries.async_update_entry.side_effect = _update
    return hass


async def test_migrate_v1_to_v4_normalizes_address() -> None:
    hass = _mig_hass_with_update()
    entry = _mig_entry(
        1,
        {CONF_BLE_ADDRESS: "c8:c9:a3:f2:7c:0a",
         CONF_DEVICE_NAME: "Phantom c8:c9:a3:f2:7c:0a"},
        title="Phantom c8:c9:a3:f2:7c:0a",
    )
    with patch("custom_components.phantom_chess.er.async_get") as er_get, \
         patch("custom_components.phantom_chess.dr.async_get") as dr_get:
        er_get.return_value = MagicMock(entities={})
        dr_get.return_value = MagicMock(devices={})
        result = await pc.async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 4
    # v1→v2 update call should have canonicalized the address to uppercase
    first_call = hass.config_entries.async_update_entry.call_args_list[0]
    assert first_call.kwargs["unique_id"] == "C8:C9:A3:F2:7C:0A"
    assert first_call.kwargs["data"][CONF_BLE_ADDRESS] == "C8:C9:A3:F2:7C:0A"
    assert first_call.kwargs["data"][CONF_DEVICE_NAME] == "Phantom C8:C9:A3:F2:7C:0A"


async def test_migrate_v1_device_name_phantom_prefix_variant() -> None:
    """v1 device_name of exact form ``Phantom <addr>`` (address NOT a
    substring appearing elsewhere) hits the elif rewrite branch."""
    hass = _mig_hass_with_update()
    entry = _mig_entry(
        1,
        {CONF_BLE_ADDRESS: "c8:c9:a3:f2:7c:0a",
         CONF_DEVICE_NAME: "Phantom c8:c9:a3:f2:7c:0a"},
        title="My Board",  # title does NOT contain the address
    )
    with patch("custom_components.phantom_chess.er.async_get") as er_get, \
         patch("custom_components.phantom_chess.dr.async_get") as dr_get:
        er_get.return_value = MagicMock(entities={})
        dr_get.return_value = MagicMock(devices={})
        result = await pc.async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == 4
    first_call = hass.config_entries.async_update_entry.call_args_list[0]
    # Title unchanged (address wasn't in it), device_name canonicalized.
    assert first_call.kwargs["title"] == "My Board"
    assert first_call.kwargs["data"][CONF_DEVICE_NAME] == "Phantom C8:C9:A3:F2:7C:0A"


async def test_migrate_v1_unparseable_address_still_bumps() -> None:
    hass = _mig_hass_with_update()
    entry = _mig_entry(1, {CONF_BLE_ADDRESS: "not-a-mac"})
    with patch("custom_components.phantom_chess.er.async_get") as er_get, \
         patch("custom_components.phantom_chess.dr.async_get") as dr_get:
        er_get.return_value = MagicMock(entities={})
        dr_get.return_value = MagicMock(devices={})
        result = await pc.async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == 4


async def test_migrate_v2_no_address_skips_consolidation() -> None:
    hass = _mig_hass_with_update()
    entry = _mig_entry(2, {})  # no CONF_BLE_ADDRESS
    with patch("custom_components.phantom_chess.er.async_get") as er_get, \
         patch("custom_components.phantom_chess.dr.async_get") as dr_get:
        er_get.return_value = MagicMock(entities={})
        dr_get.return_value = MagicMock(devices={})
        result = await pc.async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == 4


async def test_migrate_v3_to_v4_only() -> None:
    hass = _mig_hass_with_update()
    entry = _mig_entry(3, {CONF_BLE_ADDRESS: "C8:C9:A3:F2:7C:0A"})
    with patch("custom_components.phantom_chess.er.async_get") as er_get:
        er_get.return_value = MagicMock(entities={})
        result = await pc.async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == 4


async def test_migrate_unsupported_future_version_returns_false() -> None:
    hass = _mig_hass_with_update()
    entry = _mig_entry(99, {CONF_BLE_ADDRESS: "C8:C9:A3:F2:7C:0A"})
    result = await pc.async_migrate_entry(hass, entry)
    assert result is False


# ─────────────────────────────────────────────────────────────────────────
# _consolidate_registries_to_canonical — v2→v3 registry helper
# ─────────────────────────────────────────────────────────────────────────


def _fake_entity(entity_id: str, unique_id: str, config_entry_id: str,
                 device_id: str | None = None) -> MagicMock:
    e = MagicMock()
    e.entity_id = entity_id
    e.unique_id = unique_id
    e.config_entry_id = config_entry_id
    e.device_id = device_id
    return e


def test_consolidate_promotes_noncanonical_entity() -> None:
    """Only non-canonical entity present → its unique_id is rewritten to
    the canonical form (promotion path)."""
    canonical = "C8:C9:A3:F2:7C:0A"
    entry = MagicMock()
    entry.entry_id = "e1"

    e1 = _fake_entity(
        "sensor.phantom_battery",
        "c8:c9:a3:f2:7c:0a_battery",  # lowercase (non-canonical)
        "e1",
    )
    er_inst = MagicMock()
    er_inst.entities = {"1": e1}
    dev_reg = MagicMock()
    dev_reg.devices = {}

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst), \
         patch("custom_components.phantom_chess.dr.async_get", return_value=dev_reg):
        pc._consolidate_registries_to_canonical(
            MagicMock(), entry, canonical, canonical.lower()
        )

    er_inst.async_update_entity.assert_called_once_with(
        "sensor.phantom_battery", new_unique_id="C8:C9:A3:F2:7C:0A_battery"
    )


def test_consolidate_both_forms_keeps_noncanonical_deletes_canonical() -> None:
    """Both canonical + non-canonical entities exist for a suffix → keep the
    non-canonical (rewrite it), delete the canonical duplicate."""
    canonical = "C8:C9:A3:F2:7C:0A"
    entry = MagicMock()
    entry.entry_id = "e1"

    noncanon = _fake_entity(
        "sensor.phantom_battery", "c8:c9:a3:f2:7c:0a_battery", "e1"
    )
    canon = _fake_entity(
        "sensor.phantom_battery_2", "C8:C9:A3:F2:7C:0A_battery", "e1"
    )
    er_inst = MagicMock()
    er_inst.entities = {"n": noncanon, "c": canon}
    dev_reg = MagicMock()
    dev_reg.devices = {}

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst), \
         patch("custom_components.phantom_chess.dr.async_get", return_value=dev_reg):
        pc._consolidate_registries_to_canonical(
            MagicMock(), entry, canonical, canonical.lower()
        )

    er_inst.async_update_entity.assert_called_once_with(
        "sensor.phantom_battery", new_unique_id="C8:C9:A3:F2:7C:0A_battery"
    )
    er_inst.async_remove.assert_called_once_with("sensor.phantom_battery_2")


def test_consolidate_only_canonical_is_noop() -> None:
    """Only canonical entity present → nothing changes."""
    canonical = "C8:C9:A3:F2:7C:0A"
    entry = MagicMock()
    entry.entry_id = "e1"
    canon = _fake_entity(
        "sensor.phantom_battery", "C8:C9:A3:F2:7C:0A_battery", "e1"
    )
    er_inst = MagicMock()
    er_inst.entities = {"c": canon}
    dev_reg = MagicMock()
    dev_reg.devices = {}

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst), \
         patch("custom_components.phantom_chess.dr.async_get", return_value=dev_reg):
        pc._consolidate_registries_to_canonical(
            MagicMock(), entry, canonical, canonical.lower()
        )

    er_inst.async_update_entity.assert_not_called()
    er_inst.async_remove.assert_not_called()


def test_consolidate_promotes_noncanonical_device() -> None:
    """Only non-canonical device present → its identifier is rewritten."""
    canonical = "C8:C9:A3:F2:7C:0A"
    entry = MagicMock()
    entry.entry_id = "e1"

    er_inst = MagicMock()
    er_inst.entities = {}
    dev = MagicMock()
    dev.id = "dev1"
    dev.config_entries = {"e1"}
    dev.identifiers = {(DOMAIN, "c8:c9:a3:f2:7c:0a")}
    dev_reg = MagicMock()
    dev_reg.devices = {"dev1": dev}

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst), \
         patch("custom_components.phantom_chess.dr.async_get", return_value=dev_reg):
        pc._consolidate_registries_to_canonical(
            MagicMock(), entry, canonical, canonical.lower()
        )

    dev_reg.async_update_device.assert_called_once()
    _args, kwargs = dev_reg.async_update_device.call_args
    assert (DOMAIN, canonical) in kwargs["new_identifiers"]


def test_consolidate_both_devices_reassigns_and_removes() -> None:
    """Canonical + non-canonical device → entities reassigned to canonical,
    non-canonical device removed."""
    canonical = "C8:C9:A3:F2:7C:0A"
    entry = MagicMock()
    entry.entry_id = "e1"

    # An entity still attached to the non-canonical device.
    attached = _fake_entity(
        "sensor.phantom_battery", "C8:C9:A3:F2:7C:0A_battery", "e1",
        device_id="devlow",
    )
    er_inst = MagicMock()
    er_inst.entities = {"a": attached}

    dev_canon = MagicMock()
    dev_canon.id = "devcanon"
    dev_canon.config_entries = {"e1"}
    dev_canon.identifiers = {(DOMAIN, "C8:C9:A3:F2:7C:0A")}
    dev_low = MagicMock()
    dev_low.id = "devlow"
    dev_low.config_entries = {"e1"}
    dev_low.identifiers = {(DOMAIN, "c8:c9:a3:f2:7c:0a")}
    dev_reg = MagicMock()
    dev_reg.devices = {"devcanon": dev_canon, "devlow": dev_low}

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst), \
         patch("custom_components.phantom_chess.dr.async_get", return_value=dev_reg):
        pc._consolidate_registries_to_canonical(
            MagicMock(), entry, canonical, canonical.lower()
        )

    # attached entity reassigned to canonical device
    er_inst.async_update_entity.assert_any_call(
        "sensor.phantom_battery", device_id="devcanon"
    )
    dev_reg.async_remove_device.assert_called_once_with("devlow")


def test_consolidate_entity_rewrite_exception_is_swallowed() -> None:
    """async_update_entity raising during promotion is logged, not raised."""
    canonical = "C8:C9:A3:F2:7C:0A"
    entry = MagicMock(); entry.entry_id = "e1"
    e1 = _fake_entity(
        "sensor.phantom_battery", "c8:c9:a3:f2:7c:0a_battery", "e1"
    )
    # A second non-canonical dup so the removal loop also runs (and raises).
    e2 = _fake_entity(
        "sensor.phantom_battery_x", "c8:c9:a3:f2:7c:0a_battery", "e1"
    )
    er_inst = MagicMock()
    er_inst.entities = {"1": e1, "2": e2}
    er_inst.async_update_entity.side_effect = RuntimeError("boom")
    er_inst.async_remove.side_effect = RuntimeError("boom")
    dev_reg = MagicMock(); dev_reg.devices = {}

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst), \
         patch("custom_components.phantom_chess.dr.async_get", return_value=dev_reg):
        # Must not raise despite both entity ops failing.
        pc._consolidate_registries_to_canonical(
            MagicMock(), entry, canonical, canonical.lower()
        )


def test_consolidate_both_forms_delete_exception_swallowed() -> None:
    """In the both-forms branch, a failing async_remove of the canonical dup
    is swallowed (covers the except in the delete loop)."""
    canonical = "C8:C9:A3:F2:7C:0A"
    entry = MagicMock(); entry.entry_id = "e1"
    noncanon = _fake_entity(
        "sensor.phantom_battery", "c8:c9:a3:f2:7c:0a_battery", "e1"
    )
    canon = _fake_entity(
        "sensor.phantom_battery_2", "C8:C9:A3:F2:7C:0A_battery", "e1"
    )
    er_inst = MagicMock()
    er_inst.entities = {"n": noncanon, "c": canon}
    er_inst.async_remove.side_effect = RuntimeError("boom")
    dev_reg = MagicMock(); dev_reg.devices = {}

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst), \
         patch("custom_components.phantom_chess.dr.async_get", return_value=dev_reg):
        pc._consolidate_registries_to_canonical(
            MagicMock(), entry, canonical, canonical.lower()
        )


def test_consolidate_device_rewrite_exception_swallowed() -> None:
    """async_update_device / async_remove_device raising is swallowed."""
    canonical = "C8:C9:A3:F2:7C:0A"
    entry = MagicMock(); entry.entry_id = "e1"
    er_inst = MagicMock(); er_inst.entities = {}
    dev = MagicMock()
    dev.id = "dev1"; dev.config_entries = {"e1"}
    dev.identifiers = {(DOMAIN, "c8:c9:a3:f2:7c:0a")}
    dup = MagicMock()
    dup.id = "dev2"; dup.config_entries = {"e1"}
    dup.identifiers = {(DOMAIN, "c8:c9:a3:f2:7c:0a")}
    dev_reg = MagicMock()
    dev_reg.devices = {"dev1": dev, "dev2": dup}
    dev_reg.async_update_device.side_effect = RuntimeError("boom")
    dev_reg.async_remove_device.side_effect = RuntimeError("boom")

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst), \
         patch("custom_components.phantom_chess.dr.async_get", return_value=dev_reg):
        pc._consolidate_registries_to_canonical(
            MagicMock(), entry, canonical, canonical.lower()
        )


def test_consolidate_both_devices_reassign_exception_swallowed() -> None:
    """In the canonical+noncanonical device branch, a failing entity
    reassignment and a failing device removal are both swallowed."""
    canonical = "C8:C9:A3:F2:7C:0A"
    entry = MagicMock(); entry.entry_id = "e1"
    attached = _fake_entity(
        "sensor.phantom_battery", "C8:C9:A3:F2:7C:0A_battery", "e1",
        device_id="devlow",
    )
    er_inst = MagicMock()
    er_inst.entities = {"a": attached}
    er_inst.async_update_entity.side_effect = RuntimeError("boom")

    dev_canon = MagicMock()
    dev_canon.id = "devcanon"; dev_canon.config_entries = {"e1"}
    dev_canon.identifiers = {(DOMAIN, "C8:C9:A3:F2:7C:0A")}
    dev_low = MagicMock()
    dev_low.id = "devlow"; dev_low.config_entries = {"e1"}
    dev_low.identifiers = {(DOMAIN, "c8:c9:a3:f2:7c:0a")}
    dev_reg = MagicMock()
    dev_reg.devices = {"devcanon": dev_canon, "devlow": dev_low}
    dev_reg.async_remove_device.side_effect = RuntimeError("boom")

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst), \
         patch("custom_components.phantom_chess.dr.async_get", return_value=dev_reg):
        pc._consolidate_registries_to_canonical(
            MagicMock(), entry, canonical, canonical.lower()
        )


def test_consolidate_ignores_entities_without_underscore_uid() -> None:
    """Entities whose unique_id has no underscore, or whose address prefix
    doesn't match, are left untouched (covers the continue branches)."""
    canonical = "C8:C9:A3:F2:7C:0A"
    entry = MagicMock(); entry.entry_id = "e1"
    no_underscore = _fake_entity("sensor.freeform", "freeform", "e1")
    other_addr = _fake_entity("sensor.other", "AA:BB:CC:DD:EE:FF_battery", "e1")
    er_inst = MagicMock()
    er_inst.entities = {"1": no_underscore, "2": other_addr}
    dev_reg = MagicMock(); dev_reg.devices = {}

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst), \
         patch("custom_components.phantom_chess.dr.async_get", return_value=dev_reg):
        pc._consolidate_registries_to_canonical(
            MagicMock(), entry, canonical, canonical.lower()
        )
    er_inst.async_update_entity.assert_not_called()
    er_inst.async_remove.assert_not_called()


def test_rename_collision_suffix_update_exception_swallowed() -> None:
    """A failing async_update_entity during rename is swallowed; count stays 0."""
    entry = MagicMock(); entry.entry_id = "e1"
    e = _fake_entity("sensor.phantom_battery_2", "uid_battery", "e1")
    er_inst = MagicMock()
    er_inst.entities = {"1": e}
    er_inst.async_get.return_value = None
    er_inst.async_update_entity.side_effect = RuntimeError("boom")
    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst):
        renamed = pc._rename_collision_suffix_entity_ids(MagicMock(), entry)
    assert renamed == 0


# ─────────────────────────────────────────────────────────────────────────
# _rename_collision_suffix_entity_ids — v3→v4 helper
# ─────────────────────────────────────────────────────────────────────────


def test_rename_collision_suffix_renames_when_base_free() -> None:
    entry = MagicMock()
    entry.entry_id = "e1"
    e = _fake_entity("sensor.phantom_battery_2", "uid_battery", "e1")
    er_inst = MagicMock()
    er_inst.entities = {"1": e}
    er_inst.async_get.return_value = None  # base is free

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst):
        renamed = pc._rename_collision_suffix_entity_ids(MagicMock(), entry)

    assert renamed == 1
    er_inst.async_update_entity.assert_called_once_with(
        "sensor.phantom_battery_2", new_entity_id="sensor.phantom_battery"
    )


def test_rename_collision_suffix_skips_when_base_taken() -> None:
    entry = MagicMock()
    entry.entry_id = "e1"
    e = _fake_entity("sensor.phantom_battery_2", "uid_battery", "e1")
    er_inst = MagicMock()
    er_inst.entities = {"1": e}
    er_inst.async_get.return_value = MagicMock()  # base already exists

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst):
        renamed = pc._rename_collision_suffix_entity_ids(MagicMock(), entry)

    assert renamed == 0
    er_inst.async_update_entity.assert_not_called()


def test_rename_collision_suffix_ignores_non_suffixed() -> None:
    entry = MagicMock()
    entry.entry_id = "e1"
    e = _fake_entity("sensor.phantom_battery", "uid_battery", "e1")
    er_inst = MagicMock()
    er_inst.entities = {"1": e}

    with patch("custom_components.phantom_chess.er.async_get", return_value=er_inst):
        renamed = pc._rename_collision_suffix_entity_ids(MagicMock(), entry)

    assert renamed == 0
    er_inst.async_update_entity.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# dashboard_provision — storage / panel side (uncovered branches)
# ─────────────────────────────────────────────────────────────────────────

from custom_components.phantom_chess import dashboard_provision as dp  # noqa: E402


async def test_async_provision_dashboard_no_ble_address_returns_early() -> None:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock()
    entry = MagicMock()
    entry.data = {}  # no CONF_BLE_ADDRESS
    entry.entry_id = "e1"
    # Should return before building config; no exception.
    await dp.async_provision_dashboard(hass, entry)


async def test_async_provision_dashboard_full_path() -> None:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock()

    entry = MagicMock()
    entry.data = {CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF"}
    entry.entry_id = "e1"

    # Store handle for the dashboards collection.
    store = MagicMock()
    store.async_save = AsyncMock()

    lovelace_storage = MagicMock()
    lovelace_storage.async_save = AsyncMock()

    lovelace_data = MagicMock()
    lovelace_data.dashboards = {}
    hass.data = {dp.LOVELACE_DATA: lovelace_data}

    with patch.object(dp, "build_dashboard_config",
                      new=AsyncMock(return_value={"views": []})), \
         patch.object(dp.ll_dashboard, "LovelaceStorage",
                      return_value=lovelace_storage), \
         patch.object(dp, "_async_load_dashboards_store",
                      new=AsyncMock(return_value=(store, []))), \
         patch.object(dp.frontend, "async_panel_exists", return_value=False,
                      create=True), \
         patch.object(dp.frontend, "async_register_built_in_panel") as reg_panel, \
         patch.object(dp, "_sync_frontend_deps_issue") as sync_issue:
        await dp.async_provision_dashboard(hass, entry)

    lovelace_storage.async_save.assert_awaited_once_with({"views": []})
    store.async_save.assert_awaited_once()
    # New row appended to the (empty) items list.
    saved = store.async_save.call_args.args[0]
    assert saved["items"][0][dp.CONF_URL_PATH] == dp.DASHBOARD_URL_PATH
    reg_panel.assert_called_once()
    # LovelaceStorage stashed for websocket lookup.
    assert lovelace_data.dashboards[dp.DASHBOARD_URL_PATH] is lovelace_storage
    sync_issue.assert_called_once()


async def test_async_provision_dashboard_updates_existing_row() -> None:
    """An existing row for our url_path is updated in place, not duplicated."""
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock()
    entry = MagicMock()
    entry.data = {CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF"}
    entry.entry_id = "e1"

    store = MagicMock()
    store.async_save = AsyncMock()
    existing = [{dp.CONF_URL_PATH: dp.DASHBOARD_URL_PATH, "stale": True}]

    lovelace_storage = MagicMock()
    lovelace_storage.async_save = AsyncMock()
    hass.data = {}  # no LOVELACE_DATA -> the dashboards-dict block is skipped

    with patch.object(dp, "build_dashboard_config",
                      new=AsyncMock(return_value={"views": []})), \
         patch.object(dp.ll_dashboard, "LovelaceStorage",
                      return_value=lovelace_storage), \
         patch.object(dp, "_async_load_dashboards_store",
                      new=AsyncMock(return_value=(store, existing))), \
         patch.object(dp.frontend, "async_panel_exists", return_value=True,
                      create=True), \
         patch.object(dp, "_sync_frontend_deps_issue"):
        await dp.async_provision_dashboard(hass, entry)

    saved = store.async_save.call_args.args[0]
    # still a single row, merged (stale carried, real fields set)
    assert len(saved["items"]) == 1
    assert saved["items"][0][dp.CONF_TITLE] == dp.DASHBOARD_TITLE


async def test_async_provision_dashboard_render_failure_returns() -> None:
    """A template-render exception is swallowed and provisioning aborts."""
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock()
    entry = MagicMock()
    entry.data = {CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF"}
    entry.entry_id = "e1"

    with patch.object(dp, "build_dashboard_config",
                      new=AsyncMock(side_effect=RuntimeError("bad template"))), \
         patch.object(dp, "_async_load_dashboards_store",
                      new=AsyncMock()) as load_store:
        await dp.async_provision_dashboard(hass, entry)
    # We never reached the storage step.
    load_store.assert_not_awaited()


async def test_async_provision_dashboard_storage_save_failure_returns() -> None:
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock()
    entry = MagicMock()
    entry.data = {CONF_BLE_ADDRESS: "AA:BB:CC:DD:EE:FF"}
    entry.entry_id = "e1"

    lovelace_storage = MagicMock()
    lovelace_storage.async_save = AsyncMock(side_effect=RuntimeError("disk full"))

    with patch.object(dp, "build_dashboard_config",
                      new=AsyncMock(return_value={"views": []})), \
         patch.object(dp.ll_dashboard, "LovelaceStorage",
                      return_value=lovelace_storage), \
         patch.object(dp, "_async_load_dashboards_store",
                      new=AsyncMock()) as load_store:
        await dp.async_provision_dashboard(hass, entry)
    load_store.assert_not_awaited()


def test_dashboard_provision_targets_modern_frontend_panel_api() -> None:
    """dashboard_provision uses the modern frontend panel API.

    ``async_provision_dashboard`` / ``async_unprovision_dashboard`` call
    ``frontend.async_panel_exists`` and pass ``show_in_sidebar=True`` to
    ``async_register_built_in_panel``. Both were ADDED to
    ``homeassistant.components.frontend`` after HA 2026.2 (they're present on
    HA 2026.3+/2026.7 and on the live box — confirmed by the "Provisioned
    Phantom Chess dashboard" log). This raises the *effective* minimum HA for
    dashboard auto-provisioning above the declared 2024.11 floor: on an HA
    older than ~2026.3 the calls would raise, but that's swallowed by the
    ``try/except`` in async_setup_entry/async_remove_entry (the .storage
    dashboard is still written first, so the panel just registers on the next
    restart instead of instantly). This test is version-agnostic: it asserts
    the provisioning code references the modern API, without pinning whether
    the running HA happens to expose it.
    """
    import inspect

    src = inspect.getsource(dp.async_provision_dashboard)
    assert "async_panel_exists" in src
    assert "show_in_sidebar" in src


async def test_async_unprovision_dashboard_full_cleanup() -> None:
    hass = MagicMock()

    lovelace_storage = MagicMock()
    lovelace_storage.async_delete = AsyncMock()
    lovelace_data = MagicMock()
    lovelace_data.dashboards = {dp.DASHBOARD_URL_PATH: lovelace_storage}
    hass.data = {dp.LOVELACE_DATA: lovelace_data}

    store = MagicMock()
    store.async_save = AsyncMock()
    items = [{dp.CONF_URL_PATH: dp.DASHBOARD_URL_PATH}, {dp.CONF_URL_PATH: "other"}]

    with patch.object(dp.frontend, "async_panel_exists", return_value=True,
                      create=True), \
         patch.object(dp.frontend, "async_remove_panel") as rm_panel, \
         patch.object(dp, "_async_load_dashboards_store",
                      new=AsyncMock(return_value=(store, items))):
        await dp.async_unprovision_dashboard(hass)

    rm_panel.assert_called_once_with(hass, dp.DASHBOARD_URL_PATH)
    lovelace_storage.async_delete.assert_awaited_once()
    assert dp.DASHBOARD_URL_PATH not in lovelace_data.dashboards
    # Our row filtered out; the "other" row preserved.
    saved = store.async_save.call_args.args[0]
    assert all(i.get(dp.CONF_URL_PATH) != dp.DASHBOARD_URL_PATH for i in saved["items"])


async def test_async_unprovision_dashboard_no_panel_no_data() -> None:
    """No panel, no LOVELACE_DATA, no matching row → still succeeds and does
    not re-save an unchanged items list."""
    hass = MagicMock()
    hass.data = {}
    store = MagicMock()
    store.async_save = AsyncMock()
    items = [{dp.CONF_URL_PATH: "other"}]

    with patch.object(dp.frontend, "async_panel_exists", return_value=False,
                      create=True), \
         patch.object(dp, "_async_load_dashboards_store",
                      new=AsyncMock(return_value=(store, items))):
        await dp.async_unprovision_dashboard(hass)

    # items unchanged → no save
    store.async_save.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────
# _sync_frontend_deps_issue
# ─────────────────────────────────────────────────────────────────────────


def test_sync_frontend_deps_issue_creates_when_missing() -> None:
    hass = MagicMock()
    with patch.object(dp, "_missing_frontend_deps", return_value=["Mushroom"]), \
         patch.object(dp.ir, "async_create_issue") as create, \
         patch.object(dp.ir, "async_delete_issue") as delete:
        dp._sync_frontend_deps_issue(hass)
    create.assert_called_once()
    delete.assert_not_called()


def test_sync_frontend_deps_issue_clears_when_present() -> None:
    hass = MagicMock()
    with patch.object(dp, "_missing_frontend_deps", return_value=[]), \
         patch.object(dp.ir, "async_create_issue") as create, \
         patch.object(dp.ir, "async_delete_issue") as delete:
        dp._sync_frontend_deps_issue(hass)
    delete.assert_called_once()
    create.assert_not_called()

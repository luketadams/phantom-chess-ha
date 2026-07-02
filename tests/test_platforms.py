"""Tests for the entity-platform modules.

Covers the 7 entity platforms (sensor, binary_sensor, button, switch,
select, number, image): each ``async_setup_entry`` and every entity
class's properties + action coroutines. Entities are instantiated with a
MagicMock coordinator whose ``.data`` dict / attributes are set to drive
the property under test; action coroutines assert they call the right
coordinator method (those are AsyncMock).

Platform modules import homeassistant.* heavily, so this file must run
with Home Assistant installed (i.e. WITHOUT the
``-p no:pytest_homeassistant_custom_component`` flag).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import chess
import pytest

# Platform modules import homeassistant.components.* heavily; the minimal
# matrix-tests env stubs homeassistant.* as a bare module, so importing
# them there fails at collection. Same guard as test_config_flow.py.
pytest.importorskip("pytest_homeassistant_custom_component")

from custom_components.phantom_chess import (  # noqa: E402
    binary_sensor as bs_mod,
    button as button_mod,
    image as image_mod,
    number as number_mod,
    select as select_mod,
    sensor as sensor_mod,
    switch as switch_mod,
)
from custom_components.phantom_chess.const import (
    BOARD_IDLE_THRESHOLD_SECONDS,
    DEFAULT_SCULPTURE_GAME,
    DEFAULT_SETUP_MODE,
    SCULPTURE_GAMES,
    SETUP_MODE_OPTIONS,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"
NAME = "Phantom Chess Board"


# ─── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def coord() -> MagicMock:
    """A MagicMock coordinator with the attributes the entities read.

    ``super().available`` on CoordinatorEntity reads
    ``coordinator.last_update_success``; set it True so the base
    availability gate passes and ``is_ble_connected`` drives the result.
    """
    c = MagicMock()
    c.last_update_success = True
    c.is_ble_connected = True
    c.data = {}
    # scalar config attributes read by selects/numbers
    c.ai_level = 4
    c.player_color = "white"
    c.setup_mode = None
    c.selected_sculpture = None
    c.mechanism_speed = 3
    c.sound_level = 10
    c.lichess_clock_minutes = 30
    c.lichess_clock_increment = 0
    c.white_ai_level = 3
    c.black_ai_level = 3
    c.ai_vs_ai_move_delay = 1.0
    c.paused = False
    c.training_wheels = False
    c.voice_announcements = True
    # async coordinator methods invoked by action coroutines
    c.async_set_pause = AsyncMock()
    c.async_set_mechanism_speed = AsyncMock()
    c.async_set_sound_level = AsyncMock()
    c.async_phantom_start_game = AsyncMock()
    c._phantom_send_movement_verify = AsyncMock()
    c.async_set_updated_data = MagicMock()
    c._build_phantom_matrix_from_fen = MagicMock(return_value=["."] * 100)
    c._state = {}
    c._board = chess.Board()
    c._our_color = None
    return c


@pytest.fixture
def entry() -> MagicMock:
    e = MagicMock()
    e.data = {"ble_address": ADDRESS, "device_name": NAME}
    return e


def _added(entities: MagicMock):
    """Return the entity list from a captured async_add_entities MagicMock."""
    assert entities.called
    return list(entities.call_args[0][0])


# ─── async_setup_entry for every platform ───────────────────────────────


@pytest.mark.parametrize(
    "module,expected_count",
    [
        (sensor_mod, 28),
        (bs_mod, 5),
        (button_mod, 2),
        (switch_mod, 3),
        (select_mod, 4),
        (number_mod, 7),
        (image_mod, 1),
    ],
)
async def test_async_setup_entry(module, expected_count, coord, entry):
    entry.runtime_data = coord
    add = MagicMock()
    await module.async_setup_entry(MagicMock(), entry, add)
    ents = _added(add)
    assert len(ents) == expected_count
    # unique_ids all present and unique
    uids = [e._attr_unique_id for e in ents]
    assert len(set(uids)) == len(uids)
    assert all(u.startswith(ADDRESS) for u in uids)


async def test_setup_entry_default_device_name(coord):
    """entry.data without device_name falls back to the default name."""
    e = MagicMock()
    e.data = {"ble_address": ADDRESS}
    e.runtime_data = coord
    add = MagicMock()
    await sensor_mod.async_setup_entry(MagicMock(), e, add)
    ents = _added(add)
    assert ents[0]._attr_device_info["name"] == "Phantom Chess Board"


# ─── sensor.py ──────────────────────────────────────────────────────────


def _sensor(cls, coord, entry):
    return cls(coord, entry, ADDRESS, NAME)


def test_sensor_native_values(coord, entry):
    coord.data = {
        "battery_percent": 88,
        "lichess_game_id": "abc123",
        "live_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR",
        "piece_count": 32,
        "firmware_mode": "Running",
        "matrix_status": "Clean",
        "firmware_last_move": "K e1-e2",
        "opening_name": "Sicilian Defense",
        "opening_eco": "B90",
        "lichess_white_name": "Magnus",
        "lichess_black_name": "Hikaru",
        "lichess_white_clock": 574,
        "lichess_black_clock": 121,
        "eval_cp": 35,
        "eval_mate": None,
        "eval_source": "lichess-cloud",
        "eval_depth": 22,
        "best_move_san": "Nf3",
        "last_move_classification": "best",
        "last_move_cpl": 0,
        "last_move_motif": "fork",
        "threat_san": "Qh5",
        "move_history_moves": [{"san": "e4"}, {"san": "e5"}],
        "last_game_result": "1-0",
        "last_game_accuracy_white": 92.4,
        "last_game_accuracy_black": 88.1,
        "last_game_top_mistakes": [{"ply": 12}],
    }
    checks = {
        sensor_mod.PhantomBatterySensor: 88,
        sensor_mod.PhantomLichessIdSensor: "abc123",
        sensor_mod.PhantomLivePositionSensor: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR",
        sensor_mod.PhantomPieceCountSensor: 32,
        sensor_mod.PhantomFirmwareModeSensor: "Running",
        sensor_mod.PhantomMatrixStatusSensor: "Clean",
        sensor_mod.PhantomFirmwareLastMoveSensor: "K e1-e2",
        sensor_mod.PhantomOpeningNameSensor: "Sicilian Defense",
        sensor_mod.PhantomLichessWhiteNameSensor: "Magnus",
        sensor_mod.PhantomLichessBlackNameSensor: "Hikaru",
        sensor_mod.PhantomLichessWhiteClockSensor: 574,
        sensor_mod.PhantomLichessBlackClockSensor: 121,
        sensor_mod.PhantomLichessWhiteClockDisplaySensor: "9:34",
        sensor_mod.PhantomLichessBlackClockDisplaySensor: "2:01",
        sensor_mod.PhantomEvalCpSensor: 35,
        sensor_mod.PhantomEvalMateSensor: None,
        sensor_mod.PhantomEvalSourceSensor: "lichess-cloud",
        sensor_mod.PhantomEvalDepthSensor: 22,
        sensor_mod.PhantomBestMoveSanSensor: "Nf3",
        sensor_mod.PhantomLastMoveClassificationSensor: "best",
        sensor_mod.PhantomLastMoveCplSensor: 0,
        sensor_mod.PhantomLastMoveMotifSensor: "fork",
        sensor_mod.PhantomThreatSanSensor: "Qh5",
        sensor_mod.PhantomMoveHistorySensor: 2,
        sensor_mod.PhantomLastGameResultSensor: "1-0",
        sensor_mod.PhantomLastGameAccuracyWhiteSensor: 92.4,
        sensor_mod.PhantomLastGameAccuracyBlackSensor: 88.1,
        sensor_mod.PhantomLastGameReviewSensor: 1,
    }
    for cls, expected in checks.items():
        assert _sensor(cls, coord, entry).native_value == expected


def test_sensor_native_values_empty_data(coord, entry):
    """None coordinator.data -> all None / zero-length defaults."""
    coord.data = None
    assert _sensor(sensor_mod.PhantomBatterySensor, coord, entry).native_value is None
    assert _sensor(sensor_mod.PhantomMoveHistorySensor, coord, entry).native_value == 0
    assert _sensor(sensor_mod.PhantomLastGameReviewSensor, coord, entry).native_value == 0
    disp = _sensor(sensor_mod.PhantomLichessWhiteClockDisplaySensor, coord, entry)
    assert disp.native_value is None


def test_clock_display_helper():
    f = sensor_mod._clock_display
    assert f(None) is None
    assert f(0) == "0:00"
    assert f(59) == "0:59"
    assert f(60) == "1:00"
    assert f(574) == "9:34"
    assert f(-5) == "0:00"  # negative clamps to zero
    assert f("not a number") is None


def test_sensor_extra_state_attributes(coord, entry):
    coord.data = {
        "piece_grid": ["."] * 100,
        "sensor_bitmap": [0] * 100,
        "matrix_raw": "raw",
        "matrix_last_updated": "t1",
        "matrix_mismatches": [],
        "last_move": "e2e4",
        "lichess_active": True,
        "local_game_active": False,
        "game_status": "started",
        "firmware_mode_last_updated": "t2",
        "matrix_status_message": "ok",
        "firmware_last_move_updated": "t3",
        "opening_eco": "B90",
        "move_history_moves": [{"san": "e4"}],
        "last_game_top_mistakes": [{"ply": 4}],
    }
    live = _sensor(sensor_mod.PhantomLivePositionSensor, coord, entry)
    attrs = live.extra_state_attributes
    assert attrs["last_move"] == "e2e4"
    assert attrs["lichess_active"] is True
    assert attrs["our_color"] == "white"  # from player_color, _our_color None
    assert attrs["side_to_move"] == "white"  # fresh board, white to move

    assert _sensor(sensor_mod.PhantomFirmwareModeSensor, coord, entry).extra_state_attributes == {
        "last_updated": "t2"
    }
    assert _sensor(sensor_mod.PhantomMatrixStatusSensor, coord, entry).extra_state_attributes == {
        "status_message": "ok"
    }
    assert _sensor(sensor_mod.PhantomFirmwareLastMoveSensor, coord, entry).extra_state_attributes == {
        "last_updated": "t3"
    }
    assert _sensor(sensor_mod.PhantomOpeningNameSensor, coord, entry).extra_state_attributes == {
        "eco": "B90"
    }
    assert _sensor(sensor_mod.PhantomMoveHistorySensor, coord, entry).extra_state_attributes == {
        "moves": [{"san": "e4"}]
    }
    assert _sensor(sensor_mod.PhantomLastGameReviewSensor, coord, entry).extra_state_attributes == {
        "top_mistakes": [{"ply": 4}]
    }


def test_live_position_our_color_from_resolved(coord, entry):
    """When _our_color is set it wins over player_color pref."""
    coord._our_color = chess.BLACK
    coord.player_color = "white"
    coord.data = {}
    live = _sensor(sensor_mod.PhantomLivePositionSensor, coord, entry)
    assert live.extra_state_attributes["our_color"] == "black"


def test_live_position_random_pref_defaults_white(coord, entry):
    coord._our_color = None
    coord.player_color = "random"
    coord.data = {}
    live = _sensor(sensor_mod.PhantomLivePositionSensor, coord, entry)
    assert live.extra_state_attributes["our_color"] == "white"


def test_live_position_board_missing_side_to_move(coord, entry):
    """_board raising AttributeError -> side_to_move None."""
    del coord._board
    coord._our_color = None
    coord.data = {}
    live = _sensor(sensor_mod.PhantomLivePositionSensor, coord, entry)
    attrs = live.extra_state_attributes
    assert attrs["side_to_move"] is None


def test_live_position_our_color_attribute_error(entry):
    """_our_color access raising AttributeError hits the fallback branch."""

    class RaisingCoord:
        last_update_success = True
        is_ble_connected = True
        data = {}
        player_color = "black"

        def __getattr__(self, name):
            # _our_color and _board both raise -> exercise except branches
            raise AttributeError(name)

    c = RaisingCoord()
    live = _sensor(sensor_mod.PhantomLivePositionSensor, c, entry)
    attrs = live.extra_state_attributes
    assert attrs["our_color"] == "black"  # from getattr fallback -> player_color
    assert attrs["side_to_move"] is None


def test_sensor_available_gate(coord, entry):
    s = _sensor(sensor_mod.PhantomBatterySensor, coord, entry)
    coord.is_ble_connected = True
    assert s.available is True
    coord.is_ble_connected = False
    assert s.available is False


# ─── binary_sensor.py ───────────────────────────────────────────────────


def test_connected_sensor(coord, entry):
    s = bs_mod.PhantomConnectedSensor(coord, entry, ADDRESS, NAME)
    coord.is_ble_connected = True
    assert s.is_on is True
    # connected sensor stays available even when disconnected
    coord.is_ble_connected = False
    assert s.is_on is False
    assert s.available is True


def test_binary_base_available_gate(coord, entry):
    """Base binary sensors (not the connected one) gate on BLE connection."""
    s = bs_mod.PhantomLichessActiveSensor(coord, entry, ADDRESS, NAME)
    coord.is_ble_connected = True
    assert s.available is True
    coord.is_ble_connected = False
    assert s.available is False


def test_lichess_active_and_review_ready(coord, entry):
    coord.data = {"lichess_active": True, "lichess_review_ready": True}
    assert bs_mod.PhantomLichessActiveSensor(coord, entry, ADDRESS, NAME).is_on is True
    assert bs_mod.PhantomLichessReviewReadySensor(coord, entry, ADDRESS, NAME).is_on is True
    coord.data = {}
    assert bs_mod.PhantomLichessActiveSensor(coord, entry, ADDRESS, NAME).is_on is False
    assert bs_mod.PhantomLichessReviewReadySensor(coord, entry, ADDRESS, NAME).is_on is False


def test_learning_view_active(coord, entry):
    s = bs_mod.PhantomLearningViewActiveSensor(coord, entry, ADDRESS, NAME)
    coord.data = {}
    assert s.is_on is False
    coord.data = {"two_player_active": True}
    assert s.is_on is True
    coord.data = {"local_game_active": True}
    assert s.is_on is True
    coord.data = {"lichess_active": True}
    assert s.is_on is True


def test_board_idle_no_timestamp(coord, entry):
    s = bs_mod.PhantomBoardIdleSensor(coord, entry, ADDRESS, NAME)
    coord.data = {}
    assert s.is_on is True  # never seen a move -> idle


def test_board_idle_bad_timestamp(coord, entry):
    s = bs_mod.PhantomBoardIdleSensor(coord, entry, ADDRESS, NAME)
    coord.data = {"firmware_last_move_updated": "not-a-date"}
    assert s.is_on is True


def test_board_idle_recent_move_not_idle(coord, entry):
    s = bs_mod.PhantomBoardIdleSensor(coord, entry, ADDRESS, NAME)
    recent = datetime.now(timezone.utc).isoformat()
    coord.data = {"firmware_last_move_updated": recent}
    assert s.is_on is False


def test_board_idle_old_move_idle(coord, entry):
    s = bs_mod.PhantomBoardIdleSensor(coord, entry, ADDRESS, NAME)
    old = (
        datetime.now(timezone.utc)
        - timedelta(seconds=BOARD_IDLE_THRESHOLD_SECONDS + 30)
    ).isoformat()
    coord.data = {"firmware_last_move_updated": old}
    assert s.is_on is True


def test_board_idle_naive_timestamp(coord, entry):
    """A naive (tz-less) timestamp is treated as UTC."""
    s = bs_mod.PhantomBoardIdleSensor(coord, entry, ADDRESS, NAME)
    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    coord.data = {"firmware_last_move_updated": naive}
    assert s.is_on is False


async def test_board_idle_lifecycle(coord, entry, monkeypatch):
    """async_added_to_hass registers an interval; will_remove cancels it."""
    s = bs_mod.PhantomBoardIdleSensor(coord, entry, ADDRESS, NAME)
    s.hass = MagicMock()
    unsub = MagicMock()
    monkeypatch.setattr(bs_mod, "async_track_time_interval", MagicMock(return_value=unsub))
    # CoordinatorEntity.async_added_to_hass touches coordinator listeners;
    # stub the parent chain by patching the base methods to no-ops.
    monkeypatch.setattr(
        bs_mod.CoordinatorEntity, "async_added_to_hass", AsyncMock()
    )
    monkeypatch.setattr(
        bs_mod.CoordinatorEntity, "async_will_remove_from_hass", AsyncMock()
    )
    await s.async_added_to_hass()
    assert s._unsub_interval is unsub
    # the periodic callback pushes state
    s.async_write_ha_state = MagicMock()
    s._async_periodic_update(None)
    s.async_write_ha_state.assert_called_once()
    await s.async_will_remove_from_hass()
    unsub.assert_called_once()
    assert s._unsub_interval is None


async def test_board_idle_will_remove_no_interval(coord, entry, monkeypatch):
    s = bs_mod.PhantomBoardIdleSensor(coord, entry, ADDRESS, NAME)
    monkeypatch.setattr(
        bs_mod.CoordinatorEntity, "async_will_remove_from_hass", AsyncMock()
    )
    s._unsub_interval = None
    await s.async_will_remove_from_hass()  # no crash


# ─── button.py ──────────────────────────────────────────────────────────


async def test_start_game_button_press(coord, entry):
    b = button_mod.PhantomStartGameButton(coord, entry, ADDRESS, NAME)
    await b.async_press()
    coord.async_phantom_start_game.assert_awaited_once()
    kwargs = coord.async_phantom_start_game.await_args.kwargs
    assert kwargs["side"] == "W"
    assert kwargs["fen"] == chess.STARTING_FEN
    # seeded live state
    coord.async_set_updated_data.assert_called_once()
    assert coord._state["last_move"] is None


async def test_start_game_button_press_seed_failure(coord, entry):
    """A failure seeding live_position is swallowed; start still runs."""
    b = button_mod.PhantomStartGameButton(coord, entry, ADDRESS, NAME)
    coord._build_phantom_matrix_from_fen.side_effect = RuntimeError("boom")
    await b.async_press()
    coord.async_phantom_start_game.assert_awaited_once()


async def test_movement_verify_button_press(coord, entry):
    b = button_mod.PhantomMovementVerifyButton(coord, entry, ADDRESS, NAME)
    await b.async_press()
    coord._phantom_send_movement_verify.assert_awaited_once_with("1")


def test_button_available_gate(coord, entry):
    b = button_mod.PhantomStartGameButton(coord, entry, ADDRESS, NAME)
    coord.is_ble_connected = False
    assert b.available is False


# ─── switch.py ──────────────────────────────────────────────────────────


async def test_pause_switch(coord, entry):
    s = switch_mod.PhantomPauseSwitch(coord, entry, ADDRESS, NAME)
    coord.paused = True
    assert s.is_on is True
    await s.async_turn_on()
    coord.async_set_pause.assert_awaited_with(True)
    await s.async_turn_off()
    coord.async_set_pause.assert_awaited_with(False)
    coord.is_ble_connected = False
    assert s.available is False


async def test_training_wheels_switch(coord, entry):
    s = switch_mod.PhantomTrainingWheelsSwitch(coord, entry, ADDRESS, NAME)
    s.async_write_ha_state = MagicMock()
    coord.training_wheels = False
    assert s.is_on is False
    await s.async_turn_on()
    assert coord.training_wheels is True
    await s.async_turn_off()
    assert coord.training_wheels is False
    assert s.async_write_ha_state.call_count == 2


async def test_voice_announcements_switch(coord, entry):
    s = switch_mod.PhantomVoiceAnnouncementsSwitch(coord, entry, ADDRESS, NAME)
    s.async_write_ha_state = MagicMock()
    coord.voice_announcements = True
    assert s.is_on is True
    await s.async_turn_off()
    assert coord.voice_announcements is False
    await s.async_turn_on()
    assert coord.voice_announcements is True


@pytest.mark.parametrize(
    "cls,attr",
    [
        (switch_mod.PhantomTrainingWheelsSwitch, "training_wheels"),
        (switch_mod.PhantomVoiceAnnouncementsSwitch, "voice_announcements"),
    ],
)
async def test_restore_switch_added_to_hass(cls, attr, coord, entry, monkeypatch):
    monkeypatch.setattr(
        switch_mod.CoordinatorEntity, "async_added_to_hass", AsyncMock()
    )
    s = cls(coord, entry, ADDRESS, NAME)
    # restore ON
    s.async_get_last_state = AsyncMock(return_value=MagicMock(state="on"))
    await s.async_added_to_hass()
    assert getattr(coord, attr) is True
    # restore OFF
    s.async_get_last_state = AsyncMock(return_value=MagicMock(state="off"))
    await s.async_added_to_hass()
    assert getattr(coord, attr) is False
    # no last state -> unchanged
    setattr(coord, attr, "sentinel")
    s.async_get_last_state = AsyncMock(return_value=None)
    await s.async_added_to_hass()
    assert getattr(coord, attr) == "sentinel"
    # unknown state -> unchanged
    s.async_get_last_state = AsyncMock(return_value=MagicMock(state="unknown"))
    await s.async_added_to_hass()
    assert getattr(coord, attr) == "sentinel"


# ─── select.py ──────────────────────────────────────────────────────────


async def test_ai_level_select(coord, entry):
    s = select_mod.PhantomAiLevelSelect(coord, entry, ADDRESS, NAME)
    s.async_write_ha_state = MagicMock()
    coord.ai_level = 4
    assert s.current_option == "4"
    await s.async_select_option("7")
    assert coord.ai_level == 7
    s.async_write_ha_state.assert_called_once()


async def test_player_color_select(coord, entry):
    s = select_mod.PhantomPlayerColorSelect(coord, entry, ADDRESS, NAME)
    s.async_write_ha_state = MagicMock()
    coord.player_color = "white"
    assert s.current_option == "white"
    await s.async_select_option("black")
    assert coord.player_color == "black"


async def test_setup_mode_select(coord, entry):
    s = select_mod.PhantomSetupModeSelect(coord, entry, ADDRESS, NAME)
    s.async_write_ha_state = MagicMock()
    coord.setup_mode = None
    assert s.current_option == DEFAULT_SETUP_MODE
    opt = SETUP_MODE_OPTIONS[1]
    coord.setup_mode = opt
    assert s.current_option == opt
    await s.async_select_option(SETUP_MODE_OPTIONS[0])
    assert coord.setup_mode == SETUP_MODE_OPTIONS[0]


async def test_sculpture_game_select(coord, entry):
    s = select_mod.PhantomSculptureGameSelect(coord, entry, ADDRESS, NAME)
    s.async_write_ha_state = MagicMock()
    coord.selected_sculpture = None
    assert s.current_option == DEFAULT_SCULPTURE_GAME
    await s.async_select_option(SCULPTURE_GAMES[1])
    assert coord.selected_sculpture == SCULPTURE_GAMES[1]


async def test_restorable_select_added_to_hass(coord, entry, monkeypatch):
    monkeypatch.setattr(
        select_mod.CoordinatorEntity, "async_added_to_hass", AsyncMock()
    )
    s = select_mod.PhantomSetupModeSelect(coord, entry, ADDRESS, NAME)
    valid = SETUP_MODE_OPTIONS[1]
    s.async_get_last_state = AsyncMock(return_value=MagicMock(state=valid))
    await s.async_added_to_hass()
    assert coord.setup_mode == valid
    # None -> unchanged
    coord.setup_mode = "sentinel"
    s.async_get_last_state = AsyncMock(return_value=None)
    await s.async_added_to_hass()
    assert coord.setup_mode == "sentinel"
    # value not in options -> unchanged
    s.async_get_last_state = AsyncMock(return_value=MagicMock(state="bogus-mode"))
    await s.async_added_to_hass()
    assert coord.setup_mode == "sentinel"


# ─── number.py ──────────────────────────────────────────────────────────


async def test_mechanism_speed_number(coord, entry):
    n = number_mod.PhantomMechanismSpeedNumber(coord, entry, ADDRESS, NAME)
    n.async_write_ha_state = MagicMock()
    coord.mechanism_speed = 3
    assert n.native_value == 3.0
    assert n.native_min_value == 1
    assert n.native_max_value == 5
    await n.async_set_native_value(4)
    coord.async_set_mechanism_speed.assert_awaited_once_with(4)


async def test_sound_level_number(coord, entry):
    n = number_mod.PhantomSoundLevelNumber(coord, entry, ADDRESS, NAME)
    n.async_write_ha_state = MagicMock()
    coord.sound_level = 10
    assert n.native_value == 10.0
    await n.async_set_native_value(20)
    coord.async_set_sound_level.assert_awaited_once_with(20)


async def test_lichess_clock_numbers(coord, entry):
    m = number_mod.PhantomLichessClockMinutesNumber(coord, entry, ADDRESS, NAME)
    m.async_write_ha_state = MagicMock()
    coord.lichess_clock_minutes = 30
    assert m.native_value == 30.0
    assert m.available is True  # local config, always available
    await m.async_set_native_value(15)
    assert coord.lichess_clock_minutes == 15

    inc = number_mod.PhantomLichessClockIncrementNumber(coord, entry, ADDRESS, NAME)
    inc.async_write_ha_state = MagicMock()
    coord.lichess_clock_increment = 0
    assert inc.native_value == 0.0
    await inc.async_set_native_value(5)
    assert coord.lichess_clock_increment == 5


async def test_ai_vs_ai_numbers(coord, entry):
    w = number_mod.PhantomWhiteAILevelNumber(coord, entry, ADDRESS, NAME)
    w.async_write_ha_state = MagicMock()
    coord.white_ai_level = 3
    assert w.native_value == 3.0
    await w.async_set_native_value(6)
    assert coord.white_ai_level == 6

    b = number_mod.PhantomBlackAILevelNumber(coord, entry, ADDRESS, NAME)
    b.async_write_ha_state = MagicMock()
    coord.black_ai_level = 3
    assert b.native_value == 3.0
    await b.async_set_native_value(2)
    assert coord.black_ai_level == 2

    d = number_mod.PhantomAIvsAIMoveDelayNumber(coord, entry, ADDRESS, NAME)
    d.async_write_ha_state = MagicMock()
    coord.ai_vs_ai_move_delay = 1.0
    assert d.native_value == 1.0
    await d.async_set_native_value(2.5)
    assert coord.ai_vs_ai_move_delay == 2.5


def test_number_ble_gated_availability(coord, entry):
    n = number_mod.PhantomMechanismSpeedNumber(coord, entry, ADDRESS, NAME)
    coord.is_ble_connected = False
    assert n.available is False


async def test_restorable_number_added_to_hass(coord, entry, monkeypatch):
    monkeypatch.setattr(
        number_mod.CoordinatorEntity, "async_added_to_hass", AsyncMock()
    )
    n = number_mod.PhantomLichessClockMinutesNumber(coord, entry, ADDRESS, NAME)
    # valid value restored as int
    n.async_get_last_state = AsyncMock(return_value=MagicMock(state="45"))
    await n.async_added_to_hass()
    assert coord.lichess_clock_minutes == 45
    assert isinstance(coord.lichess_clock_minutes, int)
    # out-of-range value clamps to max (180)
    n.async_get_last_state = AsyncMock(return_value=MagicMock(state="9999"))
    await n.async_added_to_hass()
    assert coord.lichess_clock_minutes == 180
    # below-range clamps to min (1)
    n.async_get_last_state = AsyncMock(return_value=MagicMock(state="0"))
    await n.async_added_to_hass()
    assert coord.lichess_clock_minutes == 1
    # non-numeric -> unchanged
    coord.lichess_clock_minutes = 7
    n.async_get_last_state = AsyncMock(return_value=MagicMock(state="abc"))
    await n.async_added_to_hass()
    assert coord.lichess_clock_minutes == 7
    # None -> unchanged
    n.async_get_last_state = AsyncMock(return_value=None)
    await n.async_added_to_hass()
    assert coord.lichess_clock_minutes == 7


async def test_restorable_number_float_type(coord, entry, monkeypatch):
    monkeypatch.setattr(
        number_mod.CoordinatorEntity, "async_added_to_hass", AsyncMock()
    )
    d = number_mod.PhantomAIvsAIMoveDelayNumber(coord, entry, ADDRESS, NAME)
    d.async_get_last_state = AsyncMock(return_value=MagicMock(state="2.5"))
    await d.async_added_to_hass()
    assert coord.ai_vs_ai_move_delay == 2.5
    assert isinstance(coord.ai_vs_ai_move_delay, float)


# ─── image.py ───────────────────────────────────────────────────────────


@pytest.fixture
def board_image(coord, entry):
    return image_mod.PhantomChessBoardImage(MagicMock(), coord, entry, ADDRESS, NAME)


def test_image_render_starting_position(board_image, coord):
    coord.data = {}
    coord.player_color = "white"
    out = board_image.image()
    assert isinstance(out, bytes)
    assert out.startswith(b"<") and b"svg" in out


def test_image_render_with_lastmove_and_orientation(board_image, coord):
    coord.data = {
        "live_fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR",
        "last_move": "e2e4",
    }
    coord.player_color = "black"  # flipped orientation
    out = board_image.image()
    assert b"svg" in out


def test_image_invalid_fen_falls_back(board_image, coord):
    coord.data = {"live_fen": "this is not a fen"}
    out = board_image.image()
    assert isinstance(out, bytes)
    assert b"svg" in out


def test_image_invalid_lastmove_ignored(board_image, coord):
    coord.data = {
        "live_fen": image_mod.STARTING_FEN,
        "last_move": "zzzz",
    }
    out = board_image.image()
    assert b"svg" in out


def test_image_training_wheels_glyph_overlay(board_image, coord):
    coord.training_wheels = True
    coord.data = {
        "live_fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR",
        "last_move": "e2e4",
        "last_move_classification": "blunder",
    }
    coord.player_color = "white"
    out = board_image.image().decode("utf-8")
    # overlay injects a <text> element with the glyph
    assert "<text" in out
    assert out.count("</svg>") == 1


def test_image_suppressed_glyph_class_no_overlay(board_image, coord):
    coord.training_wheels = True
    coord.data = {
        "live_fen": image_mod.STARTING_FEN,
        "last_move": "e2e4",
        "last_move_classification": "best",  # suppressed
    }
    out = board_image.image().decode("utf-8")
    assert "<text" not in out.split("</svg>")[-2] if "</svg>" in out else True


def test_image_orientation_helper(board_image, coord):
    coord.player_color = "black"
    assert board_image._current_orientation() == chess.BLACK
    coord.player_color = "white"
    assert board_image._current_orientation() == chess.WHITE
    coord.player_color = "random"
    assert board_image._current_orientation() == chess.WHITE


def test_image_square_center_px():
    fn = image_mod.PhantomChessBoardImage._square_center_px
    # a1 white orientation: bottom-left
    cx, cy = fn(chess.A1, chess.WHITE)
    assert cx < cy  # left column, bottom row
    # a1 black orientation: top-right
    cx_b, cy_b = fn(chess.A1, chess.BLACK)
    assert cx_b != cx


def test_image_handle_coordinator_update_bumps_timestamp(board_image, coord, monkeypatch):
    monkeypatch.setattr(
        image_mod.CoordinatorEntity, "_handle_coordinator_update", MagicMock()
    )
    coord.data = {"live_fen": image_mod.STARTING_FEN, "last_move": "e2e4"}
    coord.player_color = "white"
    board_image._last_rendered_fen = None
    before = board_image._attr_image_last_updated
    board_image._handle_coordinator_update()
    after = board_image._attr_image_last_updated
    assert after >= before
    assert board_image._last_rendered_fen == image_mod.STARTING_FEN
    # second identical update should NOT bump
    same = board_image._attr_image_last_updated
    board_image._handle_coordinator_update()
    assert board_image._attr_image_last_updated == same


def test_image_available_gate(board_image, coord):
    coord.is_ble_connected = True
    assert board_image.available is True
    coord.is_ble_connected = False
    assert board_image.available is False


def test_image_overlay_no_glyph_returns_svg(board_image, monkeypatch):
    """classification_color_glyph returning empty glyph -> unchanged svg."""
    monkeypatch.setattr(
        image_mod, "classification_color_glyph", lambda c: ("#fff", "")
    )
    svg = "<svg></svg>"
    move = chess.Move.from_uci("e2e4")
    out = board_image._overlay_classification_glyph(svg, move, "blunder", chess.WHITE)
    assert out == svg


def test_image_overlay_no_closing_tag_appends(board_image, monkeypatch):
    monkeypatch.setattr(
        image_mod, "classification_color_glyph", lambda c: ("#f00", "??")
    )
    svg = "<svg-broken>"  # no </svg>
    move = chess.Move.from_uci("e2e4")
    out = board_image._overlay_classification_glyph(svg, move, "blunder", chess.WHITE)
    assert out.startswith("<svg-broken>")
    assert "<text" in out

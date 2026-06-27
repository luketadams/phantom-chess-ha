"""Voice-announcements master mute (v0.4-beta3).

``_announce_via_tts`` must:
  - ALWAYS fire the ``phantom_chess_announce`` event (carrying the
    current ``voice_enabled`` flag), so event-driven automations keep
    working regardless of the toggle.
  - Only perform the direct ``tts.speak`` call (the spoken voiceover)
    when ``coordinator.voice_announcements`` is True.

The method is async and only touches a handful of attributes, so — like
the two-player PGN tests — we bind the unbound method to a lightweight
stub and drive it directly. Runs in the minimal CI env (no board, no
real HA services).
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

from custom_components.phantom_chess.coordinator import PhantomChessCoordinator


def _stub(voice_on: bool) -> types.SimpleNamespace:
    """Stub carrying just what ``_announce_via_tts`` reads/calls.

    Options include a configured TTS engine + speaker so the direct
    ``tts.speak`` path is *eligible* — whether it actually fires is then
    governed solely by ``voice_announcements``.
    """
    stub = types.SimpleNamespace()
    stub.voice_announcements = voice_on
    stub._ble_address = "C8:C9:A3:F2:7C:0A"
    stub._entry = types.SimpleNamespace(
        options={
            "tts_service": "tts.home_assistant_cloud",
            "tts_media_player_entity_id": "media_player.living_room_voice",
        }
    )
    stub.hass = types.SimpleNamespace(
        bus=types.SimpleNamespace(async_fire=MagicMock()),
        services=types.SimpleNamespace(async_call=AsyncMock()),
    )
    return stub


def test_mute_suppresses_tts_but_still_fires_event() -> None:
    stub = _stub(voice_on=False)
    asyncio.run(PhantomChessCoordinator._announce_via_tts(stub, "Knight takes e5"))

    # Event always fires, with voice_enabled reflecting the muted state.
    stub.hass.bus.async_fire.assert_called_once()
    event_name, payload = stub.hass.bus.async_fire.call_args.args
    assert event_name == "phantom_chess_announce"
    assert payload["message"] == "Knight takes e5"
    assert payload["voice_enabled"] is False
    # The spoken voiceover is suppressed.
    stub.hass.services.async_call.assert_not_called()


def test_unmuted_speaks_via_tts() -> None:
    stub = _stub(voice_on=True)
    asyncio.run(PhantomChessCoordinator._announce_via_tts(stub, "Check"))

    stub.hass.bus.async_fire.assert_called_once()
    _, payload = stub.hass.bus.async_fire.call_args.args
    assert payload["voice_enabled"] is True
    # tts.speak is dispatched exactly once.
    stub.hass.services.async_call.assert_called_once()
    domain, service = stub.hass.services.async_call.call_args.args[:2]
    assert (domain, service) == ("tts", "speak")


def test_empty_message_is_a_noop() -> None:
    stub = _stub(voice_on=True)
    asyncio.run(PhantomChessCoordinator._announce_via_tts(stub, ""))
    stub.hass.bus.async_fire.assert_not_called()
    stub.hass.services.async_call.assert_not_called()

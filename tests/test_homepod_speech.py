"""Validate whole-message buffering and speaker isolation."""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from custom_components.phantom_chess import homepod_speech as speech


@pytest.fixture
def setup_speech(monkeypatch):
    atv = SimpleNamespace(audio=SimpleNamespace(set_volume=AsyncMock()),
                          stream=SimpleNamespace(stream_file=AsyncMock()))
    manager = SimpleNamespace(atv=atv)
    hass = MagicMock()
    hass.config_entries.async_get_entry.return_value = SimpleNamespace(runtime_data=manager)
    registry = MagicMock()
    registry.async_get.return_value = SimpleNamespace(platform='apple_tv', config_entry_id='paired')
    monkeypatch.setattr(speech.er, 'async_get', lambda _: registry)
    fetch = AsyncMock(return_value=('mp3', b'whole message audio'))
    generate = MagicMock(return_value='media-source://tts/test')
    monkeypatch.setitem(sys.modules, 'homeassistant.components.tts',
                        SimpleNamespace(async_get_media_source_audio=fetch))
    monkeypatch.setitem(sys.modules, 'homeassistant.components.tts.media_source',
                        SimpleNamespace(generate_media_source_id=generate))
    data = {'media_player_entity_id': 'media_player.homepod', 'tts_entity_id': 'tts.voice',
            'message': 'One uninterrupted sentence.', 'voice': 'exact-voice',
            'language': 'en', 'volume_level': 0.8}
    return hass, manager, registry, fetch, generate, data


async def test_whole_audio_buffer_and_volume(setup_speech):
    hass, manager, _, _, generate, data = setup_speech
    async def stream(buffer):
        assert buffer.seekable()
        assert buffer.read() == b'whole message audio'
    manager.atv.stream.stream_file.side_effect = stream
    await speech.async_speak(hass, data)
    manager.atv.audio.set_volume.assert_awaited_once_with(80)
    manager.atv.stream.stream_file.assert_awaited_once()
    generate.assert_called_once_with(hass, data['message'], engine='tts.voice',
                                    language='en', options={'voice': 'exact-voice'}, cache=True)
    hass.services.async_call.assert_not_called()  # No queue/play/restore actions.


async def test_wrong_integration_rejected_before_synthesis(setup_speech):
    hass, _, registry, fetch, _, data = setup_speech
    registry.async_get.return_value.platform = 'music_assistant'
    with pytest.raises(speech.ServiceValidationError):
        await speech.async_speak(hass, data)
    fetch.assert_not_awaited()


async def test_disconnected_speaker_rejected(setup_speech):
    hass, manager, _, fetch, _, data = setup_speech
    manager.atv = None
    with pytest.raises(speech.ServiceValidationError):
        await speech.async_speak(hass, data)
    fetch.assert_not_awaited()


async def test_empty_audio_never_starts_playback(setup_speech):
    hass, manager, _, fetch, _, data = setup_speech
    fetch.return_value = ('mp3', b'')
    with pytest.raises(speech.ServiceValidationError):
        await speech.async_speak(hass, data)
    manager.atv.stream.stream_file.assert_not_awaited()


async def test_preferred_pipeline_voice_is_resolved_for_each_message(setup_speech, monkeypatch):
    hass, _, _, _, generate, data = setup_speech
    pipeline = SimpleNamespace(tts_engine='tts.preferred', tts_language='en-GB', tts_voice='current-voice')
    resolve = MagicMock(return_value=pipeline)
    monkeypatch.setitem(sys.modules, 'homeassistant.components.assist_pipeline.pipeline',
                        SimpleNamespace(async_get_pipeline=resolve))
    data.pop('tts_entity_id')
    await speech.async_speak(hass, data)
    assert generate.call_args.kwargs['engine'] == 'tts.preferred'
    assert generate.call_args.kwargs['options'] == {'voice': 'current-voice'}
    pipeline.tts_voice = 'changed-voice'
    await speech.async_speak(hass, data)
    assert generate.call_args.kwargs['options'] == {'voice': 'changed-voice'}
    assert resolve.call_count == 2

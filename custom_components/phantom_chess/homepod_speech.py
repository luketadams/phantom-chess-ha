"""Whole-message HomePod speech without pyatv's HTTP reader or music queues."""
from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any, cast

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import DOMAIN

SCHEMA = vol.Schema({
    vol.Required("media_player_entity_id"): cv.entity_id,
    vol.Optional("tts_entity_id"): cv.entity_id,
    vol.Optional("pipeline_id"): cv.string,
    vol.Required("message"): vol.All(cv.string, vol.Length(min=1, max=4000)),
    vol.Optional("language", default="en"): cv.string,
    vol.Optional("voice"): cv.string,
    vol.Optional("volume_level", default=0.8): vol.All(vol.Coerce(float), vol.Range(min=0, max=1)),
})


async def async_speak(hass: HomeAssistant, data: dict) -> None:
    """Reuse the paired Apple TV connection and stream a seekable audio buffer.

    pyatv's documented buffer API avoids the HTTP read deadlock in issue 2849.
    No global monkey patch and no Music Assistant queue operations are involved.
    """
    from homeassistant.components.tts import async_get_media_source_audio
    from homeassistant.components.tts.media_source import generate_media_source_id

    data = dict(data)
    if not data.get("tts_entity_id"):
        from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline
        pipeline = async_get_pipeline(hass, data.get("pipeline_id"))
        if not pipeline.tts_engine:
            raise ServiceValidationError("The selected voice assistant has no speech provider")
        data["tts_entity_id"] = pipeline.tts_engine
        data["language"] = pipeline.tts_language or "en"
        if pipeline.tts_voice:
            data["voice"] = pipeline.tts_voice
    entity = er.async_get(hass).async_get(data["media_player_entity_id"])
    if entity is None or entity.platform != "apple_tv" or not entity.config_entry_id:
        raise ServiceValidationError("Choose a HomePod from the Apple TV integration")
    entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    manager = getattr(entry, "runtime_data", None)
    atv: Any = getattr(manager, "atv", None)
    if atv is None:
        raise ServiceValidationError("HomePod is not connected")
    options = {"voice": data["voice"]} if data.get("voice") else {}
    source = generate_media_source_id(
        hass, data["message"], engine=data["tts_entity_id"],
        language=data["language"], options=options, cache=True,
    )
    async with asyncio.timeout(60):
        _, audio = await async_get_media_source_audio(hass, source)
    if not audio:
        raise ServiceValidationError("Speech provider returned empty audio")
    if getattr(manager, "atv", None) is not atv:
        raise ServiceValidationError("HomePod reconnected; please try again")
    connection = cast(Any, atv)
    async with asyncio.timeout(120):
        await connection.audio.set_volume(data["volume_level"] * 100)
        with BytesIO(audio) as buffer:
            await connection.stream.stream_file(buffer)


def register_service(hass: HomeAssistant) -> None:
    """Serialize announcements per speaker, including synthesis and playback."""
    locks: dict[str, asyncio.Lock] = {}

    async def handle(call: ServiceCall) -> None:
        lock = locks.setdefault(call.data["media_player_entity_id"], asyncio.Lock())
        async with lock:
            await async_speak(hass, dict(call.data))

    hass.services.async_register(DOMAIN, "speak_homepod", handle, schema=SCHEMA)

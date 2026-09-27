"""Exercise actual streaming, hashing and atomic filesystem installation."""
import asyncio
from dataclasses import replace
import hashlib
import io
import tarfile
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.phantom_chess.engine_artifacts import (
    ASSETS, GLIBC_BASELINE, MUSL_ARM, MUSL_X86, EngineAsset, ensure_verified_engine,
)
from custom_components.phantom_chess.lichess_analysis import StockfishFallback


class Hass:
    async def async_add_executor_job(self, fn, *args):
        return await asyncio.to_thread(fn, *args)


class Response:
    def __init__(self, data, status=200, gate=None):
        self.data, self.status, self.gate = data, status, gate
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        for i in range(0, len(self.data), 17):
            if self.gate:
                await self.gate.wait()
            yield self.data[i:i+17]


def sample(*, link=False, duplicate=False, member="engine/bin", payload=b"verified bytes"):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        record = tarfile.TarInfo(member)
        if link:
            record.type = tarfile.SYMTYPE
            record.linkname = "/tmp/not-an-engine"
            archive.addfile(record)
        else:
            record.size = len(payload)
            archive.addfile(record, io.BytesIO(payload))
            if duplicate:
                archive.addfile(record, io.BytesIO(payload))
        # Never extract this irrelevant path outside the staging directory.
        extra = tarfile.TarInfo("../escaped")
        extra.size = 3
        archive.addfile(extra, io.BytesIO(b"bad"))
    data = output.getvalue()
    asset = EngineAsset("https://example.invalid/engine.tar", "engine/bin",
                        hashlib.sha256(data).hexdigest(), hashlib.sha256(payload).hexdigest(), len(data), len(payload))
    return data, asset


def session(data, **kwargs):
    return Mock(get=Mock(return_value=Response(data, **kwargs)))


async def test_verified_install_and_cache_reuse(tmp_path):
    data, asset = sample()
    remote = session(data)
    status = Mock()
    target = await ensure_verified_engine(Hass(), remote, tmp_path, asset, status)
    assert target.read_bytes() == b"verified bytes"
    assert target.stat().st_mode & 0o111
    assert not (tmp_path.parent / "escaped").exists()
    assert list(tmp_path.iterdir()) == [target]
    assert await ensure_verified_engine(Hass(), remote, tmp_path, asset, status) == target
    assert remote.get.call_count == 1
    assert [c.args[0] for c in status.call_args_list] == ["verifying", "downloading", "installing", "verifying"]


@pytest.mark.parametrize("fault", ["archive_hash", "binary_hash", "truncated", "oversized", "http", "link", "duplicate", "missing", "binary_size"])
async def test_bad_package_never_replaces_existing_engine(tmp_path, fault):
    old = tmp_path / "engine"
    old.write_bytes(b"previous installation")
    old.chmod(0o755)
    data, asset = sample(link=fault == "link", duplicate=fault == "duplicate",
                         member="wrong/path" if fault == "missing" else "engine/bin")
    if fault == "archive_hash":
        asset = replace(asset, archive_sha256="0"*64)
    if fault == "binary_hash":
        asset = replace(asset, binary_sha256="0"*64)
    if fault == "binary_size":
        asset = replace(asset, binary_bytes=999)
    if fault == "truncated":
        data = data[:-1]
    if fault == "oversized":
        data += b"extra"
    with pytest.raises(ValueError):
        await ensure_verified_engine(Hass(), session(data, status=503 if fault == "http" else 200), tmp_path, asset, Mock())
    assert old.read_bytes() == b"previous installation"
    assert list(tmp_path.iterdir()) == [old]


async def test_cancelled_download_keeps_old_engine_and_cleans_staging(tmp_path):
    data, asset = sample()
    gate = asyncio.Event()
    remote = session(data, gate=gate)
    task = asyncio.create_task(ensure_verified_engine(Hass(), remote, tmp_path, asset, Mock()))
    async with asyncio.timeout(2):
        while not remote.get.called:
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.iterdir())


async def test_two_boards_share_one_verified_download(tmp_path):
    data, asset = sample()
    remote = session(data)
    results = await asyncio.gather(*(ensure_verified_engine(Hass(), remote, tmp_path, asset, Mock()) for _ in range(2)))
    assert results[0] == results[1]
    assert remote.get.call_count == 1


async def test_install_failure_preserves_previous_file(tmp_path):
    data, asset = sample()
    target = tmp_path / "engine"
    target.write_bytes(b"old")
    with patch("custom_components.phantom_chess.engine_artifacts.os.replace", side_effect=OSError("read only")):
        with pytest.raises(OSError):
            await ensure_verified_engine(Hass(), session(data), tmp_path, asset, Mock())
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_platform_entries_are_real_and_do_not_assume_avx2():
    assert ASSETS["glibc", "x86_64"] == GLIBC_BASELINE
    assert "avx2" not in GLIBC_BASELINE.url
    assert ASSETS["musl", "aarch64"] == MUSL_ARM
    assert ("glibc", "aarch64") not in ASSETS  # upstream does not ship that asset
    for musl in (MUSL_X86, MUSL_ARM):
        # Mirror first, then the rolling Alpine repository as a fallback.
        assert musl.sources[0].startswith("https://github.com/")
        assert musl.sources[-1].startswith("https://dl-cdn.alpinelinux.org/")


def _routed(responses):
    """Session whose GET returns a per-URL response (or raises it)."""
    def get(url, **kwargs):
        result = responses[url]
        if isinstance(result, BaseException):
            raise result
        return result
    return Mock(get=Mock(side_effect=get))


@pytest.mark.parametrize("primary", ["http", "corrupt", "network"])
async def test_falls_back_to_mirror_when_primary_fails(tmp_path, primary):
    import aiohttp
    data, asset = sample()
    asset = replace(asset, mirrors=("https://mirror.invalid/engine.tar",))
    bad = {"http": Response(data, status=404), "corrupt": Response(data[:-1] + b"x"),
           "network": aiohttp.ClientConnectionError("down")}[primary]
    remote = _routed({asset.url: bad, asset.mirrors[0]: Response(data)})
    target = await ensure_verified_engine(Hass(), remote, tmp_path, asset, Mock())
    assert target.read_bytes() == b"verified bytes"
    assert [c.args[0] for c in remote.get.call_args_list] == list(asset.sources)
    assert list(tmp_path.iterdir()) == [target]


async def test_all_sources_failing_raises_last_error_and_keeps_old_engine(tmp_path):
    old = tmp_path / "engine"
    old.write_bytes(b"previous installation")
    data, asset = sample()
    asset = replace(asset, mirrors=("https://mirror.invalid/engine.tar",))
    remote = _routed({asset.url: Response(data, status=503), asset.mirrors[0]: Response(data, status=410)})
    with pytest.raises(ValueError, match="HTTP 410"):
        await ensure_verified_engine(Hass(), remote, tmp_path, asset, Mock())
    assert list(tmp_path.iterdir()) == [old]


async def test_wrapper_exposes_install_failure_and_unsupported_platform(tmp_path):
    engine = StockfishFallback(Hass(), tmp_path)
    callback = Mock()
    engine.status_callback = callback
    with patch("platform.machine", return_value="sparc64"):
        assert await engine._ensure_binary() is False
        assert await engine._ensure_binary() is False
        assert engine.engine_state["status"] == "unavailable"
    with patch("platform.machine", return_value="x86_64"), patch("custom_components.phantom_chess.lichess_analysis._detect_libc", return_value="glibc"), patch("homeassistant.helpers.aiohttp_client.async_get_clientsession", return_value=Mock()), patch("custom_components.phantom_chess.lichess_analysis.ensure_verified_engine", new=AsyncMock(side_effect=OSError("network failed"))):
        assert await engine._ensure_binary() is False
        assert engine.engine_state["error"] == "network failed"
    callback.assert_called()


async def test_wrapper_accepts_verified_result_and_cancellation(tmp_path):
    engine = StockfishFallback(Hass(), tmp_path)
    with patch("platform.machine", return_value="x86_64"), patch("custom_components.phantom_chess.lichess_analysis._detect_libc", return_value="glibc"), patch("homeassistant.helpers.aiohttp_client.async_get_clientsession", return_value=Mock()), patch("custom_components.phantom_chess.lichess_analysis.ensure_verified_engine", new=AsyncMock(return_value=tmp_path / "engine")) as install:
        assert await engine._ensure_binary() is True
        assert engine.binary_path == tmp_path / "engine"
        install.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await engine._ensure_binary()
        assert engine.engine_state["status"] == "not_checked"


async def test_engine_preflight_retries_without_board_operations():
    from .ble_mock import make_coordinator
    coord = make_coordinator(ble_connected=True)
    engine = Mock(_eval_lock=asyncio.Lock(), _available=False, engine_state={"status": "ready", "error": None})
    engine.ensure_engine = AsyncMock(return_value=Mock(id={"name": "Stockfish test"}, ping=AsyncMock()))
    coord._analysis_client = Mock(_stockfish=engine)
    coord._state["engine_error"] = "Previous failure"
    result = await coord.async_check_engine()
    assert coord._state["engine_error"] is None
    assert engine._available is True
    assert result["version"] == "Stockfish test"
    assert coord._state["engine_health"] == result
    coord._local_game_active = True
    with pytest.raises(RuntimeError, match="End the current game"):
        await coord.async_check_engine()
    coord._local_game_active = False
    coord._analysis_client = None
    with pytest.raises(RuntimeError, match="unavailable"):
        await coord.async_check_engine()


async def test_engine_preflight_timeout_is_actionable():
    from .ble_mock import make_coordinator
    coord = make_coordinator(ble_connected=True)
    engine = Mock(_eval_lock=asyncio.Lock())
    engine.ensure_engine = AsyncMock(side_effect=TimeoutError())
    coord._analysis_client = Mock(_stockfish=engine)
    assert (await coord.async_check_engine())["status"] == "error"


async def test_preflight_replaces_a_dead_cached_engine_once():
    from .ble_mock import make_coordinator
    coord = make_coordinator(ble_connected=True)
    dead = Mock(ping=AsyncMock(side_effect=RuntimeError("terminated")))
    ready = Mock(id={"name": "Recovered Stockfish"}, ping=AsyncMock())
    engine = Mock(_eval_lock=asyncio.Lock(), engine_state={"status": "ready", "error": None})
    engine.ensure_engine = AsyncMock(side_effect=[dead, ready])
    engine.shutdown = AsyncMock()
    coord._analysis_client = Mock(_stockfish=engine)
    assert (await coord.async_check_engine())["version"] == "Recovered Stockfish"
    engine.shutdown.assert_awaited_once()
    assert engine.ensure_engine.await_count == 2
    ready.ping.assert_awaited_once()


async def test_online_start_without_token_is_refused_with_guidance():
    from homeassistant.exceptions import HomeAssistantError
    from .ble_mock import make_coordinator
    coord = make_coordinator(ble_connected=True)
    coord._lichess_token = ""
    with pytest.raises(HomeAssistantError) as info:
        await coord.async_start_game()
    assert info.value.translation_key == "lichess_token_required"
    import json
    from pathlib import Path
    strings = json.loads((Path(__file__).parent.parent / "custom_components/phantom_chess/strings.json").read_text())
    assert "Reconfigure" in strings["exceptions"]["lichess_token_required"]["message"]
    assert not coord._state.get("lichess_active")

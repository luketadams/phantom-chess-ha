"""Pinned engine artifacts, bounded streaming and atomic verified installation."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
from typing import Any, Callable
from weakref import WeakValueDictionary

import aiohttp


@dataclass(frozen=True)
class EngineAsset:
    url: str
    member: str
    archive_sha256: str
    binary_sha256: str
    archive_bytes: int
    binary_bytes: int
    # Further byte-identical copies, tried in order after ``url``. Every copy
    # must pass the same archive and binary digests, so a mirror adds
    # availability without adding trust.
    mirrors: tuple[str, ...] = ()

    @property
    def sources(self) -> tuple[str, ...]:
        return (self.url, *self.mirrors)


# Alpine ships Stockfish only in edge/testing, a rolling repository: a rebuild
# (18-r1) or promotion removes the pinned 18-r0 package. The project mirror
# holds the identical signed packages; see docs/ENGINE_ARTIFACTS.md.
ENGINE_MIRROR = "https://github.com/luketadams/phantom-chess-engines/releases/download/stockfish-18"


# The GitHub archive digests match release API metadata. Alpine archive and
# binary digests are pinned from the publisher's signed 18-r0 packages.
# Acquisition and signature evidence: docs/ENGINE_ARTIFACTS.md.

GLIBC_AVX2 = EngineAsset(
    'https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64-avx2.tar',
    'stockfish/stockfish-ubuntu-x86-64-avx2',
    '536c0c2c0cf06450df0bfb5e876ef0d3119950703a8f143627f990c7b5417964',
    '6b087694916228c905a5e14db74cca8c7e5643602226af1fa5d42353c455b9f9',
    114401280, 112933248,
)

GLIBC_BASELINE = EngineAsset(
    'https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64.tar',
    'stockfish/stockfish-ubuntu-x86-64',
    '5c6f38b02a4da5f3ffe763f27da6c3e743eebefd92b50cb3661623b96696adff',
    '7a44d64fd877ee888a5160349827563444e1935ca6c1095d0f8e0859d57101c7',
    114391040, 112920960,
)

MUSL_X86 = EngineAsset(
    f'{ENGINE_MIRROR}/stockfish-18-r0-x86_64.apk',
    'usr/bin/stockfish',
    '804a4ae7d35ed55d30dd031e6e1c738a4d9d3e2cbe2c293495c12ec4c67271e1',
    'f641c102b2e46682a24284566e7bf1222600da594106f29d901e24b0460aeac9',
    75884923, 112982200,
    mirrors=('https://dl-cdn.alpinelinux.org/alpine/edge/testing/x86_64/stockfish-18-r0.apk',),
)

MUSL_ARM = EngineAsset(
    f'{ENGINE_MIRROR}/stockfish-18-r0-aarch64.apk',
    'usr/bin/stockfish',
    'be308a62ea3045a9e36b2336ebad7a6e38b9030285b1c7e4927249e97b21b9b4',
    'cc28730bcc22f1e510e82561846bef0aa5bddb92cd83441f1dc5d034a0cb3f8a',
    75887859, 112986200,
    mirrors=('https://dl-cdn.alpinelinux.org/alpine/edge/testing/aarch64/stockfish-18-r0.apk',),
)

# The baseline x86 binary avoids assuming AVX2. Stockfish 18 does not publish
# the old integration's invented stockfish-ubuntu-arm64.tar asset.
ASSETS = {
    ("glibc", "x86_64"): GLIBC_BASELINE, ("glibc", "amd64"): GLIBC_BASELINE,
    ("musl", "x86_64"): MUSL_X86, ("musl", "amd64"): MUSL_X86,
    ("musl", "aarch64"): MUSL_ARM, ("musl", "arm64"): MUSL_ARM,
}
_INSTALL_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _installed(target: Path, asset: EngineAsset) -> bool:
    if target.is_symlink() or not target.is_file() or not os.access(target, os.X_OK):
        return False
    allowed_sizes = {asset.binary_bytes}
    if asset == GLIBC_BASELINE:
        allowed_sizes.add(GLIBC_AVX2.binary_bytes)
    if target.stat().st_size not in allowed_sizes:
        return False
    allowed = {asset.binary_sha256}
    if asset == GLIBC_BASELINE:
        # Preserve a previously installed, verified official AVX2 binary.
        allowed.add(GLIBC_AVX2.binary_sha256)
    return _digest(target) in allowed


def _prepare(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="engine-install-", dir=directory))


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as stream:
        stream.write(data)


def _verify_install(archive: Path, target: Path, asset: EngineAsset) -> None:
    if archive.stat().st_size != asset.archive_bytes or _digest(archive) != asset.archive_sha256:
        raise ValueError("Engine download failed its SHA-256 check")
    with tarfile.open(archive) as package:
        matches = [m for m in package.getmembers() if m.name == asset.member]
        if len(matches) != 1 or not matches[0].isfile() or matches[0].size != asset.binary_bytes:
            raise ValueError("Engine package does not contain the expected executable")
        source = package.extractfile(matches[0])
        if source is None:
            raise ValueError("Engine executable could not be read")
        staged = archive.parent / "verified-engine"
        with source, staged.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
    if _digest(staged) != asset.binary_sha256:
        raise ValueError("Engine executable failed its SHA-256 check")
    staged.chmod(0o755)
    # Never remove the working engine first. Readers see either whole version.
    os.replace(staged, target)


async def ensure_verified_engine(hass: Any, session: Any, directory: Path,
                                 asset: EngineAsset, status: Callable[[str], None], *, timeout: float = 120) -> Path:
    key = str(directory)
    lock = _INSTALL_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        target = directory / "engine"
        status("verifying")
        if await hass.async_add_executor_job(_installed, target, asset):
            return target
        last_error: BaseException | None = None
        for url in asset.sources:
            status("downloading")
            try:
                return await _download_and_install(hass, session, directory, target, asset, url, status, timeout)
            except (ValueError, OSError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                # Try the next byte-identical source; the working engine is untouched.
                last_error = err
        assert last_error is not None
        raise last_error


async def _download_and_install(hass: Any, session: Any, directory: Path, target: Path,
                                asset: EngineAsset, url: str, status: Callable[[str], None],
                                timeout: float) -> Path:
    temporary = await hass.async_add_executor_job(_prepare, directory)
    archive = temporary / "download"
    try:
        total = 0
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            if response.status != 200:
                raise ValueError(f"Engine download returned HTTP {response.status}")
            async for chunk in response.content.iter_chunked(1024 * 1024):
                total += len(chunk)
                if total > asset.archive_bytes:
                    raise ValueError("Engine download exceeds its expected size")
                await hass.async_add_executor_job(_append, archive, chunk)
        if total != asset.archive_bytes:
            raise ValueError("Engine download is incomplete")
        status("installing")
        await hass.async_add_executor_job(_verify_install, archive, target, asset)
        return target
    finally:
        await hass.async_add_executor_job(shutil.rmtree, temporary, True)

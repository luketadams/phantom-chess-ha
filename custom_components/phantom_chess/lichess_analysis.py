"""Lichess analysis pipeline for the Phantom Chess Board.

Provides:
- Cloud-eval HTTP client (https://lichess.org/api/cloud-eval)
- Opening explorer client (https://explorer.lichess.ovh/masters)
- Move classifier (centipawn-loss diff → label per Lichess thresholds)
- Threat detector (side-to-move "if I could move now" best capture/mate)
- Fork detector (attacks ≥2 opponent pieces of higher/equal value)
- Lichess accuracy metric (per-move and game-level)
- Local Stockfish fallback for cloud-eval cache misses (added 2026-05-14)

References:
- https://lichess.org/api  (cloud-eval, explorer)
- https://lichess.org/page/accuracy  (accuracy formula)
- https://github.com/official-stockfish/Stockfish/releases/tag/sf_18  (binary releases)
- Gotcha: strip en-passant square from FEN unless a capture is legal, or
  cloud-eval returns 404 (api issue #147). Handled in _safe_fen_for_eval.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import platform
import stat
import tarfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import chess
import chess.engine

from .const import (
    CLASSIFICATION_BEST,
    CLASSIFICATION_BLUNDER,
    CLASSIFICATION_GOOD,
    CLASSIFICATION_INACCURACY,
    CLASSIFICATION_MISTAKE,
    CLASSIFICATION_UNKNOWN,
    CPL_GOOD_MAX,
    CPL_INACCURACY_MAX,
    CPL_MISTAKE_MAX,
    LICHESS_CLOUD_EVAL_URL,
    LICHESS_OPENING_URL,
)

_LOGGER = logging.getLogger(__name__)


# ── Eval result dataclass ────────────────────────────────────────────────────

@dataclass
class EvalResult:
    """One eval response. Fields normalized to white-positive perspective."""
    cp: int | None              # centipawns, white-positive. None if mate.
    mate: int | None            # plies to mate, signed (positive = white mates). None if no mate.
    depth: int | None           # engine depth
    best_uci: str | None        # engine's top move from this position (UCI)
    source: str = "lichess-cloud"   # "lichess-cloud", "stockfish-local", "stub"
    raw: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def white_win_pct(self) -> float:
        """Win-percentage from white's perspective (Lichess formula).

        winChances = 2 / (1 + exp(-0.00368208 * cp)) - 1
        winPct = 50 + 50 * winChances
        """
        if self.mate is not None:
            return 100.0 if self.mate > 0 else 0.0
        if self.cp is None:
            return 50.0
        try:
            wc = 2.0 / (1.0 + math.exp(-0.00368208 * self.cp)) - 1.0
            return 50.0 + 50.0 * wc
        except OverflowError:
            return 100.0 if self.cp > 0 else 0.0

    def from_mover_view(self, white_to_move: bool) -> int | None:
        """Return cp from the side-to-move's perspective.

        Lichess cloud-eval already returns cp from the side-to-move's
        perspective per their API docs, BUT we normalize internally to
        white-positive in `cp`. So flip for black-to-move.
        """
        if self.cp is None:
            return None
        return self.cp if white_to_move else -self.cp


# ── FEN helpers ──────────────────────────────────────────────────────────────

def _safe_fen_for_eval(fen: str) -> str:
    """Strip the en-passant target square if no pseudo-legal pawn capture
    can reach it. Cloud-eval 404s on FENs whose EP square is "spurious"
    (https://github.com/lichess-org/api/issues/147).
    """
    try:
        board = chess.Board(fen)
    except (ValueError, IndexError):
        return fen
    if board.ep_square is None:
        return fen
    # Is any legal move an en-passant?
    for m in board.legal_moves:
        if board.is_en_passant(m):
            return fen
    # Strip the EP square: replace the field with "-"
    parts = fen.split()
    if len(parts) >= 4:
        parts[3] = "-"
        return " ".join(parts)
    return fen


# ── Cloud-eval client ────────────────────────────────────────────────────────

class LichessAnalysisClient:
    """Async wrapper around Lichess cloud-eval + opening explorer.

    Owns an LRU cache keyed by FEN. The cache is in-process only; survives
    integration reloads only if we persist it (we don't, intentionally — the
    cloud cache is the real cache).
    """

    EVAL_CACHE_SIZE = 256
    OPENING_CACHE_SIZE = 64
    HTTP_TIMEOUT_SEC = 6.0
    HTTP_RETRY_BACKOFF_SEC = 0.5

    def __init__(self, hass, stockfish_bin_dir: Path | str | None = None) -> None:
        self.hass = hass
        # Lazy session — fetched on first call so HA's event loop is up
        self._eval_cache: OrderedDict[str, EvalResult | None] = OrderedDict()
        self._opening_cache: OrderedDict[str, tuple[str | None, str | None]] = OrderedDict()
        # Soft rate-limit: serialize calls to avoid hammering Lichess.
        self._eval_lock = asyncio.Lock()
        # Local Stockfish fallback (used when cloud-eval misses, AND as
        # the AI engine for local-Stockfish-only games).
        self._stockfish: StockfishFallback | None = None
        if stockfish_bin_dir is not None:
            self._stockfish = StockfishFallback(hass, stockfish_bin_dir)

    # ─── Local-game AI moves (Task #16/#17 release-readiness) ─────────────
    #
    # Public API for local Stockfish games. Replaces the duplicate
    # _find_stockfish + _stockfish_best_move pair that previously lived
    # in coordinator.py with hardcoded /config paths and silent auto-chmod.
    # Routes everything through the proper StockfishFallback engine which
    # handles libc-aware download, architecture detection (including ARM),
    # and engine lifecycle.

    # Map ai_level 1-8 → (Stockfish Skill Level 0-20, search depth).
    # Tuned to give comparable strength to Lichess's Stockfish levels so
    # difficulty feels the same whether the user plays online or offline.
    _AI_LEVEL_TABLE: dict[int, tuple[int, int]] = {
        1: (0, 5),    # Skill 0 = weakest standard; depth 5 = very fast
        2: (1, 5),
        3: (2, 5),
        4: (3, 5),
        5: (7, 5),
        6: (11, 8),
        7: (15, 13),
        8: (20, 22),  # Skill 20 = max; depth 22 ≈ master-level
    }

    async def best_move_for_ai_level(
        self, board: "chess.Board", ai_level: int
    ) -> str | None:
        """Return the best UCI move at the given AI difficulty (1-8).

        Uses the shared StockfishFallback engine — no separate subprocess
        spawn. Returns None if Stockfish isn't available (unsupported arch,
        download failed, engine spawn failed). Caller can fall back to
        cloud-eval or a random legal move.
        """
        if self._stockfish is None:
            return None
        engine = await self._stockfish.ensure_engine()
        if engine is None:
            return None
        skill, depth = self._AI_LEVEL_TABLE.get(int(ai_level), self._AI_LEVEL_TABLE[8])
        try:
            await engine.configure({"Skill Level": skill})
        except Exception as cfg_err:
            # Some engine builds reject mid-session reconfigure; safe to log + continue.
            _LOGGER.debug("engine.configure Skill Level=%d failed: %s", skill, cfg_err)
        try:
            result = await engine.play(board, chess.engine.Limit(depth=depth))
        except Exception as play_err:
            _LOGGER.warning("engine.play failed at level %d depth %d: %s",
                            ai_level, depth, play_err)
            return None
        if result is None or result.move is None:
            return None
        return result.move.uci()

    # ─── eval ─────────────────────────────────────────────────────────────

    async def get_eval(self, fen: str, multi_pv: int = 1) -> EvalResult | None:
        """Fetch cloud-eval for a FEN. None on cache miss + 404 (no Stockfish
        fallback wired yet)."""
        safe_fen = _safe_fen_for_eval(fen)
        # Cache key includes multi_pv (different multi_pv = different result).
        cache_key = f"{safe_fen}::{multi_pv}"

        if cache_key in self._eval_cache:
            self._eval_cache.move_to_end(cache_key)
            return self._eval_cache[cache_key]

        async with self._eval_lock:
            # Double-check after acquiring the lock
            if cache_key in self._eval_cache:
                self._eval_cache.move_to_end(cache_key)
                return self._eval_cache[cache_key]

            result = await self._fetch_eval(safe_fen, multi_pv)
            # Fall back to local Stockfish if cloud-eval missed.
            if result is None and self._stockfish is not None:
                _LOGGER.debug(
                    "cloud-eval miss for %s — falling back to local Stockfish",
                    safe_fen,
                )
                result = await self._stockfish.evaluate(safe_fen)
            self._eval_cache[cache_key] = result
            # LRU eviction
            while len(self._eval_cache) > self.EVAL_CACHE_SIZE:
                self._eval_cache.popitem(last=False)
            return result

    async def _fetch_eval(self, fen: str, multi_pv: int) -> EvalResult | None:
        """One HTTP request to /api/cloud-eval. Returns None on 404 or error."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        session = async_get_clientsession(self.hass)
        params = {"fen": fen, "multiPv": str(multi_pv)}
        url = LICHESS_CLOUD_EVAL_URL
        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self.HTTP_TIMEOUT_SEC),
            ) as resp:
                if resp.status == 404:
                    _LOGGER.debug("cloud-eval cache miss for %s", fen)
                    return None
                if resp.status != 200:
                    _LOGGER.debug("cloud-eval HTTP %s for %s", resp.status, fen)
                    return None
                payload = await resp.json()
                return self._parse_eval_payload(payload, fen)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("cloud-eval request failed for %s: %s", fen, err)
            return None

    def _parse_eval_payload(self, payload: dict[str, Any], fen: str) -> EvalResult | None:
        """Parse Lichess cloud-eval JSON. Normalize cp/mate to white-positive."""
        pvs = payload.get("pvs") or []
        if not pvs:
            return None
        top = pvs[0]
        depth = payload.get("depth")
        moves_str = top.get("moves", "")
        best_uci = moves_str.split(" ")[0] if moves_str else None

        # Lichess returns cp/mate from the side-to-move's perspective.
        # Normalize to white-positive: flip sign when black is to move.
        # Parse the side-to-move robustly — a board-only FEN (no fields)
        # would IndexError on a naive split, so fall back to "white".
        fen_fields = fen.split(" ")
        white_to_move = len(fen_fields) < 2 or fen_fields[1] != "b"
        cp = top.get("cp")
        mate = top.get("mate")
        if cp is not None and not white_to_move:
            cp = -cp
        if mate is not None and not white_to_move:
            mate = -mate

        return EvalResult(
            cp=cp,
            mate=mate,
            depth=depth,
            best_uci=best_uci,
            source="lichess-cloud",
            raw=payload,
        )

    # ─── opening explorer ─────────────────────────────────────────────────

    async def get_opening(self, fen: str) -> tuple[str | None, str | None]:
        """Return (name, eco) from the masters explorer. Empty when out of book."""
        # Cache aggressively — openings are deterministic per position.
        if fen in self._opening_cache:
            self._opening_cache.move_to_end(fen)
            return self._opening_cache[fen]

        result = await self._fetch_opening(fen)
        self._opening_cache[fen] = result
        while len(self._opening_cache) > self.OPENING_CACHE_SIZE:
            self._opening_cache.popitem(last=False)
        return result

    async def shutdown(self) -> None:
        """Release resources (Stockfish process). Called on integration unload."""
        if self._stockfish is not None:
            await self._stockfish.shutdown()

    async def _fetch_opening(self, fen: str) -> tuple[str | None, str | None]:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        session = async_get_clientsession(self.hass)
        params = {"fen": fen, "moves": "0", "topGames": "0"}
        try:
            async with session.get(
                LICHESS_OPENING_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self.HTTP_TIMEOUT_SEC),
            ) as resp:
                if resp.status != 200:
                    return (None, None)
                payload = await resp.json()
                op = payload.get("opening")
                if not op:
                    return (None, None)
                return (op.get("name"), op.get("eco"))
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("opening explorer failed for %s: %s", fen, err)
            return (None, None)


# ── Stockfish local fallback ─────────────────────────────────────────────────
#
# Cloud-eval covers popular positions cached by Lichess; offbeat beginner
# middlegame positions miss the cache and return 404. Stockfish-on-HA fills
# the gap: when cloud-eval returns None, we feed the FEN to a locally-running
# Stockfish 18 process and synthesize the same EvalResult shape.
#
# Binary source is selected at runtime based on the C library family:
#   musl  (HA OS Core container is Alpine-based) → Alpine apk from
#     edge/testing repo (ld-musl-x86_64.so.1)
#   glibc (some Debian-based HA installs)        → Official sf_18 release
#     from GitHub (ld-linux-x86-64.so.2)
# The binary is cached at /config/phantom_chess/bin/engine after extraction.
# Detection is by `/lib/ld-musl-*` existence — if it's there we're on musl,
# otherwise assume glibc. The wrong choice surfaces as
# "No such file or directory" on engine spawn (ENOENT from execve when the
# kernel can't find the dynamic linker the binary asks for).

# Official Stockfish release (glibc-linked).
STOCKFISH_RELEASE_TAG = "sf_18"
STOCKFISH_GLIBC_BASE = (
    "https://github.com/official-stockfish/Stockfish/releases/download/"
    + STOCKFISH_RELEASE_TAG
)

# Alpine packages (musl-linked). Pinned to the version that ships with sf_18.
# edge/testing is the bleeding-edge branch; if the version moves we'll need
# to bump these strings.
STOCKFISH_MUSL_X86_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/edge/testing/x86_64/"
    "stockfish-18-r0.apk"
)
STOCKFISH_MUSL_ARM_URL = (
    "https://dl-cdn.alpinelinux.org/alpine/edge/testing/aarch64/"
    "stockfish-18-r0.apk"
)
# Path of the binary inside the extracted Alpine apk tar tree.
STOCKFISH_MUSL_INNER_PATH = "usr/bin/stockfish"

# (libc, machine) → (URL, "inner path" of binary inside the archive)
# inner_path of None means the official-release tar layout (top-level
# directory "stockfish/" containing the binary file).
#
# ARM (aarch64) coverage added 2026-05-16 (Task #17) — HA on Raspberry Pi
# 4/5, HA Yellow, HA Green is aarch64, which is the majority of HA installs.
# Without these entries the integration silently failed local Stockfish on
# those platforms.
STOCKFISH_ASSET_MAP: dict[tuple[str, str], tuple[str, str | None]] = {
    # x86_64 / amd64 ─────────────────────────────────────────────────────
    ("musl",  "x86_64"): (STOCKFISH_MUSL_X86_URL, STOCKFISH_MUSL_INNER_PATH),
    ("musl",  "amd64"):  (STOCKFISH_MUSL_X86_URL, STOCKFISH_MUSL_INNER_PATH),
    ("glibc", "x86_64"): (
        f"{STOCKFISH_GLIBC_BASE}/stockfish-ubuntu-x86-64-avx2.tar", None
    ),
    ("glibc", "amd64"): (
        f"{STOCKFISH_GLIBC_BASE}/stockfish-ubuntu-x86-64-avx2.tar", None
    ),
    # aarch64 / arm64 ────────────────────────────────────────────────────
    # musl: Alpine edge/testing apk (mirrors the x86 packaging layout).
    ("musl",  "aarch64"): (STOCKFISH_MUSL_ARM_URL, STOCKFISH_MUSL_INNER_PATH),
    ("musl",  "arm64"):   (STOCKFISH_MUSL_ARM_URL, STOCKFISH_MUSL_INNER_PATH),
    # glibc: official release ships an ARM build (no AVX variants — ARM
    # doesn't have AVX). The binary is named simply 'stockfish' in the tar.
    ("glibc", "aarch64"): (
        f"{STOCKFISH_GLIBC_BASE}/stockfish-ubuntu-arm64.tar", None
    ),
    ("glibc", "arm64"): (
        f"{STOCKFISH_GLIBC_BASE}/stockfish-ubuntu-arm64.tar", None
    ),
}


def _detect_libc() -> str:
    """Return 'musl' or 'glibc' based on what dynamic linker is present.

    Checks both x86_64 and aarch64 musl linker paths so this works on
    Raspberry Pi (aarch64) running HA OS (Alpine/musl) too.
    """
    musl_linkers = (
        "/lib/ld-musl-x86_64.so.1",
        "/lib/ld-musl-aarch64.so.1",
        "/lib/ld-musl-armhf.so.1",  # 32-bit ARM, less common but possible
    )
    if any(os.path.exists(p) for p in musl_linkers):
        return "musl"
    return "glibc"


class StockfishFallback:
    """Local Stockfish process used as a fallback when cloud-eval misses.

    Lifecycle:
    - First evaluate() call triggers binary download + engine spawn
    - Engine persists across calls (popen overhead is significant)
    - shutdown() cleanly quits the engine; called on integration unload

    Thread/depth defaults are conservative: 1 thread, depth 18, ~1.5s/move.
    Tweaks should respect Luke's HA host's CPU budget (he's on a generic-x86-64
    box per ha_get_system_health — plenty of headroom).
    """

    DEPTH = 18
    TIME_LIMIT_SEC = 1.5
    DOWNLOAD_TIMEOUT_SEC = 60.0

    def __init__(self, hass, bin_dir: Path | str) -> None:
        self.hass = hass
        self.bin_dir = Path(bin_dir)
        self.binary_path: Path | None = None
        # Concrete type unknown at class init (loop.subprocess_exec
        # returns asyncio.SubprocessTransport when called); Any is the
        # least-friction annotation because we only call .close() on it.
        self._transport: Any = None
        self._engine: chess.engine.UciProtocol | None = None
        self._init_lock = asyncio.Lock()
        self._eval_lock = asyncio.Lock()
        self._unsupported_arch_warned = False
        self._available = True  # Flips False on permanent failure

    async def ensure_engine(self) -> chess.engine.UciProtocol | None:
        """Return a live UCI engine, or None if Stockfish isn't usable.

        Caches the engine across calls — first call pays the popen cost,
        subsequent calls reuse the running process.
        """
        if not self._available:
            return None
        if self._engine is not None:
            return self._engine
        async with self._init_lock:
            if self._engine is not None:
                return self._engine
            if not await self._ensure_binary():
                self._available = False
                return None
            try:
                transport, engine = await chess.engine.popen_uci(
                    str(self.binary_path)
                )
                # Reasonable defaults — Threads=1 avoids hammering the HA host.
                try:
                    await engine.configure({"Threads": 1, "Hash": 32})
                except Exception:
                    pass  # Some engine builds reject Configure mid-init; ignore.
                self._transport = transport
                self._engine = engine
                _LOGGER.warning(
                    "Stockfish engine started from %s", self.binary_path
                )
                return engine
            except Exception as err:
                _LOGGER.warning("Stockfish engine spawn failed: %s", err)
                self._available = False
                return None

    async def _ensure_binary(self) -> bool:
        """Verify the binary exists at self.binary_path; download if not.

        Returns True if a usable binary is now present.

        Notes on layout:
          The official Stockfish release tar extracts to a directory called
          "stockfish/" (containing the binary, AUTHORS, etc.). We can't name
          our canonical binary "stockfish" because that name collides with
          the extraction directory. Use "engine" as the canonical filename
          inside bin_dir/.
        """
        target = self.bin_dir / "engine"
        if target.exists() and os.access(target, os.X_OK):
            self.binary_path = target
            return True

        libc = _detect_libc()
        machine = platform.machine().lower()
        choice = STOCKFISH_ASSET_MAP.get((libc, machine))
        if choice is None:
            if not self._unsupported_arch_warned:
                _LOGGER.warning(
                    "Stockfish fallback unavailable: no binary for libc=%s arch=%s. "
                    "Cloud-eval misses will surface as 'unknown' classifications.",
                    libc, machine,
                )
                self._unsupported_arch_warned = True
            return False
        url, inner_path = choice

        _LOGGER.warning("Stockfish: downloading %s → %s (libc=%s)", url, target, libc)

        try:
            self.bin_dir.mkdir(parents=True, exist_ok=True)
        except Exception as err:
            _LOGGER.warning("Stockfish: cannot create %s: %s", self.bin_dir, err)
            return False

        # Prepare a temp filename (synchronous filename allocation is fine).
        tmp_path = self.bin_dir / f"download-{os.getpid()}.tar"

        # Download to memory, then write to disk in an executor to avoid
        # blocking the event loop. Stockfish tar is ~3 MB so the in-memory
        # buffer is small.
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=self.DOWNLOAD_TIMEOUT_SEC),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "Stockfish download HTTP %s for %s", resp.status, url
                    )
                    return False
                data = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Stockfish download failed: %s", err)
            return False

        def _write_and_extract(payload: bytes, inner: str | None) -> Path | None:
            try:
                # Write archive to disk
                with open(tmp_path, "wb") as fp:
                    fp.write(payload)
                # Extract. Both Alpine .apk and official .tar are gzipped
                # tarballs; tarfile auto-detects compression.
                with tarfile.open(tmp_path) as tar:
                    if inner is not None:
                        # Targeted extraction: we know the exact path.
                        try:
                            member = tar.getmember(inner)
                        except KeyError:
                            _LOGGER.warning(
                                "Stockfish: %s not found in %s",
                                inner, tmp_path,
                            )
                            return None
                        # filter="data" strips absolute paths / traversal /
                        # device members (safe-extraction hardening; also the
                        # Python 3.14 default — silences the 3.12+
                        # DeprecationWarning about unset filters).
                        tar.extract(member, self.bin_dir, filter="data")
                        return self.bin_dir / inner
                    # Bulk extraction (official-release layout):
                    # tar contains a top-level "stockfish/" directory.
                    tar.extractall(self.bin_dir, filter="data")
            except Exception as err:
                _LOGGER.warning("Stockfish write/extract failed: %s", err)
                return None
            finally:
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            # Find the binary in the extracted "stockfish/" subdir.
            extracted_dir = self.bin_dir / "stockfish"
            if extracted_dir.is_dir():
                for candidate in extracted_dir.iterdir():
                    if (
                        candidate.is_file()
                        and candidate.name.startswith("stockfish")
                        and not candidate.name.endswith(".txt")
                    ):
                        return candidate
            # Fallback: scan bin_dir directly
            for candidate in self.bin_dir.iterdir():
                if (
                    candidate.is_file()
                    and candidate.name.startswith("stockfish-")
                ):
                    return candidate
            return None

        found = await self.hass.async_add_executor_job(
            _write_and_extract, data, inner_path
        )
        if not found:
            _LOGGER.warning(
                "Stockfish: extracted but no binary found in %s", self.bin_dir
            )
            return False

        # Move/rename to canonical path and chmod +x. Sync ops, but small,
        # so we run them in executor too for cleanliness.
        def _install_binary(src: Path, dst: Path) -> bool:
            try:
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
                dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                # Clean up the now-empty release directory if possible
                try:
                    src.parent.rmdir()
                except OSError:
                    pass  # not empty — leave it
                return True
            except Exception as err:
                _LOGGER.warning("Stockfish: install (rename/chmod) failed: %s", err)
                return False

        ok = await self.hass.async_add_executor_job(_install_binary, found, target)
        if not ok:
            return False

        self.binary_path = target
        _LOGGER.warning("Stockfish: installed at %s", target)
        return True

    async def evaluate(self, fen: str) -> EvalResult | None:
        """Analyze a FEN and return an EvalResult (white-positive)."""
        engine = await self.ensure_engine()
        if engine is None:
            return None
        try:
            board = chess.Board(fen)
        except (ValueError, IndexError):
            return None
        async with self._eval_lock:
            try:
                info = await engine.analyse(
                    board,
                    chess.engine.Limit(depth=self.DEPTH, time=self.TIME_LIMIT_SEC),
                )
            except chess.engine.EngineError as err:
                _LOGGER.debug("Stockfish analyse error for %s: %s", fen, err)
                return None
            except Exception as err:
                _LOGGER.warning("Stockfish analyse unexpected error: %s", err)
                # Engine may be dead; force respawn on next call.
                await self.shutdown()
                return None

        score = info.get("score")
        if score is None:
            return None
        # Normalize to white-positive (the contract of EvalResult.cp / .mate).
        pov_white = score.white()
        cp: int | None = None
        mate: int | None = None
        if pov_white.is_mate():
            mate = pov_white.mate()
        else:
            cp = pov_white.score(mate_score=10000)

        pv = info.get("pv") or []
        best_uci = pv[0].uci() if pv else None
        return EvalResult(
            cp=cp,
            mate=mate,
            depth=info.get("depth"),
            best_uci=best_uci,
            source="stockfish-local",
            raw=None,
        )

    async def shutdown(self) -> None:
        """Cleanly stop the engine. Idempotent."""
        if self._engine is not None:
            try:
                await self._engine.quit()
            except Exception:
                pass
            self._engine = None
            self._transport = None


# ── Move classification ──────────────────────────────────────────────────────

def classify_move(
    pre_eval: EvalResult | None,
    post_eval: EvalResult | None,
    move_uci: str,
    mover_is_white: bool,
) -> tuple[str, int]:
    """Return (classification, cpl) for a move.

    Convention:
    - All cp values are white-positive (already normalized).
    - For a white move, mover's gain = post_cp - pre_cp; loss = -gain.
    - For a black move, mover's gain = pre_cp - post_cp; loss = -gain.
    - CPL is clamped to ≥0. Gains (negative CPL) are mapped to 0 — a "good"
      move doesn't lose material so it gets the best/good label depending on
      whether it matched the engine's top recommendation.
    """
    if pre_eval is None or post_eval is None:
        return (CLASSIFICATION_UNKNOWN, 0)

    # Mate handling — treat mate-in-N positions as ±10000 cp from white's view.
    def cp_or_mate(ev: EvalResult) -> int:
        if ev.mate is not None:
            return 10000 if ev.mate > 0 else -10000
        return ev.cp if ev.cp is not None else 0

    pre_cp = cp_or_mate(pre_eval)
    post_cp = cp_or_mate(post_eval)

    if mover_is_white:
        loss = pre_cp - post_cp
    else:
        loss = post_cp - pre_cp  # black loss = -(black gain)

    # Clamp loss for classification (large mate swings are still huge blunders
    # but the numeric label gets messy).
    cpl = max(0, min(loss, 9999))

    # If this move matched the engine's pre-move top choice → best
    if pre_eval.best_uci and move_uci == pre_eval.best_uci:
        return (CLASSIFICATION_BEST, cpl)

    if cpl < CPL_GOOD_MAX:
        return (CLASSIFICATION_GOOD, cpl)
    if cpl < CPL_INACCURACY_MAX:
        return (CLASSIFICATION_INACCURACY, cpl)
    if cpl < CPL_MISTAKE_MAX:
        return (CLASSIFICATION_MISTAKE, cpl)
    return (CLASSIFICATION_BLUNDER, cpl)


# Color + glyph palette for classification badges. The dashboard reads these
# off each move-history entry so the styling is centralized here.
CLASSIFICATION_DISPLAY = {
    "brilliant":  ("#00bcd4", "!!"),
    CLASSIFICATION_BEST:       ("#4caf50", "✓"),
    CLASSIFICATION_GOOD:       ("#8bc34a", "!"),
    "book":       ("#795548", "📖"),
    CLASSIFICATION_INACCURACY: ("#ffc107", "?!"),
    CLASSIFICATION_MISTAKE:    ("#ff9800", "?"),
    CLASSIFICATION_BLUNDER:    ("#f44336", "??"),
    CLASSIFICATION_UNKNOWN:    ("#9e9e9e", "…"),
}


def classification_color_glyph(classification: str) -> tuple[str, str]:
    return CLASSIFICATION_DISPLAY.get(
        classification, CLASSIFICATION_DISPLAY[CLASSIFICATION_UNKNOWN]
    )


# ── Tactical-motif detection ─────────────────────────────────────────────────

# Piece values used for fork detection (centipawns, classical).
_PIECE_VALUE = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 10000,
}


def detect_fork(board_before_move: chess.Board, move: chess.Move) -> bool:
    """True if `move` creates a fork: the moved piece, on its destination,
    attacks ≥2 opponent pieces of equal-or-greater value than itself.

    `board_before_move` is the position with the mover still to play. We
    apply the move on a copy, look at what the moved piece now attacks, and
    count valuable enemy targets.
    """
    if move not in board_before_move.legal_moves:
        return False
    b = board_before_move.copy(stack=False)
    moving_piece = b.piece_at(move.from_square)
    if moving_piece is None:
        return False
    b.push(move)
    attacker_value = _PIECE_VALUE.get(moving_piece.piece_type, 0)
    targets = 0
    enemy = not moving_piece.color
    # Squares attacked by the moved piece from its new square.
    attacked = b.attacks(move.to_square)
    for sq in attacked:
        target = b.piece_at(sq)
        if target is None or target.color != enemy:
            continue
        target_value = _PIECE_VALUE.get(target.piece_type, 0)
        # Kings count as a fork target only paired with another piece.
        # Equal-or-greater value than the attacker is the canonical
        # fork (you win material on at least one of the two captures).
        if target_value >= attacker_value:
            targets += 1
    return targets >= 2


# ── Threat detector ──────────────────────────────────────────────────────────

async def compute_threat_san(
    board: chess.Board,
    client: LichessAnalysisClient,
) -> str | None:
    """If side-to-move could move now AND its best move would be a capture
    or deliver mate, return that move's SAN. Otherwise return None.

    Used to warn the user before their turn: "Black threatens Nxe5".
    The "as if side-to-move could move now" framing means we DON'T swap
    side-to-move; we use the existing FEN. (The board already has the
    side-to-move flipped after our move pushed.)
    """
    if board.is_game_over():
        return None
    fen = board.fen()
    ev = await client.get_eval(fen)
    if ev is None or ev.best_uci is None:
        return None
    try:
        move = chess.Move.from_uci(ev.best_uci)
    except ValueError:
        return None
    if move not in board.legal_moves:
        return None
    # Only surface as a "threat" if it's noisy: capture or mate.
    is_capture = board.is_capture(move)
    is_mate = ev.mate is not None and abs(ev.mate) <= 2
    if not (is_capture or is_mate):
        return None
    try:
        return board.san(move)
    except (chess.IllegalMoveError, AssertionError):
        return None


# ── Lichess accuracy formula ─────────────────────────────────────────────────

def _win_pct_loss_to_accuracy(loss_pct: float) -> float:
    """Per-move accuracy from win-percentage loss.

    Lichess formula: accuracy = 103.1668 * exp(-0.04354 * winLoss) - 3.1669
    Clamped to 0-100.
    """
    loss_pct = max(0.0, loss_pct)
    try:
        a = 103.1668 * math.exp(-0.04354 * loss_pct) - 3.1669
    except OverflowError:
        return 0.0
    return max(0.0, min(100.0, a))


def compute_game_accuracy(
    pre_evals: list[EvalResult | None],
    post_evals: list[EvalResult | None],
    side_movers: list[bool],
) -> tuple[float | None, float | None]:
    """Return (white_accuracy, black_accuracy) percentages.

    `pre_evals[i]` / `post_evals[i]` describe the i-th move; `side_movers[i]`
    is True for a white move, False for black. Per-move accuracy uses the
    Lichess formula above; per-color accuracy is the arithmetic mean of that
    color's per-move accuracies.

    Returns (None, None) if data is too sparse.
    """
    white_acc: list[float] = []
    black_acc: list[float] = []
    for pre, post, mover in zip(pre_evals, post_evals, side_movers):
        if pre is None or post is None:
            continue
        # Win-pct loss from the mover's perspective.
        pre_pct = pre.white_win_pct
        post_pct = post.white_win_pct
        if mover:  # white
            loss = pre_pct - post_pct
            a = _win_pct_loss_to_accuracy(loss)
            white_acc.append(a)
        else:
            loss = post_pct - pre_pct  # black wants white_pct to fall
            a = _win_pct_loss_to_accuracy(loss)
            black_acc.append(a)

    w = sum(white_acc) / len(white_acc) if white_acc else None
    b = sum(black_acc) / len(black_acc) if black_acc else None
    return (
        round(w, 1) if w is not None else None,
        round(b, 1) if b is not None else None,
    )

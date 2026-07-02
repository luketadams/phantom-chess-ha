"""Reusable BLE mock layer + coordinator harness for Phantom Chess tests.

The coordinator's BLE surface is bleak: it constructs a ``BleakClient`` as an
async context manager, iterates ``client.services`` for GATT discovery,
``read_gatt_char`` / ``write_gatt_char`` for I/O, and ``start_notify`` to wire
notification callbacks. This module provides drop-in fakes for all of that so
coordinator coroutines can be exercised with no hardware and no real event
loop timers.

Design goals:

* **Faithful to the opcode map** (see phantom-chess-technical): notifications
  are injected via :meth:`FakeBleakClient.notify`, which invokes the exact
  callback the coordinator registered for a UUID, mirroring how bleak delivers
  a GATT notification.
* **Observable writes**: every ``write_gatt_char`` is appended to
  ``client.writes`` as ``(uuid, bytes, response)`` so tests can assert the
  wire bytes (opcode + payload) the coordinator emitted.
* **Injectable failures**: ``client.fail_write`` / ``client.fail_read`` let a
  test raise a chosen exception from a given UUID to drive the error paths
  (0x0D length rejection, GATT staleness, disconnects).
* **No DataUpdateCoordinator scheduling**: :func:`make_coordinator` builds the
  coordinator via ``__new__`` and replicates ``__init__``'s attribute set,
  bypassing ``super().__init__`` (which would schedule refreshes and demand a
  full HA instance). This matches the existing ``test_coordinator_state``
  convention.

Everything here is import-clean in both the minimal matrix-tests env (bleak
absent) and the ha-tests env (bleak installed) — nothing imports bleak.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable
from unittest.mock import MagicMock

import chess

from custom_components.phantom_chess import const
from custom_components.phantom_chess.coordinator import PhantomChessCoordinator


# ── GATT object fakes ───────────────────────────────────────────────────────


class FakeGATTCharacteristic:
    """Mimics ``bleak.backends.characteristic.BleakGATTCharacteristic``."""

    def __init__(
        self,
        uuid: str,
        properties: list[str] | None = None,
        max_write_without_response_size: int = 512,
    ) -> None:
        self.uuid = uuid
        self.properties = properties or ["read", "write", "notify"]
        self.max_write_without_response_size = max_write_without_response_size


class FakeGATTService:
    def __init__(self, uuid: str, characteristics: list[FakeGATTCharacteristic]) -> None:
        self.uuid = uuid
        self.characteristics = characteristics


class FakeServices:
    """A ``client.services`` stand-in: iterable of services + lookup."""

    def __init__(self, services: list[FakeGATTService]) -> None:
        self._services = services

    def __iter__(self):
        return iter(self._services)

    def get_characteristic(self, uuid: str) -> FakeGATTCharacteristic | None:
        for service in self._services:
            for char in service.characteristics:
                if char.uuid.lower() == uuid.lower():
                    return char
        return None


# The full set of characteristics a fw0.3.0/0.3.2 Phantom board exposes that
# the coordinator's discovery + subscribe paths care about. Kept as (uuid,
# props) tuples so a test can trim/extend to model older/degraded firmware.
DEFAULT_CHAR_SPECS: list[tuple[str, list[str]]] = [
    (const.UUID_VERSION, ["read"]),
    (const.UUID_STATUS_BOARD, ["notify"]),
    (const.UUID_BATTERY_INFO, ["read", "notify"]),
    (const.UUID_ERROR_MSG, ["notify"]),
    (const.UUID_SEND_MATRIX, ["read", "notify"]),
    (const.UUID_FIRMWARE_STATE, ["read", "notify"]),
    (const.UUID_GAME, ["read", "write", "notify"]),
    (const.UUID_SCULPTURE, ["read", "write", "notify"]),
    (const.UUID_SELECT_MODE, ["write"]),
    (const.UUID_MECHANISM_SPEED, ["write"]),
    (const.UUID_SOUND_LEVEL, ["write"]),
    (const.UUID_BOARD_ROTATION, ["write"]),
    (const.UUID_PAUSE, ["write"]),
]


def default_services() -> FakeServices:
    """A realistic single-service Phantom GATT layout."""
    chars = [
        FakeGATTCharacteristic(uuid, props) for uuid, props in DEFAULT_CHAR_SPECS
    ]
    return FakeServices([FakeGATTService(const.BLE_SERVICE_UUID, chars)])


class FakeBleakClient:
    """Async-context-manager BleakClient stand-in.

    Usage mirrors the coordinator::

        async with FakeBleakClient(device, disconnected_callback=cb) as client:
            await client.write_gatt_char(uuid, b"...")
            client.notify(uuid, b"payload")   # deliver a notification
    """

    def __init__(
        self,
        device: Any = None,
        disconnected_callback: Callable | None = None,
        *,
        services: FakeServices | None = None,
        read_values: dict[str, bytes] | None = None,
        mtu_size: int = 247,
    ) -> None:
        self.address = getattr(device, "address", "C8:C9:A3:F2:7C:0A")
        self._disconnected_callback = disconnected_callback
        self.services = services or default_services()
        self._read_values = {k.lower(): v for k, v in (read_values or {}).items()}
        self.mtu_size = mtu_size
        self.is_connected = False

        # Observability + injection.
        self.writes: list[tuple[str, bytes, bool]] = []
        self.notify_callbacks: dict[str, Callable] = {}
        self.started_notifies: list[str] = []
        self.stopped_notifies: list[str] = []
        # uuid(lower) -> Exception to raise on that op.
        self.fail_write: dict[str, BaseException] = {}
        self.fail_read: dict[str, BaseException] = {}
        self.fail_start_notify: dict[str, BaseException] = {}

    # -- context manager --------------------------------------------------
    async def __aenter__(self) -> "FakeBleakClient":
        self.is_connected = True
        return self

    async def __aexit__(self, *exc) -> bool:
        self.is_connected = False
        return False

    async def connect(self) -> bool:
        self.is_connected = True
        return True

    async def disconnect(self) -> bool:
        self.is_connected = False
        if self._disconnected_callback:
            self._disconnected_callback(self)
        return True

    # -- GATT I/O ---------------------------------------------------------
    async def read_gatt_char(self, uuid: str) -> bytes:
        key = uuid.lower()
        if key in self.fail_read:
            raise self.fail_read[key]
        return self._read_values.get(key, b"")

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = True) -> None:
        key = uuid.lower()
        self.writes.append((uuid, bytes(data), response))
        if key in self.fail_write:
            raise self.fail_write[key]

    async def start_notify(self, uuid: str, callback: Callable) -> None:
        key = uuid.lower()
        if key in self.fail_start_notify:
            raise self.fail_start_notify[key]
        self.notify_callbacks[key] = callback
        self.started_notifies.append(uuid)

    async def stop_notify(self, uuid: str) -> None:
        self.stopped_notifies.append(uuid)
        self.notify_callbacks.pop(uuid.lower(), None)

    # -- test helper ------------------------------------------------------
    def notify(self, uuid: str, data: bytes) -> None:
        """Deliver a GATT notification to the registered callback."""
        cb = self.notify_callbacks.get(uuid.lower())
        if cb is None:
            raise KeyError(f"no notify callback registered for {uuid}")
        cb(FakeGATTCharacteristic(uuid), bytearray(data))

    def writes_to(self, uuid: str) -> list[bytes]:
        """All payload bytes written to ``uuid`` (in order)."""
        return [w[1] for w in self.writes if w[0].lower() == uuid.lower()]

    def last_write_to(self, uuid: str) -> bytes | None:
        payloads = self.writes_to(uuid)
        return payloads[-1] if payloads else None


# ── coordinator factory ─────────────────────────────────────────────────────


def make_hass(loop: asyncio.AbstractEventLoop | None = None) -> MagicMock:
    """A MagicMock hass whose ``.loop`` is a real event loop.

    Coordinator coroutines use ``hass.loop.create_future``/``.time``/
    ``.call_soon_threadsafe``/``.create_task`` — those must be a genuine loop.
    ``async_add_executor_job`` is wired to run the function inline in a thread
    executor via the real loop so debug-dump / blocking-read paths work.
    """
    hass = MagicMock()
    hass.loop = loop or asyncio.get_event_loop()

    async def _exec(func, *args):
        return func(*args)

    hass.async_add_executor_job = MagicMock(side_effect=lambda func, *a: _exec(func, *a))
    return hass


def make_coordinator(
    *,
    hass: MagicMock | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    ble_connected: bool = True,
    client: FakeBleakClient | None = None,
    entry: Any = None,
) -> PhantomChessCoordinator:
    """Build a coordinator with a full attribute set, bypassing HA scheduling.

    Mirrors ``PhantomChessCoordinator.__init__`` attribute-for-attribute but
    skips ``DataUpdateCoordinator.__init__`` (no refresh scheduling, no HA
    instance required). ``async_set_updated_data`` and ``async_update_listeners``
    are stubbed as MagicMocks so notification handlers can push state freely.
    """
    c = PhantomChessCoordinator.__new__(PhantomChessCoordinator)
    c.hass = hass or make_hass(loop)
    c._entry = entry

    # Keep a provided fake client's connection flag in step with the
    # coordinator's ``ble_connected`` so the senders' is_connected guard
    # passes without every test remembering to enter the client CM.
    if client is not None:
        client.is_connected = ble_connected
    c._ble_address = "C8:C9:A3:F2:7C:0A"
    c._lichess_token = "test-token"

    # BLE
    c._ble_client = client
    c._ble_task = None
    c._matrix_poll_task = None
    c._ble_connected = ble_connected

    # Lichess
    c._lichess_task = None
    c._game_id = None
    c._our_color = None
    c._processed_moves = 0

    # boards
    c._board = chess.Board()
    c._analysis_board = chess.Board()
    c._analysis_client = None

    # game settings
    c.ai_level = 3
    c.player_color = "random"
    c.setup_mode = const.DEFAULT_SETUP_MODE
    c.selected_sculpture = const.DEFAULT_SCULPTURE_GAME
    c.training_wheels = const.DEFAULT_TRAINING_WHEELS
    c.voice_announcements = const.DEFAULT_VOICE_ANNOUNCEMENTS
    c.lichess_clock_minutes = const.DEFAULT_LICHESS_CLOCK_MINUTES
    c.lichess_clock_increment = const.DEFAULT_LICHESS_CLOCK_INCREMENT
    c.white_ai_level = 3
    c.black_ai_level = 3
    c.ai_vs_ai_move_delay = 1.5
    c.mechanism_speed = 3
    c.sound_level = 16
    c.paused = False

    # queues / events
    c._physical_move_queue = asyncio.Queue()
    c._stop_event = asyncio.Event()

    # local / two-player / ai-vs-ai / sculpture flags
    c._local_game_active = False
    c._two_player_active = False
    c._local_game_task = None
    c._ai_vs_ai_active = False
    c._ai_vs_ai_white_level = 3
    c._ai_vs_ai_black_level = 3
    c._ai_vs_ai_move_delay = 1.5
    c._sculpture_active = False
    c._sculpture_move_delay = 2.0
    c._sculpture_games_cache = None
    c._local_game_task_lock = None

    # echo suppression
    c._expecting_ai_echo_until = 0.0
    c._activation_settle_until = 0.0
    c._last_ai_uci = None
    c._last_ai_uci_rotated = None
    c._last_ai_uci_set_at = 0.0
    c._last_ai_echo_ucis = set()

    # snapshot protocol
    c._phantom_session_initialized = False
    c._move_done_future = None
    c._last_target_fen = None
    c._last_mismatch_signature = None
    c._discovered_uuids = set()
    c._game_start_diag_logged = False
    c._subscribe_degraded_logged = set()

    # state
    c._state = c._blank_state()

    # DataUpdateCoordinator API stubs
    c.async_set_updated_data = MagicMock()
    c.async_update_listeners = MagicMock()

    return c

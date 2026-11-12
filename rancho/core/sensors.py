"""Z-Wave door/window sensors, through zwave.service (Z-Wave JS Server on localhost).

The server owns the ZAC93 and keeps the network running; this is one of its
clients, like `task zwave`. Battery sensors sleep and only transmit when they
open, close, are tampered with, or wake up on their interval.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rancho.core.config import Config, SensorConfig
from rancho.core.events import (
    EventBus, SensorClosed, SensorHeard, SensorOpened, SensorSilent, SensorTamper, ZwaveOffline, ZwaveOnline,
)

if TYPE_CHECKING:
    from zwave_js_server.model.controller import Controller
    from zwave_js_server.model.node import Node

logger = logging.getLogger(__name__)
URL = "ws://127.0.0.1:3000"
RECONNECT_DELAY = 5
CHECK_INTERVAL = 60
# Seconds without the server before alerting; it needs ~20 s to start.
OFFLINE_AFTER = 60
# Silent after this many missed wake-ups (the Ecolink wakes every 4 h), plus a margin.
MISSED_WAKEUPS = 2
SILENT_MARGIN = 600
DEFAULT_WAKEUP = 4 * 3600

NOTIFICATION_CC = 113
BINARY_SENSOR_CC = 48
BATTERY_CC = 128
WAKE_UP_CC = 132
DOOR_OPEN, DOOR_CLOSED = 22, 23
COVER_REMOVED = 3


@dataclass(frozen=True)
class SensorStatus:
    sensor: str
    # None until the sensor reports or the server's cached state is read.
    open: bool | None
    tampered: bool
    battery: int | None
    silent: bool
    last_seen: datetime | None


class _Sensor:
    def __init__(self, config: SensorConfig) -> None:
        self.name = config.name
        self.node_id = config.node
        self.node: Node | None = None
        self.open: bool | None = None
        self.tampered: bool | None = None
        self.battery: int | None = None
        self.wakeup = DEFAULT_WAKEUP
        self.last_seen: datetime | None = None
        # Monotonic time last heard, or when watching began; None until first attached.
        self.heard_at: float | None = None
        self.silent = False

    @property
    def silent_after(self) -> float:
        return MISSED_WAKEUPS * self.wakeup + SILENT_MARGIN


class Sensors:
    def __init__(
        self,
        config: Config,
        events: EventBus,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._events = events
        self._clock = clock
        self._now = now
        self._sensors = [_Sensor(sensor) for sensor in config.sensors]
        # None until the first connection attempt.
        self.online: bool | None = None
        self.down_at: datetime | None = None
        # Counting from start, so a server that never comes up is reported too.
        self._down_since: float | None = clock()
        self._down_at = now()
        self._offline_reported = False
        self._unsubscribe: list[Callable[[], None]] = []

    @property
    def configured(self) -> bool:
        return bool(self._sensors)

    def open_sensors(self) -> list[str]:
        """Names of sensors known to be open; unknown state does not block arming."""
        return [sensor.name for sensor in self._sensors if sensor.open]

    def status(self) -> list[SensorStatus]:
        return [
            SensorStatus(
                sensor.name, sensor.open, bool(sensor.tampered), sensor.battery, sensor.silent, sensor.last_seen,
            )
            for sensor in self._sensors
        ]

    async def run(self) -> None:
        if not self._sensors:
            logger.info("Sensors disabled: no sensors in config.yaml")
            await asyncio.Future()
            return
        # Keep the library quiet; it logs every connection attempt.
        logging.getLogger("zwave_js_server").setLevel(logging.WARNING)
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._connect_loop(), name="zwave-connection")
            tasks.create_task(self._monitor(), name="zwave-monitor")

    async def _connect_loop(self) -> None:
        import aiohttp
        from zwave_js_server.exceptions import BaseZwaveJSServerError

        while True:
            try:
                await self._session()
            except (BaseZwaveJSServerError, aiohttp.ClientError, OSError) as exc:
                if self.online is not False:
                    logger.warning("Z-Wave server unreachable (%s); retrying every %ds",
                                   type(exc).__name__, RECONNECT_DELAY)
            else:
                logger.warning("Z-Wave server closed the connection")
            self._detach()
            await asyncio.sleep(RECONNECT_DELAY)

    async def _session(self) -> None:
        import aiohttp
        from zwave_js_server.client import Client

        async with aiohttp.ClientSession() as http:
            client = Client(URL, http)
            await client.connect()
            ready = asyncio.Event()
            listening = asyncio.create_task(client.listen(ready))
            try:
                waiting = asyncio.create_task(ready.wait())
                await asyncio.wait({listening, waiting}, return_when=asyncio.FIRST_COMPLETED)
                waiting.cancel()
                if ready.is_set():
                    assert client.driver is not None
                    self._attach(client.driver.controller)
                await listening
            finally:
                listening.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listening
                await client.disconnect()

    def _attach(self, controller: Controller) -> None:
        now = self._clock()
        for sensor in self._sensors:
            node = controller.nodes.get(sensor.node_id)
            if node is None:
                logger.warning("Sensor %s: node %d is not paired", sensor.name, sensor.node_id)
                if not sensor.silent:
                    sensor.silent = True
                    self._events.publish(SensorSilent(sensor=sensor.name, since=sensor.last_seen))
                continue
            sensor.node = node
            for value in node.values.values():
                self._on_value(sensor, value.command_class, value.property_, value.property_key, value.value)
            self._heard(sensor, node.last_seen, now)
            if sensor.heard_at is None:
                sensor.heard_at = now
            self._unsubscribe.append(node.on(
                "value updated", lambda data, sensor=sensor: self._on_update(sensor, data)
            ))
            self._unsubscribe.append(node.on(
                "value notification", lambda data, sensor=sensor: self._on_update(sensor, data)
            ))
        logger.info("Z-Wave server connected; watching %d sensors", len(self._sensors))
        if self._down_since is not None and self._offline_reported:
            down = now - self._down_since
            logger.info("Z-Wave server back after %.0fs", down)
            self._events.publish(ZwaveOnline(down_seconds=down))
        self.online, self.down_at = True, None
        self._down_since, self._offline_reported = None, False

    def _detach(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        for sensor in self._sensors:
            sensor.node = None
        if self.online is not False:
            self._down_since, self._down_at = self._clock(), self._now()
        self.online = False

    def _on_update(self, sensor: _Sensor, data: dict[str, Any]) -> None:
        value = data["value"]
        self._on_value(sensor, value.command_class, value.property_, value.property_key, value.value)
        # A report means the sensor was just heard.
        self._heard(sensor, self._now(), self._clock())

    def _on_value(self, sensor: _Sensor, command_class: int, prop: Any, key: Any, value: Any) -> None:
        if command_class == NOTIFICATION_CC and prop == "Access Control" and key == "Door state":
            if value in (DOOR_OPEN, DOOR_CLOSED):
                self._set_open(sensor, value == DOOR_OPEN)
        elif command_class == BINARY_SENSOR_CC and prop == "Door/Window" and isinstance(value, bool):
            self._set_open(sensor, value)
        elif command_class == NOTIFICATION_CC and prop == "Home Security" and key == "Cover status":
            self._set_tampered(sensor, value == COVER_REMOVED)
        elif command_class == BATTERY_CC and prop == "level" and isinstance(value, int):
            sensor.battery = value
        elif command_class == WAKE_UP_CC and prop == "wakeUpInterval" and isinstance(value, int) and value > 0:
            sensor.wakeup = value

    def _set_open(self, sensor: _Sensor, open_: bool) -> None:
        # The sensor sends both a notification and a binary report per change.
        if open_ == sensor.open:
            return
        known = sensor.open is not None
        sensor.open = open_
        logger.info("Sensor %s: %s", sensor.name, "open" if open_ else "closed")
        if known:
            self._events.publish(SensorOpened(sensor=sensor.name) if open_ else SensorClosed(sensor=sensor.name))

    def _set_tampered(self, sensor: _Sensor, tampered: bool) -> None:
        if tampered == sensor.tampered:
            return
        known = sensor.tampered is not None
        sensor.tampered = tampered
        if not tampered:
            logger.info("Sensor %s: cover back on", sensor.name)
            return
        logger.warning("Sensor %s: cover removed", sensor.name)
        if known:
            self._events.publish(SensorTamper(sensor=sensor.name))

    def _heard(self, sensor: _Sensor, last_seen: datetime | None, now: float) -> None:
        """Record that the sensor was heard at `last_seen` (wall clock)."""
        if last_seen is None or (sensor.last_seen is not None and last_seen <= sensor.last_seen):
            return
        previous = sensor.heard_at
        sensor.last_seen = last_seen
        # Age on the wall clock, converted to monotonic time.
        sensor.heard_at = now - max(0.0, (self._now() - last_seen).total_seconds())
        if sensor.silent:
            sensor.silent = False
            silent = sensor.heard_at - previous if previous is not None else 0.0
            logger.info("Sensor %s heard again", sensor.name)
            self._events.publish(SensorHeard(sensor=sensor.name, silent_seconds=max(0.0, silent)))

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(CHECK_INTERVAL)
            self._check(self._clock())

    def _check(self, now: float) -> None:
        if self.online is not True:
            # While the server is down nothing is heard; the offline alert covers it.
            if (not self._offline_reported and self._down_since is not None
                    and now - self._down_since >= OFFLINE_AFTER):
                self._offline_reported = True
                self.down_at = self._down_at
                logger.warning("Z-Wave server unreachable for %.0fs", now - self._down_since)
                self._events.publish(ZwaveOffline(since=self._down_at))
            return
        for sensor in self._sensors:
            if sensor.node is not None:
                # Wake-ups and other frames update the node's last seen without a value.
                self._heard(sensor, sensor.node.last_seen, now)
            if sensor.silent or sensor.heard_at is None:
                continue
            if now - sensor.heard_at >= sensor.silent_after:
                sensor.silent = True
                logger.warning("Sensor %s silent for %.0fs", sensor.name, now - sensor.heard_at)
                self._events.publish(SensorSilent(sensor=sensor.name, since=sensor.last_seen))

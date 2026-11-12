"""Mains sensing from the 120VAC relay. Alerts on every outage; it never sounds the siren."""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime

from rancho.core.events import EventBus, PowerLost, PowerRestored
from rancho.core.hardware import PowerSensor

logger = logging.getLogger(__name__)
POLL_INTERVAL = 1
# Consecutive equal reads before a change counts: the coil can chatter in a brownout.
STABLE_READS = 3


class PowerMonitor:
    def __init__(
        self,
        events: EventBus,
        sensor: PowerSensor,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._events = events
        self._sensor = sensor
        self._clock = clock
        self.available = False
        # None until the first stable reading.
        self.present: bool | None = None
        self.off_at: datetime | None = None
        self._off_since: float | None = None
        self._candidate: bool | None = None
        self._candidate_since = 0.0
        self._count = 0

    def open(self) -> None:
        try:
            self._sensor.open()
        except Exception as exc:
            logger.error("Power sensor unavailable (%s: %s)", type(exc).__name__, exc)
            return
        self.available = True

    async def run(self) -> None:
        if not self.available:
            await asyncio.Future()
            return
        try:
            while True:
                self._check(self._sensor.power_present(), self._clock())
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            self._sensor.close()

    def _check(self, reading: bool, now: float) -> None:
        if reading != self._candidate:
            self._candidate, self._candidate_since, self._count = reading, now, 0
        self._count += 1
        if self._count >= STABLE_READS and reading != self.present:
            self._change(reading, self._candidate_since)

    def _change(self, present: bool, at: float) -> None:
        first = self.present is None
        self.present = present
        if not present:
            self._off_since, self.off_at = at, datetime.now().astimezone()
            logger.warning("Power %s", "absent at start" if first else "lost")
            self._events.publish(PowerLost(since=self.off_at))
            return
        if first:
            logger.info("Power present")
            return
        assert self._off_since is not None
        down = at - self._off_since
        logger.info("Power back after %.0fs", down)
        self._events.publish(PowerRestored(down_seconds=down))
        self._off_since = self.off_at = None

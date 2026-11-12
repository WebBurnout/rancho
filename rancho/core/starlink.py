"""Watch the Starlink dish on the roof: its cable could be cut.

The dish answers on 192.168.100.1 through the Starlink router, without going
through the satellites, so satellite dropouts do not count. The dish is not on
the UPS, so it is expected to be down during a power cut and while it boots
after one; it is only reported when mains has been present long enough.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from rancho.core.events import EventBus, StarlinkOffline, StarlinkOnline
from rancho.core.power import PowerMonitor

logger = logging.getLogger(__name__)
DISH = "192.168.100.1"
# The dish's gRPC port (the Starlink app's API). A TCP connect needs no ping privileges.
DISH_PORT = 9200
CHECK_INTERVAL = 15
OFFLINE_AFTER = 60
# The dish takes ~2-3 min to boot after mains returns.
BOOT_GRACE = 300


async def dish_reachable() -> bool:
    try:
        async with asyncio.timeout(5):
            _, writer = await asyncio.open_connection(DISH, DISH_PORT)
    except (OSError, TimeoutError):
        return False
    writer.close()
    try:
        async with asyncio.timeout(5):
            await writer.wait_closed()
    except (OSError, TimeoutError):
        pass
    return True


class StarlinkMonitor:
    def __init__(
        self,
        events: EventBus,
        power: PowerMonitor,
        reachable: Callable[[], Awaitable[bool]] = dish_reachable,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._events = events
        self._power = power
        self._reachable = reachable
        self._clock = clock
        # None until the first check.
        self.online: bool | None = None
        self._down_since: float | None = None
        self._down_at: datetime | None = None
        self.reported = False
        # Monotonic time mains was last seen returning; None while it is off.
        self._mains_since: float | None = float("-inf")

    async def run(self) -> None:
        while True:
            self._check(await self._reachable(), self._clock())
            await asyncio.sleep(CHECK_INTERVAL)

    def _check(self, online: bool, now: float) -> None:
        # An unavailable sensor (None) counts as mains present, so a cut still alerts.
        if self._power.present is False:
            self._mains_since = None
        elif self._mains_since is None:
            self._mains_since = now
        if online != self.online:
            logger.log(logging.INFO if online else logging.WARNING,
                       "Starlink dish %s", "reachable" if online else "unreachable")
        self.online = online
        if online:
            if self.reported and self._down_since is not None:
                down = now - self._down_since
                logger.info("Starlink dish back after %.0fs", down)
                self._events.publish(StarlinkOnline(down_seconds=down))
            self._down_since = self._down_at = None
            self.reported = False
            return
        if self._down_since is None:
            self._down_since, self._down_at = now, datetime.now().astimezone()
        if self.reported or self._mains_since is None:
            return
        # Count from the later of the outage start and the end of the boot grace.
        start = max(self._down_since, self._mains_since + BOOT_GRACE)
        if now - start >= OFFLINE_AFTER:
            self.reported = True
            logger.warning("Starlink dish unreachable for %.0fs with mains present", now - self._down_since)
            assert self._down_at is not None
            self._events.publish(StarlinkOffline(since=self._down_at))

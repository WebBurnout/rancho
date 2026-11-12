"""Drive the siren from SirenTriggered events; disarming silences it."""

import asyncio
import logging
import time
from collections.abc import Callable

from rancho.core.events import EventBus, ModeChanged, SirenTriggered
from rancho.core.hardware import Siren

logger = logging.getLogger(__name__)


class SirenControl:
    def __init__(self, events: EventBus, siren: Siren, clock: Callable[[], float] = time.monotonic) -> None:
        self._events = events
        self._siren = siren
        self._clock = clock
        self.available = False
        self.on = False
        self._off_at: float | None = None

    def open(self) -> None:
        """Claim the GPIO, OFF. A failure leaves Telegram alerts working without a siren."""
        try:
            self._siren.open()
        except Exception as exc:
            logger.error("Siren unavailable (%s: %s)", type(exc).__name__, exc)
            return
        self.available = True
        logger.info("Siren ready (off)")

    async def run(self) -> None:
        events = self._events.subscribe()
        try:
            while True:
                # The off timer lives in this loop, not a callback, so a GPIO error
                # crashes the service (and ExecStopPost forces the pin off).
                timeout = None if self._off_at is None else max(0.0, self._off_at - self._clock())
                try:
                    async with asyncio.timeout(timeout):
                        event = await events.get()
                except TimeoutError:
                    self.off()
                    continue
                if isinstance(event, SirenTriggered):
                    self.sound(event.seconds, event.source)
                elif isinstance(event, ModeChanged) and event.mode == "disarmed":
                    self.off()
        finally:
            self._events.unsubscribe(events)
            self._off_at = None
            self.on = False
            if self.available:
                self.available = False
                self._siren.close()
                logger.info("Siren released (off)")

    def sound(self, seconds: float, source: str) -> None:
        if not self.available:
            logger.warning("Siren unavailable; not sounding for %s", source)
            return
        # A new trigger while sounding restarts the timer rather than stacking.
        self._off_at = self._clock() + seconds
        if not self.on:
            self._siren.set(True)
            self.on = True
        logger.warning("Siren on for %gs (%s)", seconds, source)

    def off(self) -> None:
        self._off_at = None
        if self.on:
            self._siren.set(False)
            self.on = False
            logger.info("Siren off")

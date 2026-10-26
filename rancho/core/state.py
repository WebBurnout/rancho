"""Alarm mode, exit delay, intrusion cooldown, and persistence to state.json."""

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from pathlib import Path

from rancho.core.events import EventBus, Intrusion, ModeChanged, PersonDetected

logger = logging.getLogger(__name__)
EXIT_DELAY = 60
# Per camera: someone walking around the house would otherwise send a stream.
INTRUSION_COOLDOWN = 60


class AlarmMode(str, Enum):
    DISARMED = "disarmed"
    ARMING = "arming"
    ARMED = "armed"


class Alarm:
    def __init__(
        self,
        path: Path,
        events: EventBus,
        exit_delay: float = EXIT_DELAY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._events = events
        self.exit_delay = exit_delay
        self._clock = clock
        self._arming: asyncio.Task[None] | None = None
        self._last_intrusion: dict[str, float] = {}
        self.mode = AlarmMode.DISARMED
        self.since = datetime.now().astimezone()
        self.by: str | None = None
        # Set when state.json exists but could not be read; front ends report it.
        self.load_failed = False

    def load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            mode = AlarmMode(data["mode"])
            since = datetime.fromisoformat(data["since"])
            by = data["by"]
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Could not read %s; starting disarmed", self._path.name)
            self.load_failed = True
            return
        self.since, self.by = since, by
        if mode == AlarmMode.ARMING:
            # Stopped mid exit delay. Arming was intended, and a restart takes longer.
            self._set(AlarmMode.ARMED, by)
        else:
            self.mode = mode
        logger.info("Alarm restored: %s", self.mode.value)

    def arm(self, by: str) -> bool:
        """Start the exit delay. Returns False if already arming or armed."""
        if self.mode != AlarmMode.DISARMED:
            return False
        self._set(AlarmMode.ARMING, by)
        self._arming = asyncio.create_task(self._finish_arming(by), name="exit-delay")
        return True

    def disarm(self, by: str) -> bool:
        """Disarm, cancelling any exit delay. Returns False if already disarmed."""
        if self.mode == AlarmMode.DISARMED:
            return False
        if self._arming is not None:
            self._arming.cancel()
            self._arming = None
        self._set(AlarmMode.DISARMED, by)
        return True

    async def run(self) -> None:
        events = self._events.subscribe()
        try:
            while True:
                event = await events.get()
                if isinstance(event, PersonDetected):
                    self._on_person(event)
        finally:
            self._events.unsubscribe(events)
            if self._arming is not None:
                self._arming.cancel()

    def _on_person(self, event: PersonDetected) -> None:
        if self.mode != AlarmMode.ARMED:
            return
        now = self._clock()
        last = self._last_intrusion.get(event.camera)
        if last is not None and now - last < INTRUSION_COOLDOWN:
            logger.info("Camera %s: intrusion within cooldown; not alerting", event.camera)
            return
        self._last_intrusion[event.camera] = now
        logger.warning("Intrusion: person on %s while armed", event.camera)
        self._events.publish(Intrusion(camera=event.camera))

    async def _finish_arming(self, by: str) -> None:
        await asyncio.sleep(self.exit_delay)
        self._arming = None
        self._set(AlarmMode.ARMED, by)

    def _set(self, mode: AlarmMode, by: str) -> None:
        self.mode, self.since, self.by = mode, datetime.now().astimezone(), by
        self._last_intrusion.clear()
        logger.info("Alarm %s by %s", mode.value, by)
        self._save()
        self._events.publish(ModeChanged(mode=mode.value, by=by))

    def _save(self) -> None:
        # Written only on change. Rename is atomic, so a power cut leaves the old or new file.
        data = {"mode": self.mode.value, "since": self.since.isoformat(), "by": self.by}
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as file:
                json.dump(data, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("Could not save %s (%s)", self._path.name, type(exc).__name__)

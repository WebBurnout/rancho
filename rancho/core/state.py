"""Alarm mode, alert sleep, intrusion cooldown, and persistence to state.json."""

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from rancho.core.events import EventBus, Intrusion, ModeChanged, PersonDetected

logger = logging.getLogger(__name__)
# Per camera: someone walking around the house would otherwise send a stream.
INTRUSION_COOLDOWN = 60
# Upper bound for /pausa, so a typo cannot silence the alarm for days.
MAX_SLEEP_HOURS = 24


class AlarmMode(str, Enum):
    DISARMED = "disarmed"
    ARMED = "armed"


class Alarm:
    def __init__(
        self,
        path: Path,
        events: EventBus,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = path
        self._events = events
        self._clock = clock
        # Alert sleep lives in memory only: a restart resumes alerts.
        self._sleep_deadline: float | None = None
        self.sleep_until: datetime | None = None
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
        self.mode, self.since, self.by = mode, since, by
        logger.info("Alarm restored: %s", self.mode.value)

    def arm(self, by: str) -> bool:
        """Arm immediately, ending any alert sleep. Returns False if already armed."""
        if self.mode == AlarmMode.ARMED:
            return False
        if self._sleep_deadline is not None:
            self._sleep_deadline = self.sleep_until = None
            logger.info("Alert sleep cleared by arming")
        self._set(AlarmMode.ARMED, by)
        return True

    def disarm(self, by: str) -> bool:
        """Returns False if already disarmed."""
        if self.mode == AlarmMode.DISARMED:
            return False
        self._set(AlarmMode.DISARMED, by)
        return True

    @property
    def sleeping(self) -> bool:
        if self._sleep_deadline is not None and self._clock() >= self._sleep_deadline:
            self._sleep_deadline = self.sleep_until = None
            logger.info("Alert sleep ended")
        return self._sleep_deadline is not None

    def sleep(self, hours: float, by: str) -> None:
        """Suppress intrusion alerts for `hours`; 0 resumes them now."""
        if not 0 <= hours <= MAX_SLEEP_HOURS:
            raise ValueError(f"hours must be between 0 and {MAX_SLEEP_HOURS}")
        if hours == 0:
            self._sleep_deadline = self.sleep_until = None
            logger.info("Alert sleep cancelled by %s", by)
            return
        self._sleep_deadline = self._clock() + hours * 3600
        self.sleep_until = datetime.now().astimezone() + timedelta(hours=hours)
        logger.info("Alerts asleep for %g h by %s", hours, by)

    async def run(self) -> None:
        events = self._events.subscribe()
        try:
            while True:
                event = await events.get()
                if isinstance(event, PersonDetected):
                    self._on_person(event)
        finally:
            self._events.unsubscribe(events)

    def _on_person(self, event: PersonDetected) -> None:
        if self.mode != AlarmMode.ARMED:
            return
        if self.sleeping:
            logger.info("Camera %s: person detected while alerts asleep; not alerting", event.camera)
            return
        now = self._clock()
        last = self._last_intrusion.get(event.camera)
        if last is not None and now - last < INTRUSION_COOLDOWN:
            logger.info("Camera %s: intrusion within cooldown; not alerting", event.camera)
            return
        self._last_intrusion[event.camera] = now
        logger.warning("Intrusion: person on %s while armed", event.camera)
        self._events.publish(Intrusion(camera=event.camera))

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

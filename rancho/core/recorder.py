"""While armed, photograph a camera every second for as long as it detects a person.

Every photo is archived at full resolution (Cameras does that on capture); every
TELEGRAM_INTERVAL seconds one is published for the front ends. The first photo of
a detection is the Intrusion alert, so follow-ups start at TELEGRAM_INTERVAL.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from rancho.core.cameras import Cameras
from rancho.core.events import EventBus, PersonCleared, PersonDetected, PersonPhoto
from rancho.core.state import Alarm, AlarmMode

logger = logging.getLogger(__name__)
CAPTURE_INTERVAL = 1
TELEGRAM_INTERVAL = 10
# Per detection: a stuck detection must not flood the chat or the SIM. Archiving goes on.
MAX_FOLLOW_UPS = 30


class Recorder:
    def __init__(
        self,
        events: EventBus,
        cameras: Cameras,
        alarm: Alarm,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._events = events
        self._cameras = cameras
        self._alarm = alarm
        self._clock = clock
        self._tracks: dict[str, asyncio.Task[None]] = {}

    async def run(self) -> None:
        events = self._events.subscribe()
        try:
            while True:
                event = await events.get()
                if isinstance(event, PersonDetected):
                    self._start(event.camera)
                elif isinstance(event, PersonCleared):
                    self._stop(event.camera)
        finally:
            self._events.unsubscribe(events)
            tracks = list(self._tracks.values())
            for task in tracks:
                task.cancel()
            await asyncio.gather(*tracks, return_exceptions=True)

    def _active(self) -> bool:
        return self._alarm.mode == AlarmMode.ARMED and not self._alarm.sleeping

    def _start(self, camera: str) -> None:
        if camera in self._tracks or not self._active():
            return
        task = asyncio.create_task(self._track(camera), name=f"recorder-{camera}")
        self._tracks[camera] = task

        def done(task: asyncio.Task[None]) -> None:
            if self._tracks.get(camera) is task:
                del self._tracks[camera]
            if not task.cancelled() and task.exception() is not None:
                logger.warning("Camera %s: recording failed (%s)", camera, type(task.exception()).__name__)

        task.add_done_callback(done)

    def _stop(self, camera: str) -> None:
        task = self._tracks.pop(camera, None)
        if task is not None:
            task.cancel()

    async def _track(self, camera: str) -> None:
        start = self._clock()
        photos = sent = 0
        logger.info("Camera %s: recording a photo every %ds", camera, CAPTURE_INTERVAL)
        try:
            # Disarming or pausing stops recording within a second.
            while self._active():
                snapshot = await self._cameras.capture(camera)
                elapsed = self._clock() - start
                if snapshot.image is not None:
                    photos += 1
                    if sent < MAX_FOLLOW_UPS and elapsed >= (sent + 1) * TELEGRAM_INTERVAL:
                        sent += 1
                        self._events.publish(PersonPhoto(camera=camera, image=snapshot.image, seconds=round(elapsed)))
                        if sent == MAX_FOLLOW_UPS:
                            logger.info("Camera %s: follow-up limit reached; still archiving", camera)
                # Keep the 1 s schedule; a slow snapshot just means the next one starts at once.
                next_at = start + (int(elapsed / CAPTURE_INTERVAL) + 1) * CAPTURE_INTERVAL
                await asyncio.sleep(max(0.0, next_at - self._clock()))
        finally:
            logger.info("Camera %s: recording stopped after %d photos", camera, photos)

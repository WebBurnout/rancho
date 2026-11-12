"""Offline recorder tests: fake cameras, alarm, and clock."""

import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from rancho.core.cameras import Snapshot
from rancho.core.events import EventBus, PersonCleared, PersonDetected, PersonPhoto
from rancho.core.recorder import MAX_FOLLOW_UPS, TELEGRAM_INTERVAL, Recorder
from rancho.core.state import AlarmMode

real_sleep = asyncio.sleep


class RecorderTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.photos = self.bus.subscribe()
        self.now = 0.0
        self.captures: list[float] = []
        self.alarm = SimpleNamespace(mode=AlarmMode.ARMED, sleeping=False)
        self.cameras = SimpleNamespace(capture=self.capture)
        self.recorder = Recorder(self.bus, self.cameras, self.alarm, clock=lambda: self.now)
        patcher = patch("rancho.core.recorder.asyncio.sleep", self.sleep)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def capture(self, camera: str) -> Snapshot:
        self.captures.append(self.now)
        self.now += 0.3  # snapshot latency
        return Snapshot(camera, image=b"jpeg")

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await real_sleep(0)

    async def run_for(self, seconds: float) -> None:
        # Fake time only moves while something is recording; stop if it doesn't.
        end = self.now + seconds
        for _ in range(100_000):
            if self.now >= end:
                return
            await real_sleep(0)

    def follow_ups(self) -> list[int]:
        events = [self.photos.get_nowait() for _ in range(self.photos.qsize())]
        return [e.seconds for e in events if isinstance(e, PersonPhoto)]

    async def test_photo_every_second_and_follow_up_every_ten(self) -> None:
        self.recorder._start("Patio")
        await self.run_for(25)
        self.assertEqual([round(t) for t in self.captures[:4]], [0, 1, 2, 3])
        self.assertEqual(self.follow_ups(), [TELEGRAM_INTERVAL, 2 * TELEGRAM_INTERVAL])
        self.recorder._stop("Patio")
        await real_sleep(0)
        count = len(self.captures)
        await self.run_for(0)
        self.assertEqual(len(self.captures), count)

    async def test_disarming_stops_recording(self) -> None:
        self.recorder._start("Patio")
        await self.run_for(3)
        task = self.recorder._tracks["Patio"]
        self.alarm.mode = AlarmMode.DISARMED
        await asyncio.wait_for(task, 1)
        self.assertNotIn("Patio", self.recorder._tracks)

    async def test_not_recording_while_disarmed_or_paused(self) -> None:
        self.alarm.mode = AlarmMode.DISARMED
        self.recorder._start("Patio")
        self.alarm.mode, self.alarm.sleeping = AlarmMode.ARMED, True
        self.recorder._start("Puerta")
        self.assertEqual(self.recorder._tracks, {})

    async def test_follow_ups_are_capped_but_archiving_continues(self) -> None:
        self.recorder._start("Patio")
        await self.run_for((MAX_FOLLOW_UPS + 5) * TELEGRAM_INTERVAL)
        self.assertEqual(len(self.follow_ups()), MAX_FOLLOW_UPS)
        self.assertGreater(len(self.captures), (MAX_FOLLOW_UPS + 4) * TELEGRAM_INTERVAL)
        self.recorder._stop("Patio")

    async def test_events_start_and_stop_tracks(self) -> None:
        task = asyncio.create_task(self.recorder.run())
        await real_sleep(0)
        self.bus.publish(PersonDetected(camera="Patio"))
        await self.run_for(2)
        self.assertIn("Patio", self.recorder._tracks)
        self.bus.publish(PersonCleared(camera="Patio"))
        await real_sleep(0)
        await real_sleep(0)
        self.assertNotIn("Patio", self.recorder._tracks)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

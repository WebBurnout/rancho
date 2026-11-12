"""Offline siren tests: a fake GPIO line and a fake clock."""

import asyncio
from unittest import IsolatedAsyncioTestCase

from rancho.core.events import EventBus, ModeChanged, SirenTriggered
from rancho.core.siren import SirenControl


class FakeSiren:
    def __init__(self, fail_open: bool = False) -> None:
        self.fail_open = fail_open
        self.calls: list = []

    def open(self) -> None:
        if self.fail_open:
            raise PermissionError("no gpio")
        self.calls.append("open")

    def set(self, on: bool) -> None:
        self.calls.append(on)

    def close(self) -> None:
        self.calls.append("close")


class SirenTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.hw = FakeSiren()
        self.now = 100.0
        self.siren = SirenControl(self.bus, self.hw, clock=lambda: self.now)

    async def test_sound_then_off_after_timer(self) -> None:
        self.siren.open()
        self.siren.sound(5, "Patio")
        self.assertTrue(self.siren.on)
        self.siren.sound(5, "Puerta")
        self.siren.off()
        self.assertEqual(self.hw.calls, ["open", True, False])

    async def test_run_turns_off_when_timer_expires(self) -> None:
        self.siren.open()
        task = asyncio.create_task(self.siren.run())
        await asyncio.sleep(0)
        self.bus.publish(SirenTriggered(camera="Patio", seconds=0.05))
        await asyncio.sleep(0.01)
        self.assertTrue(self.siren.on)
        self.now += 0.05
        await asyncio.sleep(0.1)
        self.assertFalse(self.siren.on)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.hw.calls, ["open", True, False, "close"])

    async def test_disarm_silences_and_stop_releases_off(self) -> None:
        self.siren.open()
        task = asyncio.create_task(self.siren.run())
        await asyncio.sleep(0)
        self.bus.publish(SirenTriggered(camera="Patio", seconds=60))
        self.bus.publish(ModeChanged(mode="disarmed", by="Tim"))
        await asyncio.sleep(0.01)
        self.assertFalse(self.siren.on)
        self.bus.publish(SirenTriggered(camera="Patio", seconds=60))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Cancelled while on: close() switches it off.
        self.assertEqual(self.hw.calls, ["open", True, False, True, "close"])
        self.assertFalse(self.siren.on)

    async def test_unavailable_gpio_is_reported_not_fatal(self) -> None:
        siren = SirenControl(self.bus, FakeSiren(fail_open=True))
        with self.assertLogs("rancho.core.siren", "ERROR"):
            siren.open()
        self.assertFalse(siren.available)
        siren.sound(5, "Patio")
        self.assertFalse(siren.on)

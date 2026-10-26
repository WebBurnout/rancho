"""Offline alarm tests: temporary state files and a fake clock."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from rancho.core.events import EventBus, Intrusion, ModeChanged, PersonDetected
from rancho.core.state import INTRUSION_COOLDOWN, Alarm, AlarmMode


class AlarmTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.now = 1000.0
        self.alarm = self.new_alarm()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def new_alarm(self, exit_delay: float = 0) -> Alarm:
        return Alarm(self.path, self.bus, exit_delay=exit_delay, clock=lambda: self.now)

    def drain(self) -> list:
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    async def arm_fully(self) -> None:
        self.alarm.arm("Tim")
        await asyncio.sleep(0.01)
        self.assertEqual(self.alarm.mode, AlarmMode.ARMED)
        self.drain()

    async def test_arm_goes_through_exit_delay(self) -> None:
        self.alarm.arm("Tim")
        self.assertEqual(self.alarm.mode, AlarmMode.ARMING)
        await asyncio.sleep(0.01)
        self.assertEqual(self.alarm.mode, AlarmMode.ARMED)
        modes = [(e.mode, e.by) for e in self.drain() if isinstance(e, ModeChanged)]
        self.assertEqual(modes, [("arming", "Tim"), ("armed", "Tim")])

    async def test_disarm_cancels_exit_delay(self) -> None:
        alarm = self.new_alarm(exit_delay=3600)
        alarm.arm("Tim")
        self.assertTrue(alarm.disarm("Horacio"))
        await asyncio.sleep(0.01)
        self.assertEqual(alarm.mode, AlarmMode.DISARMED)
        self.assertEqual(alarm.by, "Horacio")

    async def test_repeat_commands_are_refused(self) -> None:
        self.assertFalse(self.alarm.disarm("Tim"))
        self.alarm.arm("Tim")
        self.assertFalse(self.alarm.arm("Tim"))
        self.alarm.disarm("Tim")

    async def test_person_alerts_only_when_armed(self) -> None:
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.alarm.arm("Tim")
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.assertFalse(any(isinstance(e, Intrusion) for e in self.drain()))
        await asyncio.sleep(0.01)
        self.alarm._on_person(PersonDetected(camera="Patio"))
        intrusions = [e for e in self.drain() if isinstance(e, Intrusion)]
        self.assertEqual([e.camera for e in intrusions], ["Patio"])

    async def test_cooldown_is_per_camera(self) -> None:
        await self.arm_fully()
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.now += INTRUSION_COOLDOWN - 1
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.alarm._on_person(PersonDetected(camera="Puerta"))
        self.now += 1
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.assertEqual([e.camera for e in self.drain()], ["Patio", "Puerta", "Patio"])

    async def test_state_survives_restart(self) -> None:
        await self.arm_fully()
        restored = self.new_alarm()
        restored.load()
        self.assertEqual(restored.mode, AlarmMode.ARMED)
        self.assertEqual(restored.by, "Tim")
        self.assertFalse(restored.load_failed)

    async def test_restart_during_exit_delay_restores_armed(self) -> None:
        self.new_alarm(exit_delay=3600).arm("Tim")
        restored = self.new_alarm()
        restored.load()
        self.assertEqual(restored.mode, AlarmMode.ARMED)
        self.assertEqual(json.loads(self.path.read_text())["mode"], "armed")

    async def test_missing_file_starts_disarmed(self) -> None:
        self.alarm.load()
        self.assertEqual(self.alarm.mode, AlarmMode.DISARMED)
        self.assertFalse(self.alarm.load_failed)

    async def test_corrupt_file_starts_disarmed_and_flags_it(self) -> None:
        for text in ("{not json", '{"mode": "party"}', "[]"):
            with self.subTest(text):
                self.path.write_text(text)
                alarm = self.new_alarm()
                alarm.load()
                self.assertEqual(alarm.mode, AlarmMode.DISARMED)
                self.assertTrue(alarm.load_failed)

    async def test_save_leaves_no_temp_file(self) -> None:
        await self.arm_fully()
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["state.json"])

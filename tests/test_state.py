"""Offline alarm tests: temporary state files and a fake clock."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from rancho.core.events import EventBus, Intrusion, ModeChanged, PersonDetected
from rancho.core.state import INTRUSION_COOLDOWN, MAX_SLEEP_HOURS, Alarm, AlarmMode


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

    def new_alarm(self) -> Alarm:
        return Alarm(self.path, self.bus, clock=lambda: self.now)

    def drain(self) -> list:
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    def arm(self) -> None:
        self.alarm.arm("Tim")
        self.drain()

    async def test_arm_is_immediate(self) -> None:
        self.assertTrue(self.alarm.arm("Tim"))
        self.assertEqual(self.alarm.mode, AlarmMode.ARMED)
        self.assertTrue(self.alarm.disarm("Horacio"))
        self.assertEqual(self.alarm.by, "Horacio")
        modes = [(e.mode, e.by) for e in self.drain() if isinstance(e, ModeChanged)]
        self.assertEqual(modes, [("armed", "Tim"), ("disarmed", "Horacio")])

    async def test_repeat_commands_are_refused(self) -> None:
        self.assertFalse(self.alarm.disarm("Tim"))
        self.alarm.arm("Tim")
        self.assertFalse(self.alarm.arm("Tim"))

    async def test_person_alerts_only_when_armed(self) -> None:
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.assertFalse(any(isinstance(e, Intrusion) for e in self.drain()))
        self.alarm.arm("Tim")
        self.alarm._on_person(PersonDetected(camera="Patio"))
        intrusions = [e for e in self.drain() if isinstance(e, Intrusion)]
        self.assertEqual([e.camera for e in intrusions], ["Patio"])

    async def test_sleep_silences_alerts_until_it_ends(self) -> None:
        self.arm()
        self.alarm.sleep(1, "Tim")
        self.assertTrue(self.alarm.sleeping)
        self.assertIsNotNone(self.alarm.sleep_until)
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.assertEqual(self.drain(), [])
        self.now += 3600
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.assertEqual([e.camera for e in self.drain()], ["Patio"])
        self.assertFalse(self.alarm.sleeping)
        self.assertIsNone(self.alarm.sleep_until)

    async def test_sleep_zero_resumes_alerts(self) -> None:
        self.arm()
        self.alarm.sleep(1, "Tim")
        self.alarm.sleep(0, "Tim")
        self.assertFalse(self.alarm.sleeping)
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.assertEqual([e.camera for e in self.drain()], ["Patio"])

    async def test_arming_clears_sleep(self) -> None:
        self.alarm.sleep(1, "Tim")
        self.alarm.arm("Tim")
        self.assertFalse(self.alarm.sleeping)
        self.assertIsNone(self.alarm.sleep_until)

    async def test_disarming_keeps_sleep(self) -> None:
        self.arm()
        self.alarm.sleep(1, "Tim")
        self.alarm.disarm("Tim")
        self.assertTrue(self.alarm.sleeping)

    async def test_sleep_rejects_out_of_range_hours(self) -> None:
        for hours in (-1, MAX_SLEEP_HOURS + 1, float("nan")):
            with self.subTest(hours):
                with self.assertRaises(ValueError):
                    self.alarm.sleep(hours, "Tim")
        self.assertFalse(self.alarm.sleeping)

    async def test_cooldown_is_per_camera(self) -> None:
        self.arm()
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.now += INTRUSION_COOLDOWN - 1
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.alarm._on_person(PersonDetected(camera="Puerta"))
        self.now += 1
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.assertEqual([e.camera for e in self.drain()], ["Patio", "Puerta", "Patio"])

    async def test_state_survives_restart(self) -> None:
        self.arm()
        restored = self.new_alarm()
        restored.load()
        self.assertEqual(restored.mode, AlarmMode.ARMED)
        self.assertEqual(restored.by, "Tim")
        self.assertFalse(restored.load_failed)

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
        self.arm()
        self.assertEqual(json.loads(self.path.read_text())["mode"], "armed")
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["state.json"])

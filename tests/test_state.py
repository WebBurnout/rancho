"""Offline alarm tests: temporary state files and a fake clock."""

import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from rancho.core.events import (
    EventBus, Intrusion, ModeChanged, ObjectDetected, PersonDetected, SensorIntrusion, SensorOpened,
    SirenTriggered, Sighting,
)
from rancho.core.state import (
    CONFIRM_WINDOW, INTRUSION_COOLDOWN, MAX_SLEEP_HOURS, SIGHTING_COOLDOWN, SIREN_COOLDOWN, SIREN_SECONDS,
    Alarm, AlarmMode, ArmingBlocked,
)

NOON = datetime(2026, 11, 11, 12, 0)
MIDNIGHT = datetime(2026, 11, 11, 0, 30)


class AlarmTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.now = 1000.0
        self.local = NOON
        self.open: list[str] = []
        self.alarm = self.new_alarm()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def new_alarm(self) -> Alarm:
        return Alarm(
            self.path, self.bus, clock=lambda: self.now, local_now=lambda: self.local,
            open_sensors=lambda: list(self.open),
        )

    def drain(self) -> list:
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    def intrusions(self) -> list[str]:
        return [e.camera for e in self.drain() if isinstance(e, Intrusion)]

    def person(self, camera: str = "Patio") -> list:
        self.alarm._on_person(PersonDetected(camera=camera))
        return self.drain()

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
        self.assertEqual(self.intrusions(), ["Patio"])
        self.assertFalse(self.alarm.sleeping)
        self.assertIsNone(self.alarm.sleep_until)

    async def test_sleep_zero_resumes_alerts(self) -> None:
        self.arm()
        self.alarm.sleep(1, "Tim")
        self.alarm.sleep(0, "Tim")
        self.assertFalse(self.alarm.sleeping)
        self.alarm._on_person(PersonDetected(camera="Patio"))
        self.assertEqual(self.intrusions(), ["Patio"])

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
        self.assertEqual(self.intrusions(), ["Patio", "Puerta", "Patio"])

    async def test_daytime_detection_sounds_siren_with_photo(self) -> None:
        self.arm()
        events = self.person()
        self.assertEqual(events, [
            Intrusion(camera="Patio", siren=True, occurred_at=events[0].occurred_at),
            SirenTriggered(source="Patio", seconds=SIREN_SECONDS, occurred_at=events[1].occurred_at),
        ])

    async def test_siren_cooldown_is_global(self) -> None:
        self.arm()
        self.person("Patio")
        self.now += SIREN_COOLDOWN - 1
        events = self.person("Puerta")
        self.assertEqual([(type(e), e.siren) for e in events], [(Intrusion, False)])
        self.now += 1
        self.assertTrue(any(isinstance(e, SirenTriggered) for e in self.person("Puerta")))

    async def test_night_detection_sends_photo_but_waits_for_confirmation(self) -> None:
        self.local = MIDNIGHT
        self.arm()
        events = self.person("Patio")
        self.assertEqual([(type(e), e.siren, e.unconfirmed) for e in events], [(Intrusion, False, True)])
        self.now += CONFIRM_WINDOW - 1
        events = self.person("Puerta")
        self.assertEqual([type(e) for e in events], [Intrusion, SirenTriggered])
        self.assertTrue(events[0].siren)

    async def test_night_confirmation_within_photo_cooldown_still_sends_photo(self) -> None:
        self.local = MIDNIGHT
        self.arm()
        self.person("Patio")
        self.now += 10
        events = self.person("Patio")
        self.assertEqual([type(e) for e in events], [Intrusion, SirenTriggered])

    async def test_night_detections_too_far_apart_do_not_confirm(self) -> None:
        self.local = MIDNIGHT
        self.arm()
        self.person("Patio")
        self.now += CONFIRM_WINDOW
        events = self.person("Puerta")
        self.assertEqual([(type(e), e.unconfirmed) for e in events], [(Intrusion, True)])

    async def test_animal_sighting_alerts_only_when_armed_without_siren(self) -> None:
        self.alarm._on_object(ObjectDetected(camera="Bomba", kind="animal"))
        self.assertEqual(self.drain(), [])
        self.arm()
        self.alarm._on_object(ObjectDetected(camera="Bomba", kind="animal"))
        events = self.drain()
        self.assertEqual([(type(e), e.kind) for e in events], [(Sighting, "animal")])

    async def test_sighting_cooldown_is_per_camera_and_kind(self) -> None:
        self.arm()
        for camera, kind in (("Bomba", "animal"), ("Bomba", "animal"), ("Bomba", "vehicle"), ("Patio", "animal")):
            self.alarm._on_object(ObjectDetected(camera=camera, kind=kind))
        self.assertEqual(len(self.drain()), 3)
        self.now += SIGHTING_COOLDOWN
        self.alarm._on_object(ObjectDetected(camera="Bomba", kind="animal"))
        self.assertEqual(len(self.drain()), 1)

    async def test_pause_silences_sightings(self) -> None:
        self.arm()
        self.alarm.sleep(1, "Tim")
        self.alarm._on_object(ObjectDetected(camera="Bomba", kind="animal"))
        self.assertEqual(self.drain(), [])

    async def test_night_window_wraps_midnight(self) -> None:
        for hour, night in ((18, False), (19, True), (0, True), (6, True), (7, False)):
            with self.subTest(hour):
                self.local = NOON.replace(hour=hour)
                self.assertEqual(self.alarm.night(), night)

    async def test_disarming_forgets_detections(self) -> None:
        self.local = MIDNIGHT
        self.arm()
        self.person("Patio")
        self.alarm.disarm("Tim")
        self.arm()
        self.assertEqual([e.unconfirmed for e in self.person("Patio")], [True])

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

    async def test_arming_is_refused_while_a_sensor_is_open(self) -> None:
        self.open = ["Cocina", "Sala"]
        with self.assertRaises(ArmingBlocked) as blocked:
            self.alarm.arm("Tim")
        self.assertEqual(blocked.exception.sensors, ["Cocina", "Sala"])
        self.assertEqual(self.alarm.mode, AlarmMode.DISARMED)
        self.assertEqual(self.drain(), [])
        self.assertFalse(self.path.exists())
        self.open = []
        self.assertTrue(self.alarm.arm("Tim"))

    async def test_already_armed_wins_over_open_sensor(self) -> None:
        self.arm()
        self.open = ["Cocina"]
        self.assertFalse(self.alarm.arm("Tim"))

    async def test_sensor_opening_while_armed_sounds_siren(self) -> None:
        self.local = MIDNIGHT
        self.arm()
        for _ in range(2):
            self.alarm._on_sensor(SensorOpened(sensor="Cocina"))
            events = self.drain()
            # No night confirmation or cooldown for sensors.
            self.assertEqual(events, [
                SensorIntrusion(sensor="Cocina", occurred_at=events[0].occurred_at),
                SirenTriggered(source="Cocina", seconds=SIREN_SECONDS, occurred_at=events[1].occurred_at),
            ])

    async def test_sensor_opening_is_ignored_when_disarmed_or_asleep(self) -> None:
        self.alarm._on_sensor(SensorOpened(sensor="Cocina"))
        self.assertEqual(self.drain(), [])
        self.arm()
        self.alarm.sleep(1, "Tim")
        self.alarm._on_sensor(SensorOpened(sensor="Cocina"))
        self.assertEqual(self.drain(), [])

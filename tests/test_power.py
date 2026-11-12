"""Offline mains-sensing tests: readings are fed directly with a fake clock."""

from unittest import TestCase

from rancho.core.events import EventBus, PowerLost, PowerRestored
from rancho.core.power import STABLE_READS, PowerMonitor


class PowerTests(TestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.now = 0.0
        self.power = PowerMonitor(self.bus, sensor=None)

    def read(self, present: bool, seconds: int = 1) -> list:
        for _ in range(seconds):
            self.power._check(present, self.now)
            self.now += 1
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    def test_present_at_start_is_silent(self) -> None:
        self.assertEqual(self.read(True, 10), [])
        self.assertTrue(self.power.present)

    def test_outage_alerts_once_and_reports_duration(self) -> None:
        self.read(True, 5)
        events = self.read(False, STABLE_READS)
        self.assertEqual([type(e) for e in events], [PowerLost])
        self.assertEqual(events[0].since, self.power.off_at)
        self.assertEqual(self.read(False, 100), [])
        events = self.read(True, STABLE_READS)
        self.assertEqual([type(e) for e in events], [PowerRestored])
        self.assertEqual(events[0].down_seconds, 100 + STABLE_READS)
        self.assertIsNone(self.power.off_at)

    def test_glitch_shorter_than_stable_reads_is_ignored(self) -> None:
        self.read(True, 5)
        self.assertEqual(self.read(False, STABLE_READS - 1), [])
        self.assertEqual(self.read(True, 5), [])
        self.assertTrue(self.power.present)

    def test_chatter_does_not_end_an_outage(self) -> None:
        self.read(True, 5)
        self.read(False, STABLE_READS)
        events = []
        for _ in range(10):
            events += self.read(True, STABLE_READS - 1)
            events += self.read(False, 1)
        self.assertEqual(events, [])
        self.assertFalse(self.power.present)

    def test_absent_at_start_alerts(self) -> None:
        events = self.read(False, STABLE_READS)
        self.assertEqual([type(e) for e in events], [PowerLost])

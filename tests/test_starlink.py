"""Offline Starlink dish tests: reachability and mains are faked."""

from types import SimpleNamespace
from unittest import TestCase

from rancho.core.events import EventBus, StarlinkOffline, StarlinkOnline
from rancho.core.starlink import BOOT_GRACE, CHECK_INTERVAL, OFFLINE_AFTER, StarlinkMonitor


class StarlinkTests(TestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.power = SimpleNamespace(present=True)
        self.now = 0.0
        self.starlink = StarlinkMonitor(self.bus, self.power)

    def check(self, online: bool, seconds: float = CHECK_INTERVAL) -> list:
        end = self.now + seconds
        while self.now < end:
            self.starlink._check(online, self.now)
            self.now += CHECK_INTERVAL
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    def test_reachable_is_silent(self) -> None:
        self.assertEqual(self.check(True, 120), [])
        self.assertTrue(self.starlink.online)

    def test_short_dropout_is_not_reported(self) -> None:
        self.check(True)
        self.assertEqual(self.check(False, OFFLINE_AFTER), [])
        self.assertEqual(self.check(True), [])

    def test_cut_with_mains_alerts_once_then_reports_recovery(self) -> None:
        self.check(True)
        events = self.check(False, OFFLINE_AFTER + CHECK_INTERVAL + 300)
        self.assertEqual([type(e) for e in events], [StarlinkOffline])
        events = self.check(True)
        self.assertEqual([type(e) for e in events], [StarlinkOnline])
        self.assertGreater(events[0].down_seconds, OFFLINE_AFTER)

    def test_power_cut_is_not_reported(self) -> None:
        self.check(True)
        self.power.present = False
        self.assertEqual(self.check(False, 3600), [])

    def test_dish_gets_boot_grace_after_mains_returns(self) -> None:
        self.check(True)
        self.power.present = False
        self.check(False, 600)
        self.power.present = True
        self.assertEqual(self.check(False, BOOT_GRACE + OFFLINE_AFTER - CHECK_INTERVAL), [])
        self.assertEqual([type(e) for e in self.check(False, 2 * CHECK_INTERVAL)], [StarlinkOffline])

    def test_dish_that_boots_after_power_cut_is_silent(self) -> None:
        self.check(True)
        self.power.present = False
        self.check(False, 600)
        self.power.present = True
        self.check(False, 180)
        self.assertEqual(self.check(True), [])

    def test_missing_power_sensor_counts_as_mains_present(self) -> None:
        self.power.present = None
        events = self.check(False, OFFLINE_AFTER + CHECK_INTERVAL)
        self.assertEqual([type(e) for e in events], [StarlinkOffline])

"""Offline sensor tests: fake Z-Wave nodes and clocks, no server connection."""

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase

from rancho.core.config import Config, SensorConfig
from rancho.core.events import (
    EventBus, SensorClosed, SensorHeard, SensorOpened, SensorSilent, SensorTamper, ZwaveOffline, ZwaveOnline,
)
from rancho.core.sensors import (
    BATTERY_CC, BINARY_SENSOR_CC, DOOR_CLOSED, DOOR_OPEN, MISSED_WAKEUPS, NOTIFICATION_CC, OFFLINE_AFTER,
    SILENT_MARGIN, WAKE_UP_CC, Sensors,
)

START = datetime(2026, 11, 11, 22, 0).astimezone()


def value(command_class: int, prop: str, key: str | None, current) -> SimpleNamespace:
    return SimpleNamespace(command_class=command_class, property_=prop, property_key=key, value=current)


def door(open_: bool) -> SimpleNamespace:
    return value(NOTIFICATION_CC, "Access Control", "Door state", DOOR_OPEN if open_ else DOOR_CLOSED)


def cover(removed: bool) -> SimpleNamespace:
    return value(NOTIFICATION_CC, "Home Security", "Cover status", 3 if removed else 0)


class FakeNode:
    def __init__(self, *values: SimpleNamespace, last_seen: datetime | None = None) -> None:
        self.values = {str(i): v for i, v in enumerate(values)}
        self.last_seen = last_seen
        self.listeners: dict[str, list] = {}

    def on(self, name: str, callback):
        self.listeners.setdefault(name, []).append(callback)
        return lambda: self.listeners[name].remove(callback)

    def report(self, update: SimpleNamespace) -> None:
        for callback in list(self.listeners.get("value updated", [])):
            callback({"value": update})


class SensorsTests(TestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.mono = 0.0
        self.wall = START
        config = Config(
            state_file=Path("state.json"), telegram_bot_token="", telegram_allowed_chat_ids=frozenset(),
            sensors=(SensorConfig("Cocina", 4),),
        )
        self.sensors = Sensors(config, self.bus, clock=lambda: self.mono, now=lambda: self.wall)
        self.node = FakeNode(
            door(False), cover(False), value(BATTERY_CC, "level", None, 99),
            value(WAKE_UP_CC, "wakeUpInterval", None, 3600), last_seen=START,
        )

    def attach(self, nodes: dict | None = None) -> None:
        self.sensors._attach(SimpleNamespace(nodes={4: self.node} if nodes is None else nodes))

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds)

    def drain(self) -> list:
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    def test_cached_state_is_read_without_events(self) -> None:
        self.attach()
        self.assertEqual(self.drain(), [])
        status = self.sensors.status()[0]
        self.assertEqual((status.open, status.tampered, status.battery, status.silent), (False, False, 99, False))
        self.assertTrue(self.sensors.online)

    def test_open_and_close_publish_once_per_change(self) -> None:
        self.attach()
        self.node.report(door(True))
        # The Ecolink sends a binary report with each notification.
        self.node.report(value(BINARY_SENSOR_CC, "Door/Window", None, True))
        self.assertEqual(self.sensors.open_sensors(), ["Cocina"])
        self.node.report(value(BINARY_SENSOR_CC, "Door/Window", None, False))
        self.node.report(door(False))
        self.assertEqual([type(e) for e in self.drain()], [SensorOpened, SensorClosed])
        self.assertEqual(self.sensors.open_sensors(), [])

    def test_unknown_state_does_not_block_arming(self) -> None:
        self.node = FakeNode()
        self.attach()
        self.assertEqual(self.sensors.open_sensors(), [])
        self.assertIsNone(self.sensors.status()[0].open)

    def test_change_while_disconnected_is_published_on_reconnect(self) -> None:
        self.attach()
        self.sensors._detach()
        self.node = FakeNode(door(True), last_seen=START)
        self.attach()
        self.assertIn(SensorOpened, [type(e) for e in self.drain()])

    def test_removed_cover_alerts(self) -> None:
        self.attach()
        self.node.report(cover(True))
        self.node.report(cover(True))
        self.assertEqual([type(e) for e in self.drain()], [SensorTamper])
        self.assertTrue(self.sensors.status()[0].tampered)
        self.node.report(cover(False))
        self.assertEqual(self.drain(), [])
        self.assertFalse(self.sensors.status()[0].tampered)

    def test_unsubscribes_on_disconnect(self) -> None:
        self.attach()
        self.sensors._detach()
        self.node.report(door(True))
        self.assertEqual(self.drain(), [])

    def test_silent_after_missed_wakeups_then_heard(self) -> None:
        self.attach()
        limit = MISSED_WAKEUPS * 3600 + SILENT_MARGIN
        self.advance(limit - 1)
        self.sensors._check(self.mono)
        self.assertEqual(self.drain(), [])
        self.advance(1)
        self.sensors._check(self.mono)
        self.sensors._check(self.mono)
        events = self.drain()
        self.assertEqual(events, [SensorSilent(sensor="Cocina", since=START, occurred_at=events[0].occurred_at)])
        self.assertTrue(self.sensors.status()[0].silent)
        self.advance(600)
        self.node.last_seen = self.wall
        self.sensors._check(self.mono)
        events = self.drain()
        self.assertEqual([type(e) for e in events], [SensorHeard])
        self.assertEqual(events[0].silent_seconds, limit + 600)

    def test_reports_keep_sensor_from_going_silent(self) -> None:
        self.attach()
        for _ in range(4):
            self.advance(3600)
            self.node.report(door(True))
            self.sensors._check(self.mono)
        self.assertNotIn(SensorSilent, [type(e) for e in self.drain()])

    def test_old_last_seen_counts_toward_silence(self) -> None:
        self.node.last_seen = START - timedelta(seconds=MISSED_WAKEUPS * 3600 + SILENT_MARGIN)
        self.attach()
        self.sensors._check(self.mono)
        self.assertEqual([type(e) for e in self.drain()], [SensorSilent])

    def test_unpaired_node_is_reported_silent(self) -> None:
        self.attach(nodes={})
        self.attach(nodes={})
        events = self.drain()
        self.assertEqual(events, [SensorSilent(sensor="Cocina", since=None, occurred_at=events[0].occurred_at)])

    def test_server_down_alerts_once_and_recovery_reports_duration(self) -> None:
        self.attach()
        self.sensors._detach()
        self.advance(OFFLINE_AFTER - 1)
        self.sensors._check(self.mono)
        self.assertEqual(self.drain(), [])
        self.advance(1)
        self.sensors._check(self.mono)
        self.sensors._check(self.mono)
        self.assertEqual([type(e) for e in self.drain()], [ZwaveOffline])
        self.assertFalse(self.sensors.online)
        self.advance(100)
        self.attach()
        events = self.drain()
        self.assertEqual([type(e) for e in events], [ZwaveOnline])
        self.assertEqual(events[0].down_seconds, OFFLINE_AFTER + 100)
        self.assertIsNone(self.sensors.down_at)

    def test_server_never_up_is_reported(self) -> None:
        self.advance(OFFLINE_AFTER)
        self.sensors._check(self.mono)
        self.assertEqual([type(e) for e in self.drain()], [ZwaveOffline])

    def test_sensors_are_not_silent_while_server_is_down(self) -> None:
        self.attach()
        self.sensors._detach()
        self.advance(24 * 3600)
        self.sensors._check(self.mono)
        self.assertEqual([type(e) for e in self.drain()], [ZwaveOffline])

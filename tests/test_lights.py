"""Offline light schedule tests: fake Z-Wave nodes, clock and dusk, no server connection."""

import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase

from zwave_js_server.exceptions import FailedZWaveCommand

from rancho.core.config import Config, LightConfig, Location
from rancho.core.lights import BINARY_SWITCH_CC, NODE_DEAD, Lights, civil_dusk


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute).astimezone()


def dusk(day: date) -> datetime:
    return datetime.combine(day, time(19, 0)).astimezone()


class NoJitter:
    def uniform(self, low: float, high: float) -> float:
        return 0.0


def switch(prop: str, current: bool | None) -> SimpleNamespace:
    return SimpleNamespace(command_class=BINARY_SWITCH_CC, property_=prop, endpoint=0, value=current)


class FakeNode:
    def __init__(self, on: bool | None = False) -> None:
        self.values = {"current": switch("currentValue", on), "target": switch("targetValue", None)}
        self.status = 4
        self.listeners: dict[str, list] = {}
        self.sent: list[bool] = []
        self.error: Exception | None = None

    def on(self, name: str, callback):
        self.listeners.setdefault(name, []).append(callback)
        return lambda: self.listeners[name].remove(callback)

    def report(self, on: bool) -> None:
        for callback in list(self.listeners.get("value updated", [])):
            callback({"value": switch("currentValue", on)})

    async def async_set_value(self, value, new_value):
        assert value.property_ == "targetValue"
        if self.error is not None:
            raise self.error
        self.sent.append(new_value)
        return None


def config(jitter: int = 30) -> Config:
    return Config(
        state_file=Path("state.json"), telegram_bot_token="", telegram_allowed_chat_ids=frozenset(),
        lights=(LightConfig("Foco", 7, "dusk", time(3, 0), jitter=jitter),),
    )


class ScheduleTests(TestCase):
    def setUp(self) -> None:
        self.now = local(12, 12)
        self.lights = Lights(config(), now=lambda: self.now, rng=NoJitter(), dusk=dusk)
        self.light = self.lights._lights[0]

    def schedule(self, now: datetime) -> tuple[bool, datetime]:
        _, on, change = self.lights._schedule(self.light, now)
        return on, change

    def test_on_from_dusk_until_three_across_midnight(self) -> None:
        self.assertEqual(self.schedule(local(12, 12)), (False, local(12, 19)))
        self.assertEqual(self.schedule(local(12, 19)), (True, local(13, 3)))
        self.assertEqual(self.schedule(local(12, 23, 59)), (True, local(13, 3)))
        self.assertEqual(self.schedule(local(13, 2, 59)), (True, local(13, 3)))
        self.assertEqual(self.schedule(local(13, 3)), (False, local(13, 19)))

    def test_daytime_window(self) -> None:
        lights = Lights(
            Config(
                state_file=Path("state.json"), telegram_bot_token="", telegram_allowed_chat_ids=frozenset(),
                lights=(LightConfig("Foco", 7, time(7, 0), "dusk"),),
            ),
            rng=NoJitter(), dusk=dusk,
        )
        light = lights._lights[0]
        self.assertEqual(lights._schedule(light, local(12, 6))[1:], (False, local(12, 7)))
        self.assertEqual(lights._schedule(light, local(12, 8))[1:], (True, local(12, 19)))
        self.assertEqual(lights._schedule(light, local(12, 20))[1:], (False, local(13, 7)))

    def test_jitter_stays_within_bounds_and_is_picked_once_per_night(self) -> None:
        lights = Lights(config(), rng=random.Random(1), dusk=dusk)
        light = lights._lights[0]
        offsets = set()
        for day in range(1, 29):
            window = lights._window(light, date(2026, 11, day))
            self.assertEqual(lights._window(light, date(2026, 11, day)), window)
            on_shift = window.on_at - local(day, 19)
            off_shift = window.off_at - (local(day, 3) + timedelta(days=1))
            for shift in (on_shift, off_shift):
                self.assertLessEqual(abs(shift), timedelta(minutes=30))
            offsets.add(on_shift)
        self.assertGreater(len(offsets), 20)

    def test_civil_dusk_is_on_the_local_date(self) -> None:
        dusk_on = civil_dusk(Location(18.95, -98.75))
        for day in (date(2026, 6, 21), date(2026, 11, 12), date(2026, 12, 21)):
            at = dusk_on(day).astimezone()
            self.assertEqual(at.date(), day)
            self.assertTrue(time(17, 0) < at.time() < time(21, 0), at)

    def test_old_nights_are_forgotten(self) -> None:
        for day in range(1, 20):
            self.lights._schedule(self.light, local(day, 12))
        self.assertLessEqual(len(self.light.windows), 5)


class SwitchingTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = local(12, 20)
        self.lights = Lights(config(), now=lambda: self.now, rng=NoJitter(), dusk=dusk)
        self.node = FakeNode(on=False)

    def attach(self) -> None:
        self.lights._attach(SimpleNamespace(nodes={7: self.node}))

    def status(self):
        return self.lights.status()[0]

    async def test_start_applies_the_schedule_once(self) -> None:
        await self.lights._apply(self.now)
        self.assertEqual(self.node.sent, [])
        self.attach()
        self.assertFalse(self.status().on)
        await self.lights._apply(self.now)
        await self.lights._apply(self.now)
        self.assertEqual(self.node.sent, [True])
        status = self.status()
        self.assertEqual((status.on, status.manual, status.next_change), (True, False, local(13, 3)))

    async def test_switches_at_each_scheduled_time(self) -> None:
        self.attach()
        await self.lights._apply(self.now)
        await self.lights._apply(local(13, 3, 1))
        await self.lights._apply(local(13, 12))
        await self.lights._apply(local(13, 19, 1))
        self.assertEqual(self.node.sent, [True, False, True])

    async def test_manual_switch_lasts_until_the_next_scheduled_time(self) -> None:
        self.attach()
        await self.lights._apply(self.now)
        self.node.report(False)
        await self.lights._apply(local(12, 22))
        self.assertEqual(self.node.sent, [True])
        self.assertTrue(self.status().manual)
        await self.lights._apply(local(13, 3, 1))
        self.assertEqual(self.node.sent, [True, False])
        self.now = local(13, 4)
        self.assertFalse(self.status().manual)

    async def test_reconnect_keeps_a_manual_switch(self) -> None:
        self.attach()
        await self.lights._apply(self.now)
        self.node.report(False)
        self.lights._detach()
        self.node = FakeNode(on=False)
        self.attach()
        await self.lights._apply(local(12, 22))
        self.assertEqual(self.node.sent, [])

    async def test_failed_switch_is_retried(self) -> None:
        self.attach()
        self.node.error = FailedZWaveCommand("1", 0, "no ack")
        with self.assertLogs("rancho.core.lights", "WARNING") as logs:
            await self.lights._apply(self.now)
            await self.lights._apply(self.now)
        self.assertEqual(len(logs.records), 1)
        self.node.error = None
        await self.lights._apply(self.now)
        self.assertEqual(self.node.sent, [True])

    async def test_switch_change_while_disconnected_happens_on_reconnect(self) -> None:
        self.attach()
        await self.lights._apply(self.now)
        self.lights._detach()
        await self.lights._apply(local(13, 3, 1))
        self.attach()
        await self.lights._apply(local(13, 3, 2))
        self.assertEqual(self.node.sent, [True, False])

    async def test_switch_by_hand_lasts_until_the_next_scheduled_time(self) -> None:
        self.assertFalse(await self.lights.switch("foco", False))
        self.attach()
        await self.lights._apply(self.now)
        self.assertTrue(await self.lights.switch("foco", False))
        self.assertTrue(self.status().manual)
        await self.lights._apply(local(12, 22))
        self.assertEqual(self.node.sent, [True, False])
        await self.lights._apply(local(13, 3, 1))
        self.assertEqual(self.node.sent, [True, False, False])
        with self.assertRaises(KeyError):
            await self.lights.switch("Pozo", True)

    async def test_switch_failure_is_reported(self) -> None:
        self.attach()
        self.node.error = FailedZWaveCommand("1", 0, "no ack")
        self.assertFalse(await self.lights.switch("Foco", True))

    def test_unreachable_light(self) -> None:
        self.assertFalse(self.status().reachable)
        self.node.status = NODE_DEAD
        self.attach()
        self.assertFalse(self.status().reachable)

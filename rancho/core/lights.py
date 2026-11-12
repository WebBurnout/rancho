"""Z-Wave relays switched on a schedule, so the house looks lived in.

Each night's on and off times shift randomly by up to the light's jitter. The
schedule switches a light only at its own on and off times, and once at service
start; switching it by hand (at the relay or with `switch`) in between is left
alone. Nothing here goes to Telegram: the state shows up in the status only.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from rancho.core import zwave
from rancho.core.config import Config, LightConfig, Location, Moment

if TYPE_CHECKING:
    from zwave_js_server.model.controller import Controller
    from zwave_js_server.model.node import Node
    from zwave_js_server.model.value import Value

logger = logging.getLogger(__name__)
CHECK_INTERVAL = 30
# Seconds to wait for the relay to confirm a switch; a failed one is retried next check.
SWITCH_TIMEOUT = 30
BINARY_SWITCH_CC = 37
# zwave-js NodeStatus.DEAD: the controller gave up reaching the node.
NODE_DEAD = 3


@dataclass(frozen=True)
class Window:
    """When a light is on for one night."""
    on_at: datetime
    off_at: datetime


@dataclass(frozen=True)
class LightStatus:
    light: str
    # None until the relay reports or the server's cached state is read.
    on: bool | None
    reachable: bool
    # Switched by hand since the schedule last switched it.
    manual: bool
    # When the schedule next switches it.
    next_change: datetime


def civil_dusk(location: Location) -> Callable[[date], datetime]:
    def dusk_on(day: date) -> datetime:
        from astral import Observer
        from astral.sun import dusk

        observer = Observer(location.latitude, location.longitude)
        # Local time, so `day` is the local date: in UTC, dusk in Mexico is after midnight.
        tz = datetime.combine(day, time(12)).astimezone().tzinfo
        return dusk(observer, date=day, tzinfo=tz)
    return dusk_on


class _Light:
    def __init__(self, config: LightConfig) -> None:
        self.config = config
        self.name = config.name
        self.node_id = config.node
        self.node: Node | None = None
        self.on: bool | None = None
        # Windows by the date they are planned from, so each night's random offsets are picked once.
        self.windows: dict[date, Window] = {}
        # The schedule phase last applied; None until the first switch after start.
        self.applied: tuple[date, bool] | None = None
        self.failed: tuple[date, bool] | None = None


class Lights:
    def __init__(
        self,
        config: Config,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        rng: random.Random | None = None,
        dusk: Callable[[date], datetime] | None = None,
    ) -> None:
        self._now = now
        self._random = rng or random.Random()
        if dusk is None and config.location is not None:
            dusk = civil_dusk(config.location)
        self._dusk = dusk
        self._lights = [_Light(light) for light in config.lights]
        self.online: bool | None = None
        self._unsubscribe: list[Callable[[], None]] = []

    @property
    def configured(self) -> bool:
        return bool(self._lights)

    def status(self) -> list[LightStatus]:
        now = self._now()
        statuses = []
        for light in self._lights:
            phase, on, next_change = self._schedule(light, now)
            reachable = light.node is not None and light.node.status != NODE_DEAD
            manual = light.applied == phase and light.on is not None and light.on != on
            statuses.append(LightStatus(light.name, light.on, reachable, manual, next_change))
        return statuses

    def names(self) -> list[str]:
        return [light.name for light in self._lights]

    async def switch(self, name: str, on: bool) -> bool:
        """Switch a light by hand until its next scheduled time; False if the relay didn't confirm."""
        light = next((light for light in self._lights if light.name.casefold() == name.casefold()), None)
        if light is None:
            raise KeyError(name)
        if light.node is None:
            return False
        if not await self._switch(light, on):
            return False
        logger.info("Light %s: switched %s by hand", light.name, "on" if on else "off")
        return True

    async def run(self) -> None:
        if not self._lights:
            logger.info("Lights disabled: no lights in config.yaml")
            await asyncio.Future()
            return
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._connect_loop(), name="lights-connection")
            tasks.create_task(self._schedule_loop(), name="lights-schedule")

    async def _connect_loop(self) -> None:
        import aiohttp
        from zwave_js_server.exceptions import BaseZwaveJSServerError

        while True:
            try:
                await zwave.session(self._attach)
            except (BaseZwaveJSServerError, aiohttp.ClientError, OSError) as exc:
                if self.online is not False:
                    logger.warning("Lights: Z-Wave server unreachable (%s)", type(exc).__name__)
            self._detach()
            await asyncio.sleep(zwave.RECONNECT_DELAY)

    def _attach(self, controller: Controller) -> None:
        for light in self._lights:
            node = controller.nodes.get(light.node_id)
            if node is None:
                logger.warning("Light %s: node %d is not paired", light.name, light.node_id)
                continue
            light.node = node
            for value in node.values.values():
                self._on_value(light, value)
            self._unsubscribe.append(node.on(
                "value updated", lambda data, light=light: self._on_value(light, data["value"])
            ))
        self.online = True

    def _detach(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        for light in self._lights:
            light.node = None
        self.online = False

    def _on_value(self, light: _Light, value: Value) -> None:
        if (value.command_class == BINARY_SWITCH_CC and value.property_ == "currentValue"
                and isinstance(value.value, bool) and value.value != light.on):
            if light.on is not None:
                logger.info("Light %s: %s", light.name, "on" if value.value else "off")
            light.on = value.value

    async def _schedule_loop(self) -> None:
        while True:
            await self._apply(self._now())
            await asyncio.sleep(CHECK_INTERVAL)

    async def _apply(self, now: datetime) -> None:
        for light in self._lights:
            phase, on, until = self._schedule(light, now)
            if phase == light.applied or light.node is None:
                continue
            if await self._switch(light, on):
                logger.info("Light %s: switched %s until %s", light.name, "on" if on else "off", f"{until:%d/%m %H:%M}")
                light.applied, light.failed = phase, None
            elif light.failed != phase:
                # Retried every check; logged once per scheduled switch.
                light.failed = phase
                logger.warning("Light %s: could not switch %s; retrying", light.name, "on" if on else "off")

    async def _switch(self, light: _Light, on: bool) -> bool:
        import aiohttp
        from zwave_js_server.const import SetValueStatus
        from zwave_js_server.exceptions import BaseZwaveJSServerError

        assert light.node is not None
        target = _target(light.node)
        if target is None:
            logger.warning("Light %s: node %d has no on/off switch", light.name, light.node_id)
            return False
        try:
            result = await asyncio.wait_for(light.node.async_set_value(target, on), SWITCH_TIMEOUT)
        except (BaseZwaveJSServerError, aiohttp.ClientError, OSError, TimeoutError) as exc:
            logger.debug("Light %s: switch failed (%s)", light.name, type(exc).__name__)
            return False
        if result is not None and result.status not in (
            SetValueStatus.SUCCESS, SetValueStatus.SUCCESS_UNSUPERVISED, SetValueStatus.WORKING,
        ):
            return False
        light.on = on
        return True

    def _schedule(self, light: _Light, now: datetime) -> tuple[tuple[date, bool], bool, datetime]:
        """The current phase, whether the light should be on, and when that changes."""
        today = now.date()
        for day in [d for d in light.windows if d < today - timedelta(days=1)]:
            del light.windows[day]
        windows = [(day, self._window(light, day)) for day in (today + timedelta(days=n) for n in range(-1, 3))]
        for day, window in windows:
            if window.on_at <= now < window.off_at:
                return (day, True), True, window.off_at
        day, window = min(((d, w) for d, w in windows if w.on_at > now), key=lambda item: item[1].on_at)
        return (day, False), False, window.on_at

    def _window(self, light: _Light, day: date) -> Window:
        if day not in light.windows:
            on = self._moment(light.config.on, day)
            off = self._moment(light.config.off, day)
            if off <= on:
                off = self._moment(light.config.off, day + timedelta(days=1))
            on += self._shift(light.config.jitter)
            off += self._shift(light.config.jitter)
            light.windows[day] = Window(on, max(on, off))
        return light.windows[day]

    def _moment(self, moment: Moment, day: date) -> datetime:
        if isinstance(moment, time):
            return datetime.combine(day, moment).astimezone()
        assert self._dusk is not None, "config requires a location for dusk"
        return self._dusk(day)

    def _shift(self, minutes: int) -> timedelta:
        return timedelta(seconds=self._random.uniform(-minutes * 60, minutes * 60))


def _target(node: Node) -> Any:
    """The relay's writable on/off value, on the lowest endpoint that has one."""
    values = [
        value for value in node.values.values()
        if value.command_class == BINARY_SWITCH_CC and value.property_ == "targetValue"
    ]
    return min(values, key=lambda value: value.endpoint or 0) if values else None

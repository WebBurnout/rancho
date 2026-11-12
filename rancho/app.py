"""Compose the core and front ends; own the service lifecycle."""

import asyncio
import logging
import signal

from rancho.core.cameras import Cameras
from rancho.core.config import Config
from rancho.core.events import EventBus
from rancho.core.hardware import GpioPowerSensor, GpioSiren
from rancho.core.lights import Lights
from rancho.core.photos import PhotoArchive
from rancho.core.power import PowerMonitor
from rancho.core.recorder import Recorder
from rancho.core.sensors import Sensors
from rancho.core.siren import SirenControl
from rancho.core.starlink import StarlinkMonitor
from rancho.core.state import Alarm
from rancho.frontends.telegram import TelegramFrontend

logger = logging.getLogger(__name__)


async def run(config: Config) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    events = EventBus()
    # Claim the siren first, so it is driven OFF as early as possible.
    siren = SirenControl(events, GpioSiren(config.siren_active_low))
    siren.open()
    power = PowerMonitor(events, GpioPowerSensor())
    power.open()
    starlink = StarlinkMonitor(events, power)
    sensors = Sensors(config, events)
    lights = Lights(config)
    alarm = Alarm(config.state_file, events, open_sensors=sensors.open_sensors)
    alarm.load()
    archive = PhotoArchive(config.state_file.parent / "photos")
    archive.load()
    cameras = Cameras(config, events, archive)
    recorder = Recorder(events, cameras, alarm)
    telegram = TelegramFrontend(config, events, cameras, alarm, siren, power, starlink, sensors, lights)
    workers = {
        asyncio.create_task(siren.run(), name="siren"),
        asyncio.create_task(power.run(), name="power"),
        asyncio.create_task(starlink.run(), name="starlink"),
        asyncio.create_task(alarm.run(), name="alarm"),
        asyncio.create_task(telegram.run(), name="telegram"),
        asyncio.create_task(cameras.run(), name="cameras"),
        asyncio.create_task(sensors.run(), name="sensors"),
        asyncio.create_task(lights.run(), name="lights"),
        asyncio.create_task(recorder.run(), name="recorder"),
    }
    stop_task = asyncio.create_task(stop.wait(), name="shutdown")
    try:
        logger.info("Rancho started (mode=%s)", alarm.mode.value)
        done, _ = await asyncio.wait(
            workers | {stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done & workers:
            # A dead worker must not leave a deceptively live service.
            await task
            raise RuntimeError(f"{task.get_name()} worker exited unexpectedly")
    finally:
        logger.info("Rancho stopping")
        for task in workers | {stop_task}:
            task.cancel()
        await asyncio.gather(*workers, stop_task, return_exceptions=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        logger.info("Rancho stopped")

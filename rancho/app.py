"""Compose the core and front ends; own the service lifecycle."""

import asyncio
import logging
import signal

from rancho.core.cameras import Cameras
from rancho.core.config import Config
from rancho.core.events import EventBus
from rancho.core.state import AlarmState
from rancho.frontends.telegram import TelegramFrontend

logger = logging.getLogger(__name__)


async def run(config: Config) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    events = EventBus()
    state = AlarmState()
    cameras = Cameras(config, events)
    telegram = TelegramFrontend(config, events, cameras)
    workers = {
        asyncio.create_task(telegram.run(), name="telegram"),
        asyncio.create_task(cameras.run(), name="cameras"),
    }
    stop_task = asyncio.create_task(stop.wait(), name="shutdown")
    try:
        logger.info(
            "Rancho started (mode=%s; state persistence not implemented)",
            state.mode.value,
        )
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

"""Compose the core and front ends; own the service lifecycle."""

import asyncio
import logging
import signal

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
    telegram = TelegramFrontend(config, events)
    try:
        await telegram.start()
        logger.info(
            "Rancho started (skeleton; mode=%s; no hardware access or state persistence)",
            state.mode.value,
        )
        await stop.wait()
    finally:
        logger.info("Rancho stopping")
        await telegram.stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        logger.info("Rancho stopped")

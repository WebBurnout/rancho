"""Telegram lifecycle placeholder; deliberately does not poll or send messages."""

import logging

from rancho.core.config import Config
from rancho.core.events import EventBus

logger = logging.getLogger(__name__)


class TelegramFrontend:
    def __init__(self, config: Config, events: EventBus) -> None:
        self._config = config
        self._events = events

    async def start(self) -> None:
        logger.info("Telegram placeholder: polling and messaging are not implemented")

    async def stop(self) -> None:
        logger.info("Telegram placeholder stopped")

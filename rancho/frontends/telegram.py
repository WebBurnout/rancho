"""Outbound-only Telegram long polling, restricted to allow-listed chats."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from telegram import Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from rancho.core.cameras import Cameras
from rancho.core.config import Config
from rancho.core.events import EventBus

logger = logging.getLogger(__name__)
T = TypeVar("T")

HELP_TEXT = (
    "Comandos:\n"
    "/foto — fotos de todas las cámaras\n"
    "/ayuda — muestra esta lista"
)


async def _send_with_retry(send: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(3):
        try:
            return await send()
        except BadRequest:
            # PTB subclasses BadRequest from NetworkError, but a rejected
            # request will not improve by retrying the same payload.
            raise
        except NetworkError:
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)
    raise AssertionError("unreachable")


class TelegramFrontend:
    def __init__(
        self, config: Config, events: EventBus, cameras: Cameras | None = None
    ) -> None:
        self._config = config
        self._events = events
        self._cameras = cameras if cameras is not None else Cameras(config, events)

    async def run(self) -> None:
        # HTTP request URLs contain the bot token. Never log them, even in DEBUG.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("telegram").setLevel(logging.INFO)
        if not self._config.telegram_bot_token or not self._config.telegram_allowed_chat_ids:
            logger.warning("Telegram disabled: set token and allowed chat IDs in .env")
            await asyncio.Future()
            return

        try:
            while True:
                try:
                    await self._poll()
                except (TelegramError, OSError) as exc:
                    # Log the type only: exception text can contain sensitive URLs.
                    logger.warning("Telegram connection failed (%s); retrying in 5s", type(exc).__name__)
                    await asyncio.sleep(5)
        finally:
            logger.info("Telegram stopped")

    async def _poll(self) -> None:
        application = Application.builder().token(self._config.telegram_bot_token).build()
        application.add_handler(CommandHandler("foto", self._photo))
        application.add_handler(CommandHandler(["ayuda", "start"], self._help))
        application.add_error_handler(self._handle_error)
        async with application:
            try:
                await application.start()
                assert application.updater is not None
                await application.updater.start_polling(
                    allowed_updates=["message"],
                    bootstrap_retries=0,
                    error_callback=self._poll_error,
                )
                logger.info("Telegram polling started; try /ayuda in an allowed chat")
                await asyncio.Future()
            finally:
                if application.updater is not None and application.updater.running:
                    await application.updater.stop()
                if application.running:
                    await application.stop()

    async def _help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None:
            return
        if chat.id not in self._config.telegram_allowed_chat_ids:
            return
        await _send_with_retry(lambda: message.reply_text(HELP_TEXT))

    async def _photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None:
            return
        if chat.id not in self._config.telegram_allowed_chat_ids:
            return
        if not self._cameras.configured:
            await _send_with_retry(lambda: message.reply_text(
                "Configura las cámaras en cameras.yaml y CAMERA_USERNAME y "
                "CAMERA_PASSWORD en .env, y reinicia Rancho."
            ))
            return

        snapshots = await self._cameras.capture_all()
        failures = []
        for snapshot in snapshots:
            if snapshot.image is None:
                failures.append(f"{snapshot.camera}: {snapshot.error}")
                continue
            try:
                # Bind this snapshot for every retry. Images stay in memory.
                async def send_photo(snapshot=snapshot):
                    return await message.reply_photo(
                        photo=snapshot.image,
                        filename=f"{snapshot.camera}.jpg",
                        connect_timeout=10,
                        write_timeout=60,
                        read_timeout=30,
                    )
                await _send_with_retry(send_photo)
            except TelegramError as exc:
                logger.warning("Photo upload failed for %s (%s)", snapshot.camera, type(exc).__name__)
                failures.append(f"{snapshot.camera}: no se pudo enviar la foto a Telegram")
        if failures:
            report = "Algunas cámaras no pudieron enviar fotos:\n" + "\n".join(failures)
            await _send_with_retry(lambda: message.reply_text(report))

    @staticmethod
    def _poll_error(error: TelegramError) -> None:
        logger.warning("Telegram polling error (%s); polling will retry", type(error).__name__)

    @staticmethod
    async def _handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.warning("Telegram command failed (%s)", type(context.error).__name__)

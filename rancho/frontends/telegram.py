"""Outbound-only Telegram long polling, restricted to allow-listed chats."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from rancho.core.cameras import Cameras
from rancho.core.config import Config
from rancho.core.events import Event, EventBus, Intrusion, ModeChanged
from rancho.core.state import Alarm, AlarmMode

logger = logging.getLogger(__name__)
T = TypeVar("T")

HELP_TEXT = (
    "Comandos:\n"
    "/armar — arma la alarma (60 s para salir)\n"
    "/desarmar — desarma la alarma\n"
    "/estado — estado de la alarma\n"
    "/foto — fotos de todas las cámaras\n"
    "/ayuda — muestra esta lista"
)
# Buttons send the command name as callback data.
KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Armar", callback_data="armar"),
     InlineKeyboardButton("Desarmar", callback_data="desarmar")],
    [InlineKeyboardButton("Estado", callback_data="estado"),
     InlineKeyboardButton("Foto", callback_data="foto")],
])
# Uploads can be slow on Telcel; keep these generous.
PHOTO_TIMEOUTS = {"connect_timeout": 10, "write_timeout": 60, "read_timeout": 30}
# Alerts retry for ~30s in total so they survive a Starlink/Telcel failover.
ALERT_ATTEMPTS = 6


async def _send_with_retry(send: Callable[[], Awaitable[T]], attempts: int = 3) -> T:
    for attempt in range(attempts):
        try:
            return await send()
        except BadRequest:
            # PTB subclasses BadRequest from NetworkError, but a rejected
            # request will not improve by retrying the same payload.
            raise
        except NetworkError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(2 ** attempt)
    raise AssertionError("unreachable")


class TelegramFrontend:
    def __init__(
        self,
        config: Config,
        events: EventBus,
        cameras: Cameras | None = None,
        alarm: Alarm | None = None,
    ) -> None:
        self._config = config
        self._events = events
        self._cameras = cameras if cameras is not None else Cameras(config, events)
        self._alarm = alarm if alarm is not None else Alarm(config.state_file, events)
        # The restart notice goes out once per process, not on every reconnect.
        self._announced = False

    async def run(self) -> None:
        # HTTP request URLs contain the bot token. Never log them, even in DEBUG.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("telegram").setLevel(logging.INFO)
        if not self._config.telegram_bot_token or not self._config.telegram_allowed_chat_ids:
            logger.warning("Telegram disabled: set token and allowed chat IDs in .env")
            await asyncio.Future()
            return

        # Subscribe once, so events published while reconnecting wait in the queue.
        events = self._events.subscribe()
        try:
            while True:
                try:
                    await self._poll(events)
                except (TelegramError, OSError) as exc:
                    # Log the type only: exception text can contain sensitive URLs.
                    logger.warning("Telegram connection failed (%s); retrying in 5s", type(exc).__name__)
                    await asyncio.sleep(5)
        finally:
            self._events.unsubscribe(events)
            logger.info("Telegram stopped")

    async def _poll(self, events: asyncio.Queue[Event]) -> None:
        application = Application.builder().token(self._config.telegram_bot_token).build()
        application.add_handler(CommandHandler("armar", self._arm))
        application.add_handler(CommandHandler("desarmar", self._disarm))
        application.add_handler(CommandHandler("estado", self._status))
        application.add_handler(CommandHandler("foto", self._photo))
        application.add_handler(CommandHandler(["ayuda", "start"], self._help))
        application.add_handler(CallbackQueryHandler(self._button))
        application.add_error_handler(self._handle_error)
        async with application:
            try:
                await application.start()
                assert application.updater is not None
                await application.updater.start_polling(
                    allowed_updates=["message", "callback_query"],
                    bootstrap_retries=0,
                    error_callback=self._poll_error,
                )
                logger.info("Telegram polling started; try /ayuda in an allowed chat")
                if not self._announced:
                    self._announced = True
                    await self._announce_restart(application.bot)
                while True:
                    await self._handle_event(application.bot, await events.get())
            finally:
                if application.updater is not None and application.updater.running:
                    await application.updater.stop()
                if application.running:
                    await application.stop()

    async def _handle_event(self, bot: Bot, event: Event) -> None:
        # A failed alert must not take down polling; log it and wait for the next.
        try:
            if isinstance(event, Intrusion):
                await self._alert_intrusion(bot, event)
            elif isinstance(event, ModeChanged):
                await self._broadcast(bot, self._mode_changed_text(event))
        except Exception as exc:
            logger.warning("Alert for %s failed (%s)", event.name, type(exc).__name__)

    async def _alert_intrusion(self, bot: Bot, event: Intrusion) -> None:
        snapshot = await self._cameras.capture(event.camera)
        text = f"Persona detectada: {event.camera}"
        if snapshot.image is None:
            await self._broadcast(bot, f"{text}\nNo se pudo tomar la foto: {snapshot.error}")
            return
        for chat_id in self._config.telegram_allowed_chat_ids:
            await _send_with_retry(lambda: bot.send_photo(
                chat_id, snapshot.image, caption=text, reply_markup=KEYBOARD,
                filename=f"{snapshot.camera}.jpg", **PHOTO_TIMEOUTS,
            ), ALERT_ATTEMPTS)

    async def _announce_restart(self, bot: Bot) -> None:
        text = f"Rancho reiniciado. {self._status_text()}"
        if self._alarm.load_failed:
            text += "\nNo se pudo leer el estado guardado; la alarma quedó desarmada."
        try:
            await self._broadcast(bot, text)
        except TelegramError as exc:
            logger.warning("Restart notice failed (%s)", type(exc).__name__)

    async def _broadcast(self, bot: Bot, text: str) -> None:
        for chat_id in self._config.telegram_allowed_chat_ids:
            await _send_with_retry(
                lambda: bot.send_message(chat_id, text, reply_markup=KEYBOARD), ALERT_ATTEMPTS
            )

    def _mode_changed_text(self, event: ModeChanged) -> str:
        if event.mode == AlarmMode.ARMING:
            return f"Armando… se arma en {self._alarm.exit_delay:g} s ({event.by})"
        if event.mode == AlarmMode.ARMED:
            return "Alarma armada"
        return f"Alarma desarmada ({event.by})"

    def _status_text(self) -> str:
        alarm = self._alarm
        label = {
            AlarmMode.DISARMED: "Alarma desarmada",
            AlarmMode.ARMING: "Armando la alarma",
            AlarmMode.ARMED: "Alarma armada",
        }[alarm.mode]
        text = f"{label} desde {alarm.since:%d/%m %H:%M}"
        return f"{text} ({alarm.by})" if alarm.by else text

    def _allowed_message(self, update: Update) -> Message | None:
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None:
            return None
        if chat.id not in self._config.telegram_allowed_chat_ids:
            return None
        return message

    @staticmethod
    def _who(update: Update) -> str:
        user = update.effective_user
        return user.first_name if user is not None else "?"

    async def _button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or self._allowed_message(update) is None:
            return
        # Stops the button's loading spinner.
        await query.answer()
        handler = {
            "armar": self._arm, "desarmar": self._disarm,
            "estado": self._status, "foto": self._photo,
        }.get(query.data or "")
        if handler is not None:
            await handler(update, context)

    async def _arm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._allowed_message(update)
        if message is None:
            return
        # On success, the ModeChanged broadcast is the reply.
        if not self._alarm.arm(self._who(update)):
            text = ("La alarma ya se está armando." if self._alarm.mode == AlarmMode.ARMING
                    else "La alarma ya está armada.")
            await _send_with_retry(lambda: message.reply_text(text, reply_markup=KEYBOARD))

    async def _disarm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._allowed_message(update)
        if message is None:
            return
        if not self._alarm.disarm(self._who(update)):
            await _send_with_retry(lambda: message.reply_text(
                "La alarma ya está desarmada.", reply_markup=KEYBOARD
            ))

    async def _status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._allowed_message(update)
        if message is None:
            return
        await _send_with_retry(lambda: message.reply_text(self._status_text(), reply_markup=KEYBOARD))

    async def _help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._allowed_message(update)
        if message is None:
            return
        await _send_with_retry(lambda: message.reply_text(HELP_TEXT, reply_markup=KEYBOARD))

    async def _photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._allowed_message(update)
        if message is None:
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
                        **PHOTO_TIMEOUTS,
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

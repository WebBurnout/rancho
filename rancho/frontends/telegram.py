"""Outbound-only Telegram long polling, restricted to allow-listed chats."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime
from io import BytesIO
from typing import Any, TypeVar

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message, Update
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from rancho.core.cameras import Cameras
from rancho.core.config import Config
from rancho.core.events import (
    CameraOffline, CameraOnline, Event, EventBus, Intrusion, ModeChanged, PersonPhoto, PowerLost, PowerRestored,
    SensorHeard, SensorIntrusion, SensorSilent, SensorTamper, Sighting, StarlinkOffline, StarlinkOnline,
    ZwaveOffline, ZwaveOnline,
)
from rancho.core.lights import Lights, LightStatus
from rancho.core.power import PowerMonitor
from rancho.core.sensors import Sensors, SensorStatus
from rancho.core.siren import SirenControl
from rancho.core.starlink import StarlinkMonitor
from rancho.core.state import CONFIRM_WINDOW, MAX_SLEEP_HOURS, Alarm, AlarmMode, ArmingBlocked

logger = logging.getLogger(__name__)
T = TypeVar("T")

HELP_TEXT = (
    "Comandos:\n"
    "/armar — activar la alarma\n"
    "/desarmar — desactivar la alarma\n"
    "/estado — estado de la alarma, las cámaras y los sensores\n"
    "/foto — fotos de todas las cámaras\n"
    "/pausa 1 — pausa las alertas 1 hora (/pausa 0 las reactiva)\n"
    "/encender, /apagar — las luces, hasta su próximo horario (o /encender patio)\n"
    "/ayuda — muestra esta lista"
)
# Buttons send the command name as callback data.
KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("Armar", callback_data="armar"),
     InlineKeyboardButton("Desarmar", callback_data="desarmar")],
    [InlineKeyboardButton("Estado", callback_data="estado"),
     InlineKeyboardButton("Foto", callback_data="foto")],
])
# Set to KEYBOARD to attach the buttons to every message again.
REPLY_MARKUP: InlineKeyboardMarkup | None = None
SIGHTING_TEXT = {"animal": "Animal detectado", "vehicle": "Vehículo detectado"}
# Telegram stores ordinary photos at most 1280 px on the long side, so anything
# bigger only costs SIM data. Full resolution stays in photos/ on the Pi.
TELEGRAM_MAX_SIDE = 1280
# Uploads can be slow on Telcel; keep these generous.
PHOTO_TIMEOUTS = {"connect_timeout": 10, "write_timeout": 60, "read_timeout": 30}
# Alerts keep retrying through an uplink failover, including a Starlink reboot
# after a power cut (Starlink is not on the UPS). A connection opened on the dead
# uplink can hang with no error, so each attempt also has a hard time limit.
ALERT_DEADLINE = 900
TEXT_ATTEMPT_TIMEOUT = 20
PHOTO_ATTEMPT_TIMEOUT = 120


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


async def _send_alert(
    send: Callable[[], Awaitable[T]], what: str, attempt_timeout: float | None = None,
) -> T:
    attempt_timeout = attempt_timeout or TEXT_ATTEMPT_TIMEOUT
    loop = asyncio.get_running_loop()
    give_up = loop.time() + ALERT_DEADLINE
    delay = 1
    attempt = 1
    while True:
        try:
            async with asyncio.timeout(attempt_timeout):
                result = await send()
        except BadRequest:
            raise
        except (NetworkError, TimeoutError) as exc:
            if loop.time() + delay >= give_up:
                raise
            logger.warning(
                "Telegram send of %s failed (attempt %d, %s); retrying in %ds",
                what, attempt, type(exc).__name__, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
            attempt += 1
            continue
        logger.info("Telegram sent %s%s", what, f" after {attempt} attempts" if attempt > 1 else "")
        return result


def _shrink(image: bytes) -> bytes:
    from PIL import Image

    with Image.open(BytesIO(image)) as photo:
        if max(photo.size) <= TELEGRAM_MAX_SIDE:
            return image
        # JPEG draft mode decodes straight at 1/2 scale (2560 -> 1280): fast on the Pi.
        photo.draft("RGB", (TELEGRAM_MAX_SIDE, TELEGRAM_MAX_SIDE))
        photo = photo.convert("RGB")
        photo.thumbnail((TELEGRAM_MAX_SIDE, TELEGRAM_MAX_SIDE))
        out = BytesIO()
        photo.save(out, "JPEG", quality=85)
        return out.getvalue()


async def _for_telegram(image: bytes) -> bytes:
    try:
        return await asyncio.to_thread(_shrink, image)
    except Exception as exc:
        logger.warning("Could not downscale photo (%s); sending full size", type(exc).__name__)
        return image


def _duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _sensor_state(status: SensorStatus) -> str:
    parts = [{None: "comprobando…", True: "abierto", False: "cerrado"}[status.open]]
    if status.tampered:
        parts.append("tapa quitada")
    if status.silent:
        parts.append(f"sin señal desde las {status.last_seen:%H:%M}" if status.last_seen else "sin señal")
    if status.battery is not None:
        parts.append(f"batería {status.battery}%")
    return ", ".join(parts)


def _light_state(status: LightStatus) -> str:
    if not status.reachable:
        return "sin respuesta"
    if status.on is None:
        return "comprobando…"
    state = "encendida" if status.on else "apagada"
    if status.manual:
        return f"{state} a mano; el horario sigue a las {status.next_change:%H:%M}"
    verb = "se apaga" if status.on else "se enciende"
    return f"{state}, {verb} a las {status.next_change:%H:%M}"


def _link_state(online: bool | None, since: datetime | None) -> str:
    if online is None:
        return "comprobando…"
    if online:
        return "conectada"
    if since is not None:
        return f"sin conexión desde las {since:%H:%M}"
    return "sin conexión"


class TelegramFrontend:
    def __init__(
        self,
        config: Config,
        events: EventBus,
        cameras: Cameras | None = None,
        alarm: Alarm | None = None,
        siren: SirenControl | None = None,
        power: PowerMonitor | None = None,
        starlink: StarlinkMonitor | None = None,
        sensors: Sensors | None = None,
        lights: Lights | None = None,
    ) -> None:
        self._config = config
        self._events = events
        self._cameras = cameras if cameras is not None else Cameras(config, events)
        self._alarm = alarm if alarm is not None else Alarm(config.state_file, events)
        # None means not wired (tests); the status reports only real hardware.
        self._siren = siren
        self._power = power
        self._starlink = starlink
        self._sensors = sensors
        self._lights = lights
        # The restart notice goes out once per process, not on every reconnect.
        self._announced = False
        # Photo uploads run beside the event loop, so a slow upload never delays
        # the next alert's text.
        self._uploads: set[asyncio.Task[None]] = set()

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
        application.add_handler(CommandHandler("pausa", self._pause))
        application.add_handler(CommandHandler("encender", self._light_on))
        application.add_handler(CommandHandler("apagar", self._light_off))
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
                for task in self._uploads:
                    task.cancel()
                await asyncio.gather(*self._uploads, return_exceptions=True)
                if application.updater is not None and application.updater.running:
                    await application.updater.stop()
                if application.running:
                    await application.stop()

    async def _handle_event(self, bot: Bot, event: Event) -> None:
        # A failed alert must not take down polling; log it and wait for the next.
        try:
            if isinstance(event, Intrusion):
                self._upload(event.name, self._alert_intrusion(bot, event))
            elif isinstance(event, Sighting):
                self._upload(event.name, self._alert_sighting(bot, event))
            elif isinstance(event, PersonPhoto):
                self._upload(event.name, self._follow_up(bot, event))
            elif isinstance(event, ModeChanged):
                await self._broadcast(bot, self._mode_changed_text(event), event.name)
            elif isinstance(event, CameraOffline):
                await self._broadcast(
                    bot, f"Cámara sin conexión: {event.camera} (desde las {event.since:%H:%M})", event.name
                )
            elif isinstance(event, CameraOnline):
                await self._broadcast(
                    bot, f"Cámara reconectada: {event.camera} (sin conexión {_duration(event.down_seconds)})",
                    event.name,
                )
            elif isinstance(event, PowerLost):
                await self._broadcast(bot, f"Corte de luz desde las {event.since:%H:%M}", event.name)
                self._upload(event.name, self._power_photos(bot, event))
            elif isinstance(event, StarlinkOffline):
                await self._broadcast(
                    bot, f"Antena Starlink sin conexión desde las {event.since:%H:%M}, con luz. "
                    "Revisa el cable de la antena.", event.name,
                )
            elif isinstance(event, StarlinkOnline):
                await self._broadcast(
                    bot, f"Antena Starlink conectada de nuevo (sin conexión {_duration(event.down_seconds)})",
                    event.name,
                )
            elif isinstance(event, PowerRestored):
                await self._broadcast(
                    bot, f"Volvió la luz (sin luz {_duration(event.down_seconds)})", event.name
                )
            elif isinstance(event, SensorIntrusion):
                await self._broadcast(bot, f"Sensor abierto: {event.sensor}\nSirena activada", event.name)
            elif isinstance(event, SensorTamper):
                await self._broadcast(bot, f"Tapa del sensor quitada: {event.sensor}", event.name)
            elif isinstance(event, SensorSilent):
                since = f" desde las {event.since:%H:%M}" if event.since else ""
                await self._broadcast(
                    bot, f"Sensor sin señal{since}: {event.sensor}. Revisa la batería o si está emparejado.",
                    event.name,
                )
            elif isinstance(event, SensorHeard):
                await self._broadcast(
                    bot, f"Sensor con señal de nuevo: {event.sensor} (sin señal {_duration(event.silent_seconds)})",
                    event.name,
                )
            elif isinstance(event, ZwaveOffline):
                await self._broadcast(
                    bot, f"Z-Wave sin conexión desde las {event.since:%H:%M}: los sensores no funcionan. "
                    "Revisa zwave.service.", event.name,
                )
            elif isinstance(event, ZwaveOnline):
                await self._broadcast(
                    bot, f"Z-Wave conectado de nuevo (sin conexión {_duration(event.down_seconds)})", event.name
                )
        except Exception as exc:
            logger.warning("Alert for %s failed (%s)", event.name, type(exc).__name__)

    def _upload(self, what: str, upload: Coroutine[Any, Any, None]) -> None:
        async def guarded() -> None:
            try:
                await upload
            except Exception as exc:
                logger.warning("Alert for %s failed (%s)", what, type(exc).__name__)

        task = asyncio.create_task(guarded(), name=f"telegram-{what}")
        self._uploads.add(task)
        task.add_done_callback(self._uploads.discard)

    async def _alert_intrusion(self, bot: Bot, event: Intrusion) -> None:
        snapshot = await self._cameras.capture(event.camera)
        text = f"Persona detectada: {event.camera}"
        if event.siren:
            text += "\nSirena activada"
        elif event.unconfirmed:
            text += f"\nDe noche la sirena suena solo si hay otra detección en {CONFIRM_WINDOW // 60} min"
        if snapshot.image is None:
            await self._broadcast(bot, f"{text}\nNo se pudo tomar la foto: {snapshot.error}", event.name)
            return
        image = await _for_telegram(snapshot.image)
        for chat_id in self._config.telegram_allowed_chat_ids:
            await _send_alert(lambda: bot.send_photo(
                chat_id, image, caption=text, reply_markup=REPLY_MARKUP,
                filename=f"{snapshot.camera}.jpg", **PHOTO_TIMEOUTS,
            ), f"{event.name} photo", PHOTO_ATTEMPT_TIMEOUT)

    async def _alert_sighting(self, bot: Bot, event: Sighting) -> None:
        text = f"{SIGHTING_TEXT.get(event.kind, event.kind)}: {event.camera}"
        snapshot = await self._cameras.capture(event.camera)
        if snapshot.image is None:
            await self._broadcast(bot, f"{text}\nNo se pudo tomar la foto: {snapshot.error}", event.name)
            return
        image = await _for_telegram(snapshot.image)
        for chat_id in self._config.telegram_allowed_chat_ids:
            await _send_alert(lambda: bot.send_photo(
                chat_id, image, caption=text, reply_markup=REPLY_MARKUP,
                filename=f"{snapshot.camera}.jpg", **PHOTO_TIMEOUTS,
            ), f"{event.name} photo", PHOTO_ATTEMPT_TIMEOUT)

    async def _follow_up(self, bot: Bot, event: PersonPhoto) -> None:
        image = await _for_telegram(event.image)
        text = f"{event.camera}: persona aún detectada ({event.seconds} s)"
        for chat_id in self._config.telegram_allowed_chat_ids:
            await _send_alert(lambda: bot.send_photo(
                chat_id, image, caption=text, reply_markup=REPLY_MARKUP,
                filename=f"{event.camera}.jpg", **PHOTO_TIMEOUTS,
            ), f"{event.name}", PHOTO_ATTEMPT_TIMEOUT)

    async def _power_photos(self, bot: Bot, event: PowerLost) -> None:
        # Cameras and the Pi are on the UPS, so photos should still work.
        snapshots = await self._cameras.capture_all() if self._cameras.configured else []
        media = [
            InputMediaPhoto(await _for_telegram(snapshot.image), filename=f"{snapshot.camera}.jpg")
            for snapshot in snapshots if snapshot.image is not None
        ]
        if not media:
            return
        caption = f"Fotos del corte de luz de las {event.since:%H:%M}"
        for chat_id in self._config.telegram_allowed_chat_ids:
            await _send_alert(lambda: bot.send_media_group(
                chat_id, media, caption=caption, **PHOTO_TIMEOUTS,
            ), f"{event.name} photos", PHOTO_ATTEMPT_TIMEOUT)

    async def _announce_restart(self, bot: Bot) -> None:
        # Cameras and Starlink are not checked yet at start; offline alerts follow if needed.
        text = f"Rancho reiniciado. {self._status_text(links=False)}"
        if self._alarm.load_failed:
            text += "\nNo se pudo leer el estado guardado; la alarma quedó desarmada."
        try:
            await self._broadcast(bot, text, "restart notice")
        except (TelegramError, TimeoutError) as exc:
            logger.warning("Restart notice failed (%s)", type(exc).__name__)

    async def _broadcast(self, bot: Bot, text: str, what: str = "message") -> None:
        for chat_id in self._config.telegram_allowed_chat_ids:
            await _send_alert(lambda: bot.send_message(chat_id, text, reply_markup=REPLY_MARKUP), what)

    def _mode_changed_text(self, event: ModeChanged) -> str:
        if event.mode == AlarmMode.ARMED:
            return f"Alarma armada ({event.by})"
        return f"Alarma desarmada ({event.by})"

    def _status_text(self, links: bool = True) -> str:
        alarm = self._alarm
        label = {
            AlarmMode.DISARMED: "Alarma desarmada",
            AlarmMode.ARMED: "Alarma armada",
        }[alarm.mode]
        text = f"{label} desde {alarm.since:%d/%m %H:%M}"
        if alarm.by:
            text += f" ({alarm.by})"
        if alarm.sleeping and alarm.sleep_until is not None:
            text += f"\nAlertas en pausa hasta {alarm.sleep_until:%d/%m %H:%M}"
        power = self._power
        if power is not None:
            if not power.available:
                text += "\nSensor de luz no disponible (revisa el journal)"
            elif power.present is False and power.off_at is not None:
                text += f"\nSin luz desde las {power.off_at:%H:%M}"
            elif power.present:
                text += "\nLuz: normal"
        if self._siren is not None and not self._siren.available:
            text += "\nSirena no disponible (revisa el journal)"
        if links and self._cameras.configured:
            text += "\nCámaras:"
            for status in self._cameras.status():
                text += f"\n  {status.camera}: {_link_state(status.online, status.since)}"
        if links and self._starlink is not None:
            text += f"\nAntena Starlink: {_link_state(self._starlink.online, self._starlink.down_at)}"
        if links and self._sensors is not None and self._sensors.configured:
            if self._sensors.online is False:
                text += f"\nZ-Wave: {_link_state(False, self._sensors.down_at)}"
            text += "\nSensores:"
            for status in self._sensors.status():
                text += f"\n  {status.sensor}: {_sensor_state(status)}"
        if links and self._lights is not None and self._lights.configured:
            text += "\nLuces:"
            for status in self._lights.status():
                text += f"\n  {status.light}: {_light_state(status)}"
        return text

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
        try:
            armed = self._alarm.arm(self._who(update))
        except ArmingBlocked as blocked:
            await _send_with_retry(lambda: message.reply_text(
                f"No se puede armar: sensor abierto: {', '.join(blocked.sensors)}", reply_markup=REPLY_MARKUP
            ))
            return
        if not armed:
            await _send_with_retry(lambda: message.reply_text(
                "La alarma ya está armada.", reply_markup=REPLY_MARKUP
            ))

    async def _disarm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._allowed_message(update)
        if message is None:
            return
        if not self._alarm.disarm(self._who(update)):
            await _send_with_retry(lambda: message.reply_text(
                "La alarma ya está desarmada.", reply_markup=REPLY_MARKUP
            ))

    async def _status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._allowed_message(update)
        if message is None:
            return
        await _send_with_retry(lambda: message.reply_text(self._status_text(), reply_markup=REPLY_MARKUP))

    async def _help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._allowed_message(update)
        if message is None:
            return
        await _send_with_retry(lambda: message.reply_text(HELP_TEXT, reply_markup=REPLY_MARKUP))

    async def _pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._allowed_message(update)
        if message is None:
            return
        args = context.args or []
        who = self._who(update)
        try:
            hours = float(args[0]) if args else 1.0
            self._alarm.sleep(hours, who)
        except ValueError:
            await _send_with_retry(lambda: message.reply_text(
                f"Uso: /pausa <horas>, de 0 a {MAX_SLEEP_HOURS}. Ejemplo: /pausa 1",
                reply_markup=REPLY_MARKUP,
            ))
            return
        until = self._alarm.sleep_until
        # Silencing alerts concerns every allowed chat, like a mode change.
        text = (f"Alertas reactivadas ({who})" if until is None
                else f"Alertas en pausa hasta {until:%H:%M} ({who})")
        await self._broadcast(context.bot, text)

    async def _light_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._switch_lights(update, context, True)

    async def _light_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._switch_lights(update, context, False)

    async def _switch_lights(self, update: Update, context: ContextTypes.DEFAULT_TYPE, on: bool) -> None:
        message = self._allowed_message(update)
        if message is None:
            return
        lights = self._lights
        if lights is None or not lights.configured:
            await _send_with_retry(lambda: message.reply_text(
                "No hay luces en config.yaml.", reply_markup=REPLY_MARKUP
            ))
            return
        # A name may have spaces; no name means every light.
        asked = " ".join(context.args or [])
        known = {name.casefold(): name for name in lights.names()}
        if asked and asked.casefold() not in known:
            await _send_with_retry(lambda: message.reply_text(
                f"No hay una luz «{asked}». Luces: {', '.join(known.values())}", reply_markup=REPLY_MARKUP
            ))
            return
        lines = []
        for name in [known[asked.casefold()]] if asked else known.values():
            if await lights.switch(name, on):
                lines.append(f"{name}: {'encendida' if on else 'apagada'} hasta su próximo horario")
            else:
                lines.append(f"{name}: sin respuesta, no se pudo {'encender' if on else 'apagar'}")
        await _send_with_retry(lambda: message.reply_text("\n".join(lines), reply_markup=REPLY_MARKUP))

    async def _photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = self._allowed_message(update)
        if message is None:
            return
        if not self._cameras.configured:
            await _send_with_retry(lambda: message.reply_text(
                "Configura las cámaras en config.yaml y CAMERA_USERNAME y "
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
                # Bind this photo for every retry. Images stay in memory.
                async def send_photo(snapshot=snapshot, image=await _for_telegram(snapshot.image)):
                    return await message.reply_photo(
                        photo=image,
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

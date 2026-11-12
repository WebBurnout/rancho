"""Offline tests: no real token, polling, network, or hardware access."""

import asyncio
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import BadRequest, NetworkError

from rancho.core.cameras import CameraStatus, Snapshot
from rancho.core.config import Config
from rancho.core.events import (
    CameraOffline, CameraOnline, EventBus, Intrusion, ModeChanged, PersonPhoto, PowerLost, PowerRestored,
    SensorHeard, SensorIntrusion, SensorSilent, SensorTamper, Sighting, StarlinkOffline, StarlinkOnline,
    ZwaveOffline, ZwaveOnline,
)
from rancho.core.sensors import SensorStatus
from rancho.core.state import Alarm, AlarmMode
from rancho.frontends.telegram import HELP_TEXT, TelegramFrontend


class HelpTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        config = Config(
            state_file=Path("state.json"),
            telegram_bot_token="test-token",
            telegram_allowed_chat_ids=frozenset({-123}),
        )
        self.frontend = TelegramFrontend(config, EventBus())
        self.message = SimpleNamespace(reply_text=AsyncMock())

    def update(self, chat_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id),
            effective_message=self.message,
        )

    async def test_allowed_group_gets_help(self) -> None:
        await self.frontend._help(self.update(-123), None)
        self.message.reply_text.assert_awaited_once_with(HELP_TEXT, reply_markup=None)
        self.assertIn("/foto", HELP_TEXT)

    async def test_other_chat_is_ignored(self) -> None:
        await self.frontend._help(self.update(456), None)
        self.message.reply_text.assert_not_awaited()

    async def test_missing_message_is_ignored(self) -> None:
        update = SimpleNamespace(effective_chat=None, effective_message=None)
        await self.frontend._help(update, None)
        self.message.reply_text.assert_not_awaited()

    async def test_send_retries_after_network_failure(self) -> None:
        self.message.reply_text.side_effect = [NetworkError("offline"), None]
        with patch("rancho.frontends.telegram.asyncio.sleep", new_callable=AsyncMock):
            await self.frontend._help(self.update(-123), None)
        self.assertEqual(self.message.reply_text.await_count, 2)

    async def test_send_stops_after_three_attempts(self) -> None:
        self.message.reply_text.side_effect = NetworkError("offline")
        with patch("rancho.frontends.telegram.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(NetworkError):
                await self.frontend._help(self.update(-123), None)
        self.assertEqual(self.message.reply_text.await_count, 3)


class PhotoTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        config = Config(
            state_file=Path("state.json"),
            telegram_bot_token="test-token", telegram_allowed_chat_ids=frozenset({-123}),
        )
        self.cameras = Mock(configured=True, capture_all=AsyncMock())
        self.frontend = TelegramFrontend(config, EventBus(), self.cameras)
        self.message = SimpleNamespace(reply_text=AsyncMock(), reply_photo=AsyncMock())
        self.update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-123), effective_message=self.message,
        )

    async def test_unauthorized_chat_does_not_contact_cameras(self) -> None:
        self.update.effective_chat.id = 456
        await self.frontend._photo(self.update, None)
        self.cameras.capture_all.assert_not_awaited()
        self.message.reply_text.assert_not_awaited()
        self.message.reply_photo.assert_not_awaited()

    async def test_missing_credentials_does_not_contact_cameras(self) -> None:
        self.cameras.configured = False
        await self.frontend._photo(self.update, None)
        self.cameras.capture_all.assert_not_awaited()
        self.assertIn("CAMERA_USERNAME", self.message.reply_text.call_args.args[0])

    async def test_sends_available_photo_and_reports_failed_camera(self) -> None:
        self.cameras.capture_all.return_value = [
            Snapshot("Bomba", image=b"jpeg"),
            Snapshot("Patio", error="unreachable"),
        ]
        await self.frontend._photo(self.update, None)
        self.message.reply_photo.assert_awaited_once()
        self.assertEqual(self.message.reply_photo.call_args.kwargs["photo"], b"jpeg")
        self.assertNotIn("caption", self.message.reply_photo.call_args.kwargs)
        self.assertIn("Patio: unreachable", self.message.reply_text.call_args.args[0])

    async def test_failed_upload_does_not_block_other_photos(self) -> None:
        self.cameras.capture_all.return_value = [
            Snapshot("Bomba", image=b"first"),
            Snapshot("Patio", image=b"second"),
        ]
        self.message.reply_photo.side_effect = [BadRequest("invalid image"), None]
        await self.frontend._photo(self.update, None)
        self.assertEqual(self.message.reply_photo.await_count, 2)
        self.assertIn("Bomba: no se pudo enviar la foto a Telegram", self.message.reply_text.call_args.args[0])


class IntrusionAlertTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        config = Config(
            state_file=Path("state.json"),
            telegram_bot_token="test-token", telegram_allowed_chat_ids=frozenset({-123}),
        )
        self.cameras = Mock(configured=True, capture=AsyncMock())
        self.frontend = TelegramFrontend(config, EventBus(), self.cameras)
        self.bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())

    async def handle(self, event) -> None:
        await self.frontend._handle_event(self.bot, event)
        await asyncio.gather(*self.frontend._uploads)

    async def test_sends_photo_from_detecting_camera(self) -> None:
        self.cameras.capture.return_value = Snapshot("Patio", image=b"jpeg")
        await self.handle(Intrusion(camera="Patio"))
        self.cameras.capture.assert_awaited_once_with("Patio")
        args, kwargs = self.bot.send_photo.call_args
        self.assertEqual(args, (-123, b"jpeg"))
        self.assertEqual(kwargs["caption"], "Persona detectada: Patio")

    async def test_caption_says_whether_siren_sounded(self) -> None:
        self.cameras.capture.return_value = Snapshot("Patio", image=b"jpeg")
        await self.handle(Intrusion(camera="Patio", siren=True))
        self.assertEqual(self.bot.send_photo.call_args.kwargs["caption"], "Persona detectada: Patio\nSirena activada")
        await self.handle(Intrusion(camera="Patio", unconfirmed=True))
        self.assertIn("De noche la sirena", self.bot.send_photo.call_args.kwargs["caption"])

    async def test_animal_sighting_sends_photo(self) -> None:
        self.cameras.capture.return_value = Snapshot("Bomba", image=b"jpeg")
        await self.handle(Sighting(camera="Bomba", kind="animal"))
        self.cameras.capture.assert_awaited_once_with("Bomba")
        self.assertEqual(self.bot.send_photo.call_args.kwargs["caption"], "Animal detectado: Bomba")

    async def test_follow_up_photo_is_sent_with_elapsed_time(self) -> None:
        await self.handle(PersonPhoto(camera="Patio", image=b"jpeg", seconds=20))
        args, kwargs = self.bot.send_photo.call_args
        self.assertEqual(args, (-123, b"jpeg"))
        self.assertEqual(kwargs["caption"], "Patio: persona aún detectada (20 s)")
        self.cameras.capture.assert_not_awaited()

    async def test_failed_snapshot_still_alerts(self) -> None:
        self.cameras.capture.return_value = Snapshot("Patio", error="aún no conectada")
        await self.handle(Intrusion(camera="Patio"))
        self.bot.send_photo.assert_not_awaited()
        text = self.bot.send_message.call_args.args[1]
        self.assertIn("Persona detectada: Patio", text)
        self.assertIn("aún no conectada", text)

    async def test_alert_retries_through_failover(self) -> None:
        self.cameras.capture.return_value = Snapshot("Patio", image=b"jpeg")
        self.bot.send_photo.side_effect = [NetworkError("offline")] * 4 + [None]
        with patch("rancho.frontends.telegram.asyncio.sleep", new_callable=AsyncMock):
            await self.handle(Intrusion(camera="Patio"))
        self.assertEqual(self.bot.send_photo.await_count, 5)

    async def test_failed_alert_does_not_raise(self) -> None:
        self.cameras.capture.side_effect = RuntimeError("boom")
        await self.handle(Intrusion(camera="Patio"))
        self.bot.send_message.assert_not_awaited()

    async def test_camera_offline_is_broadcast(self) -> None:
        since = datetime(2026, 11, 10, 15, 16)
        await self.frontend._handle_event(self.bot, CameraOffline(camera="Cocina", since=since))
        self.assertEqual(self.bot.send_message.call_args.args,
                         (-123, "Cámara sin conexión: Cocina (desde las 15:16)"))

    async def test_camera_online_reports_duration(self) -> None:
        await self.frontend._handle_event(self.bot, CameraOnline(camera="Cocina", down_seconds=3900))
        self.assertEqual(self.bot.send_message.call_args.args,
                         (-123, "Cámara reconectada: Cocina (sin conexión 1 h 5 min)"))


class PowerAlertTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        config = Config(
            state_file=Path("state.json"),
            telegram_bot_token="test-token", telegram_allowed_chat_ids=frozenset({-123}),
        )
        self.cameras = Mock(configured=True, capture_all=AsyncMock())
        self.frontend = TelegramFrontend(config, EventBus(), self.cameras)
        self.bot = SimpleNamespace(send_media_group=AsyncMock(), send_message=AsyncMock())
        self.since = datetime(2026, 11, 10, 21, 5)

    async def test_power_cut_sends_text_before_taking_photos(self) -> None:
        self.cameras.capture_all.return_value = [
            Snapshot("Patio", image=b"one"), Snapshot("Bomba", error="offline"), Snapshot("Puerta", image=b"two"),
        ]
        await self.frontend._handle_event(self.bot, PowerLost(since=self.since))
        self.assertEqual(self.bot.send_message.call_args.args, (-123, "Corte de luz desde las 21:05"))
        self.cameras.capture_all.assert_not_awaited()
        await asyncio.gather(*self.frontend._uploads)
        args, kwargs = self.bot.send_media_group.call_args
        self.assertEqual(args[0], -123)
        self.assertEqual(len(args[1]), 2)
        self.assertEqual(kwargs["caption"], "Fotos del corte de luz de las 21:05")

    async def test_power_cut_without_cameras_sends_only_text(self) -> None:
        self.cameras.configured = False
        await self.frontend._handle_event(self.bot, PowerLost(since=self.since))
        await asyncio.gather(*self.frontend._uploads)
        self.assertEqual(self.bot.send_message.call_args.args, (-123, "Corte de luz desde las 21:05"))
        self.bot.send_media_group.assert_not_awaited()

    async def test_hung_send_is_cut_off_and_retried(self) -> None:
        calls = []

        async def send_message(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                await asyncio.Event().wait()
        self.bot.send_message = send_message
        with patch("rancho.frontends.telegram.TEXT_ATTEMPT_TIMEOUT", 0.01), \
                patch("rancho.frontends.telegram.asyncio.sleep", new_callable=AsyncMock):
            await self.frontend._handle_event(self.bot, PowerLost(since=self.since))
        self.assertEqual(len(calls), 2)

    async def test_alert_gives_up_at_deadline(self) -> None:
        self.cameras.configured = False
        self.bot.send_message.side_effect = NetworkError("offline")
        with patch("rancho.frontends.telegram.ALERT_DEADLINE", 0), \
                self.assertLogs("rancho.frontends.telegram", "WARNING") as logs:
            await self.frontend._handle_event(self.bot, PowerLost(since=self.since))
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn("Alert for power_lost failed (NetworkError)", logs.output[-1])

    async def test_starlink_alerts(self) -> None:
        await self.frontend._handle_event(self.bot, StarlinkOffline(since=self.since))
        self.assertTrue(self.bot.send_message.call_args.args[1].startswith(
            "Antena Starlink sin conexión desde las 21:05"))
        await self.frontend._handle_event(self.bot, StarlinkOnline(down_seconds=600))
        self.assertEqual(self.bot.send_message.call_args.args,
                         (-123, "Antena Starlink conectada de nuevo (sin conexión 10 min)"))

    async def test_sensor_alerts(self) -> None:
        cases = [
            (SensorIntrusion(sensor="Cocina"), "Sensor abierto: Cocina\nSirena activada"),
            (SensorTamper(sensor="Cocina"), "Tapa del sensor quitada: Cocina"),
            (SensorSilent(sensor="Cocina", since=self.since),
             "Sensor sin señal desde las 21:05: Cocina. Revisa la batería o si está emparejado."),
            (SensorSilent(sensor="Cocina", since=None),
             "Sensor sin señal: Cocina. Revisa la batería o si está emparejado."),
            (SensorHeard(sensor="Cocina", silent_seconds=9 * 3600),
             "Sensor con señal de nuevo: Cocina (sin señal 9 h 0 min)"),
            (ZwaveOffline(since=self.since),
             "Z-Wave sin conexión desde las 21:05: los sensores no funcionan. Revisa zwave.service."),
            (ZwaveOnline(down_seconds=120), "Z-Wave conectado de nuevo (sin conexión 2 min)"),
        ]
        for event, text in cases:
            with self.subTest(event.name):
                await self.frontend._handle_event(self.bot, event)
                self.assertEqual(self.bot.send_message.call_args.args, (-123, text))

    async def test_power_restored_reports_duration(self) -> None:
        await self.frontend._handle_event(self.bot, PowerRestored(down_seconds=600))
        self.assertEqual(self.bot.send_message.call_args.args, (-123, "Volvió la luz (sin luz 10 min)"))


class AlarmCommandTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        config = Config(
            state_file=Path(self.tmp.name) / "state.json",
            telegram_bot_token="test-token", telegram_allowed_chat_ids=frozenset({-123}),
        )
        self.bus = EventBus()
        self.open: list[str] = []
        self.alarm = Alarm(config.state_file, self.bus, open_sensors=lambda: self.open)
        self.cameras = Mock(configured=False)
        self.frontend = TelegramFrontend(config, self.bus, self.cameras, self.alarm)
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def context(self, *args: str) -> SimpleNamespace:
        return SimpleNamespace(args=list(args), bot=self.bot)

    def update(self, chat_id: int = -123, data: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id),
            effective_message=self.message,
            effective_user=SimpleNamespace(first_name="Tim"),
            callback_query=SimpleNamespace(data=data, answer=AsyncMock()),
        )

    async def test_arm_is_immediate_and_names_who(self) -> None:
        events = self.bus.subscribe()
        await self.frontend._arm(self.update(), None)
        self.assertEqual(self.alarm.mode, AlarmMode.ARMED)
        event = events.get_nowait()
        self.assertEqual(self.frontend._mode_changed_text(event), "Alarma armada (Tim)")
        self.message.reply_text.assert_not_awaited()

    async def test_arm_twice_says_already_armed(self) -> None:
        await self.frontend._arm(self.update(), None)
        await self.frontend._arm(self.update(), None)
        self.assertIn("ya está armada", self.message.reply_text.call_args.args[0])

    async def test_arm_with_open_sensor_is_refused(self) -> None:
        self.open = ["Cocina"]
        await self.frontend._arm(self.update(), None)
        self.assertEqual(self.alarm.mode, AlarmMode.DISARMED)
        self.assertEqual(self.message.reply_text.call_args.args[0], "No se puede armar: sensor abierto: Cocina")

    async def test_disarm_when_disarmed_says_so(self) -> None:
        await self.frontend._disarm(self.update(), None)
        self.assertIn("ya está desarmada", self.message.reply_text.call_args.args[0])

    async def test_other_chat_cannot_arm(self) -> None:
        await self.frontend._arm(self.update(chat_id=456), None)
        self.assertEqual(self.alarm.mode, AlarmMode.DISARMED)

    async def test_status_shows_mode_and_who(self) -> None:
        self.alarm.arm("Horacio")
        await self.frontend._status(self.update(), None)
        text = self.message.reply_text.call_args.args[0]
        self.assertTrue(text.startswith("Alarma armada desde "))
        self.assertTrue(text.endswith("(Horacio)"))

    async def test_status_shows_power_and_missing_siren(self) -> None:
        self.frontend._power = SimpleNamespace(available=True, present=False, off_at=datetime(2026, 11, 10, 21, 5))
        self.frontend._siren = SimpleNamespace(available=False)
        text = self.frontend._status_text()
        self.assertIn("\nSin luz desde las 21:05", text)
        self.assertIn("\nSirena no disponible", text)
        self.frontend._power = SimpleNamespace(available=True, present=True, off_at=None)
        self.frontend._siren = SimpleNamespace(available=True)
        self.assertTrue(self.frontend._status_text().endswith("\nLuz: normal"))
        self.frontend._starlink = SimpleNamespace(online=False, down_at=datetime(2026, 11, 10, 21, 7))
        self.assertTrue(self.frontend._status_text().endswith("\nAntena Starlink: sin conexión desde las 21:07"))
        self.frontend._starlink = SimpleNamespace(online=None, down_at=None)
        self.assertTrue(self.frontend._status_text().endswith("\nAntena Starlink: comprobando…"))
        self.frontend._starlink = SimpleNamespace(online=True, down_at=None)
        self.assertTrue(self.frontend._status_text().endswith("\nAntena Starlink: conectada"))

    async def test_status_lists_cameras(self) -> None:
        self.cameras.configured = True
        self.cameras.status.return_value = [
            CameraStatus("Patio", True),
            CameraStatus("Bomba", False, datetime(2026, 11, 10, 21, 5)),
            CameraStatus("Cocina", None),
        ]
        self.frontend._starlink = SimpleNamespace(online=True, down_at=None)
        text = self.frontend._status_text()
        self.assertTrue(text.endswith(
            "\nCámaras:\n  Patio: conectada\n  Bomba: sin conexión desde las 21:05\n  Cocina: comprobando…"
            "\nAntena Starlink: conectada"
        ))
        text = self.frontend._status_text(links=False)
        self.assertNotIn("Cámaras", text)
        self.assertNotIn("Starlink", text)

    async def test_status_lists_sensors(self) -> None:
        self.frontend._sensors = SimpleNamespace(configured=True, online=True, status=lambda: [
            SensorStatus("Cocina", False, False, 99, False, None),
            SensorStatus("Sala", True, True, None, True, datetime(2026, 11, 10, 3, 4)),
            SensorStatus("Baño", None, False, None, False, None),
        ])
        self.assertTrue(self.frontend._status_text().endswith(
            "\nSensores:\n  Cocina: cerrado, batería 99%"
            "\n  Sala: abierto, tapa quitada, sin señal desde las 03:04\n  Baño: comprobando…"
        ))
        self.frontend._sensors.online = False
        self.frontend._sensors.down_at = datetime(2026, 11, 10, 21, 5)
        self.assertIn("\nZ-Wave: sin conexión desde las 21:05\nSensores:", self.frontend._status_text())
        self.assertNotIn("Sensores", self.frontend._status_text(links=False))

    async def test_sleep_broadcasts_and_shows_in_status(self) -> None:
        await self.frontend._pause(self.update(), self.context("1"))
        self.assertTrue(self.alarm.sleeping)
        text = self.bot.send_message.call_args.args[1]
        self.assertTrue(text.startswith("Alertas en pausa hasta "))
        self.assertTrue(text.endswith("(Tim)"))
        await self.frontend._status(self.update(), None)
        self.assertIn("Alertas en pausa hasta", self.message.reply_text.call_args.args[0])

    async def test_sleep_defaults_to_one_hour(self) -> None:
        await self.frontend._pause(self.update(), self.context())
        self.assertTrue(self.alarm.sleeping)

    async def test_sleep_zero_resumes_alerts(self) -> None:
        await self.frontend._pause(self.update(), self.context("1"))
        await self.frontend._pause(self.update(), self.context("0"))
        self.assertFalse(self.alarm.sleeping)
        self.assertEqual(self.bot.send_message.call_args.args[1], "Alertas reactivadas (Tim)")

    async def test_sleep_with_bad_hours_shows_usage(self) -> None:
        for arg in ("una", "-1", "100"):
            with self.subTest(arg):
                await self.frontend._pause(self.update(), self.context(arg))
                self.assertIn("Uso: /pausa", self.message.reply_text.call_args.args[0])
        self.assertFalse(self.alarm.sleeping)
        self.bot.send_message.assert_not_awaited()

    async def test_other_chat_cannot_sleep(self) -> None:
        await self.frontend._pause(self.update(chat_id=456), self.context("1"))
        self.assertFalse(self.alarm.sleeping)

    async def test_button_runs_command_and_answers_query(self) -> None:
        update = self.update(data="armar")
        await self.frontend._button(update, None)
        update.callback_query.answer.assert_awaited_once()
        self.assertEqual(self.alarm.mode, AlarmMode.ARMED)

    async def test_button_from_other_chat_is_ignored(self) -> None:
        update = self.update(chat_id=456, data="armar")
        await self.frontend._button(update, None)
        update.callback_query.answer.assert_not_awaited()
        self.assertEqual(self.alarm.mode, AlarmMode.DISARMED)

    async def test_mode_change_is_broadcast_without_buttons(self) -> None:
        await self.frontend._handle_event(self.bot, ModeChanged(mode=AlarmMode.DISARMED.value, by="Tim"))
        self.bot.send_message.assert_awaited_once_with(
            -123, "Alarma desarmada (Tim)", reply_markup=None
        )

    async def test_restart_notice_reports_unreadable_state(self) -> None:
        self.alarm.load_failed = True
        await self.frontend._announce_restart(self.bot)
        text = self.bot.send_message.call_args.args[1]
        self.assertTrue(text.startswith("Rancho reiniciado. Alarma desarmada"))
        self.assertIn("No se pudo leer el estado guardado", text)

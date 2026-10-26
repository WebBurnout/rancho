"""Offline tests: no real token, polling, network, or hardware access."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import BadRequest, NetworkError

from rancho.core.cameras import Snapshot
from rancho.core.config import Config
from rancho.core.events import EventBus, Intrusion, ModeChanged
from rancho.core.state import Alarm, AlarmMode
from rancho.frontends.telegram import HELP_TEXT, KEYBOARD, TelegramFrontend


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
        self.message.reply_text.assert_awaited_once_with(HELP_TEXT, reply_markup=KEYBOARD)
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

    async def test_sends_photo_from_detecting_camera(self) -> None:
        self.cameras.capture.return_value = Snapshot("Patio", image=b"jpeg")
        await self.frontend._handle_event(self.bot, Intrusion(camera="Patio"))
        self.cameras.capture.assert_awaited_once_with("Patio")
        args, kwargs = self.bot.send_photo.call_args
        self.assertEqual(args, (-123, b"jpeg"))
        self.assertEqual(kwargs["caption"], "Persona detectada: Patio")

    async def test_failed_snapshot_still_alerts(self) -> None:
        self.cameras.capture.return_value = Snapshot("Patio", error="aún no conectada")
        await self.frontend._handle_event(self.bot, Intrusion(camera="Patio"))
        self.bot.send_photo.assert_not_awaited()
        text = self.bot.send_message.call_args.args[1]
        self.assertIn("Persona detectada: Patio", text)
        self.assertIn("aún no conectada", text)

    async def test_alert_retries_through_failover(self) -> None:
        self.cameras.capture.return_value = Snapshot("Patio", image=b"jpeg")
        self.bot.send_photo.side_effect = [NetworkError("offline")] * 4 + [None]
        with patch("rancho.frontends.telegram.asyncio.sleep", new_callable=AsyncMock):
            await self.frontend._handle_event(self.bot, Intrusion(camera="Patio"))
        self.assertEqual(self.bot.send_photo.await_count, 5)

    async def test_failed_alert_does_not_raise(self) -> None:
        self.cameras.capture.side_effect = RuntimeError("boom")
        await self.frontend._handle_event(self.bot, Intrusion(camera="Patio"))
        self.bot.send_message.assert_not_awaited()


class AlarmCommandTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        config = Config(
            state_file=Path(self.tmp.name) / "state.json",
            telegram_bot_token="test-token", telegram_allowed_chat_ids=frozenset({-123}),
        )
        self.bus = EventBus()
        self.alarm = Alarm(config.state_file, self.bus, exit_delay=3600)
        self.frontend = TelegramFrontend(config, self.bus, Mock(), self.alarm)
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())

    def tearDown(self) -> None:
        self.alarm.disarm("test")
        self.tmp.cleanup()

    def update(self, chat_id: int = -123, data: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=chat_id),
            effective_message=self.message,
            effective_user=SimpleNamespace(first_name="Tim"),
            callback_query=SimpleNamespace(data=data, answer=AsyncMock()),
        )

    async def test_arm_starts_exit_delay_and_names_who(self) -> None:
        events = self.bus.subscribe()
        await self.frontend._arm(self.update(), None)
        self.assertEqual(self.alarm.mode, AlarmMode.ARMING)
        event = events.get_nowait()
        self.assertEqual(self.frontend._mode_changed_text(event), "Armando… se arma en 3600 s (Tim)")
        self.message.reply_text.assert_not_awaited()

    async def test_arm_twice_says_already_arming(self) -> None:
        await self.frontend._arm(self.update(), None)
        await self.frontend._arm(self.update(), None)
        self.assertIn("ya se está armando", self.message.reply_text.call_args.args[0])

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
        self.assertTrue(text.startswith("Armando la alarma desde "))
        self.assertTrue(text.endswith("(Horacio)"))

    async def test_button_runs_command_and_answers_query(self) -> None:
        update = self.update(data="armar")
        await self.frontend._button(update, None)
        update.callback_query.answer.assert_awaited_once()
        self.assertEqual(self.alarm.mode, AlarmMode.ARMING)

    async def test_button_from_other_chat_is_ignored(self) -> None:
        update = self.update(chat_id=456, data="armar")
        await self.frontend._button(update, None)
        update.callback_query.answer.assert_not_awaited()
        self.assertEqual(self.alarm.mode, AlarmMode.DISARMED)

    async def test_mode_change_is_broadcast_with_buttons(self) -> None:
        await self.frontend._handle_event(self.bot, ModeChanged(mode=AlarmMode.DISARMED.value, by="Tim"))
        self.bot.send_message.assert_awaited_once_with(
            -123, "Alarma desarmada (Tim)", reply_markup=KEYBOARD
        )

    async def test_restart_notice_reports_unreadable_state(self) -> None:
        self.alarm.load_failed = True
        await self.frontend._announce_restart(self.bot)
        text = self.bot.send_message.call_args.args[1]
        self.assertTrue(text.startswith("Rancho reiniciado. Alarma desarmada"))
        self.assertIn("No se pudo leer el estado guardado", text)

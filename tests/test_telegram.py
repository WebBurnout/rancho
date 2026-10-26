"""Offline tests: no real token, polling, network, or hardware access."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import BadRequest, NetworkError

from rancho.core.cameras import Snapshot
from rancho.core.config import Config
from rancho.core.events import EventBus
from rancho.frontends.telegram import HELP_TEXT, TelegramFrontend


class HelpTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        config = Config(
            log_level="INFO",
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
        self.message.reply_text.assert_awaited_once_with(HELP_TEXT)
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
            log_level="INFO", state_file=Path("state.json"),
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
        now = datetime.now(timezone.utc)
        self.cameras.capture_all.return_value = [
            Snapshot("10.10.0.10", now, image=b"jpeg"),
            Snapshot("10.10.0.11", now, error="unreachable"),
        ]
        await self.frontend._photo(self.update, None)
        self.message.reply_photo.assert_awaited_once()
        self.assertEqual(self.message.reply_photo.call_args.kwargs["photo"], b"jpeg")
        self.assertIn("10.10.0.11: unreachable", self.message.reply_text.call_args.args[0])

    async def test_failed_upload_does_not_block_other_photos(self) -> None:
        now = datetime.now(timezone.utc)
        self.cameras.capture_all.return_value = [
            Snapshot("10.10.0.10", now, image=b"first"),
            Snapshot("10.10.0.11", now, image=b"second"),
        ]
        self.message.reply_photo.side_effect = [BadRequest("invalid image"), None]
        await self.frontend._photo(self.update, None)
        self.assertEqual(self.message.reply_photo.await_count, 2)
        self.assertIn("10.10.0.10: no se pudo enviar la foto a Telegram", self.message.reply_text.call_args.args[0])

"""Offline camera adapter tests; no reolink client or network is created."""

import asyncio
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from rancho.core.cameras import Cameras, Snapshot, _Camera
from rancho.core.config import CameraConfig, Config
from rancho.core.events import EventBus, PersonDetected

JPEG = b"\xff\xd8\xfftest-image"
BOMBA = CameraConfig(name="Bomba", host="10.10.0.10")


def config() -> Config:
    return Config(
        state_file=Path("state.json"),
        telegram_bot_token="test-token", telegram_allowed_chat_ids=frozenset({-123}),
        cameras=(BOMBA, CameraConfig(name="Patio", host="10.10.0.11")),
        camera_username="viewer", camera_password="test-password",
    )


def fake_client() -> Mock:
    client = Mock()
    client.get_host_data = AsyncMock()
    client.get_snapshot = AsyncMock(return_value=JPEG)
    client.logout = AsyncMock()
    client.ai_supported.return_value = True
    client.ai_detected.return_value = False
    client.baichuan.subscribe_events = AsyncMock()
    client.baichuan.unsubscribe_events = AsyncMock()
    client.baichuan.check_subscribe_events = AsyncMock()
    return client


class CameraTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.camera = _Camera(BOMBA, config(), self.bus)
        self.client = fake_client()
        self.camera._client = self.client
        self.camera._ready = True

    async def test_snapshot_uses_library_channel_zero(self) -> None:
        result = await self.camera.snapshot()
        self.client.get_snapshot.assert_awaited_once_with(0)
        self.assertEqual(result.image, JPEG)
        self.assertIsNone(result.error)

    async def test_empty_image_is_a_failure(self) -> None:
        self.client.get_snapshot.return_value = None
        result = await self.camera.snapshot()
        self.assertIsNone(result.image)
        self.assertIn("no devolvió un JPEG", result.error)

    async def test_non_jpeg_is_a_failure(self) -> None:
        self.client.get_snapshot.return_value = b'{"error":"auth"}'
        result = await self.camera.snapshot()
        self.assertIsNone(result.image)

    async def test_oversized_image_is_rejected(self) -> None:
        with patch("rancho.core.cameras.MAX_IMAGE_BYTES", 2):
            result = await self.camera.snapshot()
        self.assertIsNone(result.image)
        self.assertIn("demasiado grande", result.error)

    async def test_snapshot_errors_do_not_leak_credentials(self) -> None:
        self.client.get_snapshot.side_effect = TimeoutError("sensitive-password")
        result = await self.camera.snapshot()
        self.assertIsNone(result.image)
        self.assertIn("TimeoutError", result.error)
        self.assertNotIn("sensitive-password", result.error)

    async def test_snapshot_is_labeled_with_camera_name(self) -> None:
        result = await self.camera.snapshot()
        self.assertEqual(result.camera, "Bomba")

    async def test_not_connected_does_not_request_image(self) -> None:
        self.camera._ready = False
        result = await self.camera.snapshot()
        self.assertEqual(result.error, "aún no conectada")
        self.client.get_snapshot.assert_not_awaited()

    async def test_only_person_rising_edges_publish(self) -> None:
        queue = self.bus.subscribe()
        self.client.ai_detected.side_effect = [False, True, True, False, True]
        for _ in range(5):
            self.camera._on_push()
        self.assertEqual(queue.qsize(), 2)
        event = queue.get_nowait()
        self.assertIsInstance(event, PersonDetected)
        self.assertEqual(event.camera, "Bomba")
        self.assertEqual(event.name, "person_detected")
        self.client.ai_detected.assert_called_with(0, "person")

    async def test_true_first_push_is_not_lost(self) -> None:
        queue = self.bus.subscribe()
        self.client.ai_detected.return_value = True
        self.camera._on_push()
        self.assertEqual(queue.qsize(), 1)

    async def test_late_callback_after_close_is_ignored(self) -> None:
        queue = self.bus.subscribe()
        self.client.ai_detected.return_value = True
        await self.camera._close()
        self.camera._on_push()
        self.assertTrue(queue.empty())
        self.client.baichuan.unregister_callback.assert_called_once_with("rancho-person")
        self.client.baichuan.unsubscribe_events.assert_awaited_once()
        self.client.logout.assert_awaited_once()

    async def test_logout_still_happens_when_unsubscribe_fails(self) -> None:
        self.client.baichuan.unsubscribe_events.side_effect = TimeoutError()
        await self.camera._close()
        self.client.logout.assert_awaited_once()
        self.assertFalse(self.camera._ready)

    async def test_worker_subscribes_and_cleans_up_on_cancel(self) -> None:
        subscribed = asyncio.Event()
        self.client.baichuan.subscribe_events.side_effect = lambda: subscribed.set()
        camera = _Camera(BOMBA, config(), self.bus)
        with patch("rancho.core.cameras._new_host", return_value=self.client):
            task = asyncio.create_task(camera.run())
            try:
                await asyncio.wait_for(subscribed.wait(), timeout=1)
                self.client.get_host_data.assert_awaited_once()
                self.client.baichuan.register_callback.assert_called_once_with(
                    "rancho-person", camera._on_push, cmd_id=33, channel=0
                )
                self.assertFalse(camera._push_confirmed)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.client.baichuan.unsubscribe_events.assert_awaited_once()
        self.client.logout.assert_awaited_once()

    async def test_failed_initialization_is_cleaned_up(self) -> None:
        closed = asyncio.Event()
        self.client.get_host_data.side_effect = TimeoutError()
        self.client.logout.side_effect = lambda: closed.set()
        camera = _Camera(BOMBA, config(), self.bus)
        with patch("rancho.core.cameras._new_host", return_value=self.client):
            task = asyncio.create_task(camera.run())
            try:
                await asyncio.wait_for(closed.wait(), timeout=1)
                self.assertFalse(camera._ready)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.client.logout.assert_awaited_once()


class CamerasTests(IsolatedAsyncioTestCase):
    async def test_one_failed_camera_does_not_discard_other_photo(self) -> None:
        cameras = Cameras(config(), EventBus())
        cameras._cameras[0].snapshot = AsyncMock(return_value=Snapshot("Bomba", error="offline"))
        cameras._cameras[1].snapshot = AsyncMock(return_value=Snapshot("Patio", image=JPEG))
        results = await cameras.capture_all()
        self.assertIsNone(results[0].image)
        self.assertEqual(results[1].image, JPEG)

    async def test_capture_takes_only_the_named_camera(self) -> None:
        cameras = Cameras(config(), EventBus())
        cameras._cameras[0].snapshot = AsyncMock()
        cameras._cameras[1].snapshot = AsyncMock(return_value=Snapshot("Patio", image=JPEG))
        result = await cameras.capture("Patio")
        self.assertEqual(result.image, JPEG)
        cameras._cameras[0].snapshot.assert_not_awaited()

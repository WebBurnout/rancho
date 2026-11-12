"""Offline camera adapter tests; no reolink client or network is created."""

import asyncio
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from rancho.core.cameras import OFFLINE_AFTER, Cameras, CameraStatus, Snapshot, _Camera
from rancho.core.config import CameraConfig, Config
from rancho.core.events import (
    CameraOffline, CameraOnline, EventBus, ObjectDetected, PersonCleared, PersonDetected,
)

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

    async def test_only_person_transitions_publish(self) -> None:
        queue = self.bus.subscribe()
        self.client.ai_detected.side_effect = [False, True, True, False, True]
        for _ in range(5):
            self.camera._on_push()
        events = [queue.get_nowait() for _ in range(queue.qsize())]
        self.assertEqual([type(e) for e in events], [PersonDetected, PersonCleared, PersonDetected])
        self.assertEqual(events[0].camera, "Bomba")
        self.assertEqual(events[0].name, "person_detected")
        self.client.ai_detected.assert_called_with(0, "person")

    async def test_true_first_push_is_not_lost(self) -> None:
        queue = self.bus.subscribe()
        self.client.ai_detected.return_value = True
        self.camera._on_push()
        self.assertEqual(queue.qsize(), 1)

    async def test_animal_alerts_only_where_configured(self) -> None:
        queue = self.bus.subscribe()
        self.client.ai_detected.side_effect = lambda channel, kind: kind == "pet"
        self.camera._on_push()
        self.assertTrue(queue.empty())
        camera = _Camera(CameraConfig("Bomba", "10.10.0.10", alerts=("person", "animal")), config(), self.bus)
        camera._client, camera._ready = self.client, True
        camera._on_push()
        camera._on_push()
        events = [queue.get_nowait() for _ in range(queue.qsize())]
        self.assertEqual(events, [ObjectDetected(camera="Bomba", kind="animal", occurred_at=events[0].occurred_at)])

    async def test_disconnect_while_detected_clears_person(self) -> None:
        queue = self.bus.subscribe()
        self.client.ai_detected.return_value = True
        self.camera._on_push()
        await self.camera._close()
        events = [queue.get_nowait() for _ in range(queue.qsize())]
        self.assertEqual([type(e) for e in events], [PersonDetected, PersonCleared])

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


class HealthTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.queue = self.bus.subscribe()
        self.camera = _Camera(BOMBA, config(), self.bus)

    async def test_short_outage_is_not_reported(self) -> None:
        self.camera._check_health(False, 0)
        self.camera._check_health(False, OFFLINE_AFTER - 1)
        self.camera._check_health(True, OFFLINE_AFTER)
        self.assertTrue(self.queue.empty())

    async def test_long_outage_reports_once_then_recovery(self) -> None:
        self.camera._check_health(False, 100)
        for now in (100 + OFFLINE_AFTER, 200 + OFFLINE_AFTER, 300 + OFFLINE_AFTER):
            self.camera._check_health(False, now)
        offline = self.queue.get_nowait()
        self.assertIsInstance(offline, CameraOffline)
        self.assertEqual(offline.camera, "Bomba")
        self.assertTrue(self.queue.empty())
        self.camera._check_health(True, 400)
        online = self.queue.get_nowait()
        self.assertIsInstance(online, CameraOnline)
        self.assertEqual(online.down_seconds, 300)
        self.camera._check_health(True, 500)
        self.assertTrue(self.queue.empty())

    async def test_new_outage_after_recovery_is_reported_again(self) -> None:
        for online, now in [(False, 0), (False, OFFLINE_AFTER), (True, 100),
                            (False, 200), (False, 200 + OFFLINE_AFTER)]:
            self.camera._check_health(online, now)
        names = [self.queue.get_nowait().name for _ in range(self.queue.qsize())]
        self.assertEqual(names, ["camera_offline", "camera_online", "camera_offline"])

    async def test_status_follows_health_checks(self) -> None:
        self.assertIsNone(self.camera.status.online)
        self.camera._check_health(False, 0)
        self.assertFalse(self.camera.status.online)
        self.assertIsNotNone(self.camera.status.since)
        self.camera._check_health(True, 10)
        self.assertEqual(self.camera.status, CameraStatus("Bomba", True))

    async def test_not_ready_skips_probe_and_counts_as_down(self) -> None:
        self.camera._reachable = AsyncMock(return_value=True)
        self.camera._check_health = Mock()
        with patch("rancho.core.cameras.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with self.assertRaises(asyncio.CancelledError):
                await self.camera.monitor()
        self.camera._reachable.assert_not_awaited()
        self.assertFalse(self.camera._check_health.call_args.args[0])

    async def test_ready_and_reachable_counts_as_online(self) -> None:
        self.camera._ready = True
        self.camera._reachable = AsyncMock(return_value=True)
        self.camera._check_health = Mock()
        with patch("rancho.core.cameras.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with self.assertRaises(asyncio.CancelledError):
                await self.camera.monitor()
        self.assertTrue(self.camera._check_health.call_args.args[0])

    async def test_unreachable_when_connection_fails(self) -> None:
        with patch("rancho.core.cameras.asyncio.open_connection", new=AsyncMock(side_effect=OSError())):
            self.assertFalse(await self.camera._reachable())


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

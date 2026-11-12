"""Offline photo tests: a temporary archive folder and generated images."""

from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from PIL import Image

from rancho.core.cameras import Cameras, Snapshot
from rancho.core.config import CameraConfig, Config
from rancho.core.events import EventBus
from rancho.core.photos import PhotoArchive
from rancho.frontends.telegram import TELEGRAM_MAX_SIDE, _for_telegram


def jpeg(width: int, height: int) -> bytes:
    out = BytesIO()
    Image.new("RGB", (width, height), "gray").save(out, "JPEG")
    return out.getvalue()


class ArchiveTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name) / "photos"
        self.now = datetime(2026, 11, 11, 17, 45, 3, 250000)
        self.archive = self.new_archive()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def new_archive(self, max_bytes: int = 10**9) -> PhotoArchive:
        return PhotoArchive(self.root, now=lambda: self.now, max_bytes=max_bytes)

    def names(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir())

    async def save(self, camera: str, image: bytes, archive: PhotoArchive | None = None) -> None:
        await (archive or self.archive).save(camera, image)
        self.now += timedelta(seconds=1)

    async def test_save_writes_full_image_to_one_folder(self) -> None:
        await self.save("Patio", b"full")
        self.assertEqual((self.root / "2026-11-11_174503.250-Patio.jpg").read_bytes(), b"full")

    async def test_camera_name_cannot_escape_folder(self) -> None:
        await self.save("../Patio trasero", b"full")
        self.assertEqual(self.names(), ["2026-11-11_174503.250-___Patio_trasero.jpg"])

    async def test_failed_save_is_logged_not_raised(self) -> None:
        self.root.write_text("not a folder")
        with self.assertLogs("rancho.core.photos", "WARNING"):
            await self.save("Patio", b"full")

    async def test_oldest_photos_are_deleted_over_the_limit(self) -> None:
        archive = self.new_archive(max_bytes=10)
        for camera in ("Patio", "Bomba", "Puerta"):
            await self.save(camera, b"12345", archive)
        self.assertEqual(self.names(), ["2026-11-11_174504.250-Bomba.jpg", "2026-11-11_174505.250-Puerta.jpg"])
        self.assertEqual(archive.total_bytes, 10)

    async def test_load_counts_existing_photos_toward_the_limit(self) -> None:
        for camera in ("Patio", "Bomba"):
            await self.save(camera, b"12345")
        archive = self.new_archive(max_bytes=10)
        archive.load()
        self.assertEqual(archive.total_bytes, 10)
        await self.save("Puerta", b"12345", archive)
        self.assertNotIn("2026-11-11_174503.250-Patio.jpg", self.names())

    async def test_cameras_archive_every_captured_photo(self) -> None:
        config = Config(
            state_file=Path("state.json"), telegram_bot_token="", telegram_allowed_chat_ids=frozenset(),
            cameras=(CameraConfig("Patio", "10.10.0.10"), CameraConfig("Bomba", "10.10.0.12")),
            camera_username="viewer", camera_password="secret",
        )
        cameras = Cameras(config, EventBus(), self.archive)
        cameras._cameras[0].snapshot = AsyncMock(return_value=Snapshot("Patio", image=b"one"))
        cameras._cameras[1].snapshot = AsyncMock(return_value=Snapshot("Bomba", error="offline"))
        await cameras.capture_all()
        self.now += timedelta(seconds=1)
        await cameras.capture("Patio")
        self.assertEqual(self.names(), ["2026-11-11_174503.250-Patio.jpg", "2026-11-11_174504.250-Patio.jpg"])


class DownscaleTests(IsolatedAsyncioTestCase):
    async def test_camera_photo_is_halved_to_telegram_size(self) -> None:
        small = await _for_telegram(jpeg(2560, 1920))
        with Image.open(BytesIO(small)) as image:
            self.assertEqual(image.size, (TELEGRAM_MAX_SIDE, 960))

    async def test_small_photo_is_sent_unchanged(self) -> None:
        image = jpeg(640, 480)
        self.assertIs(await _for_telegram(image), image)

    async def test_undecodable_photo_is_sent_as_is(self) -> None:
        with self.assertLogs("rancho.frontends.telegram", "WARNING"):
            self.assertEqual(await _for_telegram(b"not a jpeg"), b"not a jpeg")

"""Reolink snapshots and native TCP person events, independent of front ends."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import logging
import time
from typing import TYPE_CHECKING

from rancho.core.config import DETECTION_TYPES, CameraConfig, Config
from rancho.core.events import (
    CameraOffline, CameraOnline, EventBus, ObjectDetected, PersonCleared, PersonDetected,
)
from rancho.core.photos import PhotoArchive

if TYPE_CHECKING:
    from reolink_aio.api import Host

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 9 * 1024 * 1024
CALLBACK_ID = "rancho-person"
BAICHUAN_PORT = 9000
# Seconds a camera must stay unreachable before alerting, and between checks.
OFFLINE_AFTER = 60
HEALTH_INTERVAL = 15


@dataclass(frozen=True)
class Snapshot:
    camera: str
    image: bytes | None = field(default=None, repr=False)
    error: str | None = None


def _new_host(address: str, config: Config) -> Host:
    # Imported only when starting a camera connection. Offline tests replace this
    # factory and never instantiate the library's network clients.
    from reolink_aio.api import Host

    # Baichuan (port 9000) only: setup, snapshots and push all work over it, and
    # some models (E1) have no HTTP API at all.
    return Host(
        host=address,
        username=config.camera_username,
        password=config.camera_password,
        bc_port=BAICHUAN_PORT,
        bc_only=True,
        timeout=10,
    )


class _Camera:
    def __init__(self, camera: CameraConfig, config: Config, events: EventBus) -> None:
        self.name = camera.name
        self.address = camera.host
        # Log both: names are for people, addresses identify the device.
        self.label = f"{camera.name} ({camera.host})"
        self._config = config
        self._events = events
        self._client: Host | None = None
        self._lock = asyncio.Lock()
        self._ready = False
        self._alerts = camera.alerts
        self._person: bool | None = None
        # Last state of each other alert type (vehicle, animal); None means unknown.
        self._objects: dict[str, bool | None] = {}
        self._subscribed = False
        self._push_confirmed = False
        # Monotonic time the current outage started; None while online.
        self._down_since: float | None = None
        self._down_at: datetime | None = None
        self._offline_reported = False

    def _on_push(self) -> None:
        client = self._client
        if client is None or not self._ready:
            return
        if not self._push_confirmed:
            self._push_confirmed = True
            logger.info("Camera %s: native push received", self.label)
        # Detections are logged by the alarm, and only while armed.
        for kind in self._alerts:
            detected = client.ai_detected(0, DETECTION_TYPES[kind])
            if kind == "person":
                if detected and self._person is not True:
                    logger.debug("Camera %s: person detected", self.label)
                    self._events.publish(PersonDetected(camera=self.name))
                elif not detected and self._person is True:
                    logger.debug("Camera %s: person cleared", self.label)
                    self._events.publish(PersonCleared(camera=self.name))
                self._person = detected
                continue
            if detected and self._objects.get(kind) is not True:
                logger.debug("Camera %s: %s detected", self.label, kind)
                self._events.publish(ObjectDetected(camera=self.name, kind=kind))
            self._objects[kind] = detected

    def _forget_person(self) -> None:
        # Unknown is not 'no person', but recording must stop with the connection.
        if self._person is True:
            self._events.publish(PersonCleared(camera=self.name))
        self._person = None
        self._objects.clear()

    async def run(self) -> None:
        while True:
            try:
                async with self._lock:
                    self._client = _new_host(self.address, self._config)
                    async with asyncio.timeout(90):
                        await self._client.get_host_data()
                    self._ready = True
                logger.info("Camera %s: connected; snapshots ready", self.label)
                supported = [k for k in self._alerts if self._client.ai_supported(0, DETECTION_TYPES[k])]
                for kind in self._alerts:
                    if kind not in supported:
                        logger.warning("Camera %s: %s detection not advertised by firmware/account", self.label, kind)
                self._subscribed = bool(supported)
                if self._subscribed:
                    self._client.baichuan.register_callback(
                        CALLBACK_ID, self._on_push, cmd_id=33, channel=0
                    )
                    await self._client.baichuan.subscribe_events()
                    logger.info("Camera %s: native push subscription requested on port 9000 (%s)",
                                self.label, ", ".join(supported))
                while True:
                    await asyncio.sleep(30)
                    if self._subscribed:
                        # The library maintains/reconnects the TCP subscription.
                        # This watchdog checks it, not the camera's AI state.
                        await self._client.baichuan.check_subscribe_events()
                        if not self._client.baichuan.session_active:
                            # Disconnected means unknown, not 'no person'. A true
                            # state after reconnect may legitimately emit again.
                            self._forget_person()
                            self._push_confirmed = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Library exceptions/URLs may contain credentials: log types only.
                logger.warning("Camera %s: connection failed (%s); retrying in 10s", self.label, type(exc).__name__)
            finally:
                await self._close()
            await asyncio.sleep(10)

    async def monitor(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            # The session can look alive after a silent power or cable loss, so
            # also confirm the camera still accepts TCP connections.
            online = self._ready and await self._reachable()
            self._check_health(online, time.monotonic())

    async def _reachable(self) -> bool:
        try:
            async with asyncio.timeout(5):
                _, writer = await asyncio.open_connection(self.address, BAICHUAN_PORT)
        except (OSError, TimeoutError):
            return False
        writer.close()
        try:
            async with asyncio.timeout(5):
                await writer.wait_closed()
        except (OSError, TimeoutError):
            pass
        return True

    def _check_health(self, online: bool, now: float) -> None:
        if online:
            if self._offline_reported and self._down_since is not None:
                down = now - self._down_since
                logger.info("Camera %s: back online after %.0fs", self.label, down)
                self._events.publish(CameraOnline(camera=self.name, down_seconds=down))
            self._down_since = self._down_at = None
            self._offline_reported = False
            return
        if self._down_since is None:
            self._down_since, self._down_at = now, datetime.now().astimezone()
        if not self._offline_reported and now - self._down_since >= OFFLINE_AFTER:
            self._offline_reported = True
            logger.warning("Camera %s: offline for %.0fs", self.label, now - self._down_since)
            assert self._down_at is not None
            self._events.publish(CameraOffline(camera=self.name, since=self._down_at))

    async def _close(self) -> None:
        async with self._lock:
            self._ready = False
            client, self._client = self._client, None
            self._forget_person()
            self._push_confirmed = False
            if client is None:
                return
            client.baichuan.unregister_callback(CALLBACK_ID)
            try:
                async with asyncio.timeout(5):
                    await client.baichuan.unsubscribe_events()
            except Exception as exc:
                logger.warning("Camera %s: unsubscribe failed (%s)", self.label, type(exc).__name__)
            finally:
                try:
                    async with asyncio.timeout(5):
                        await client.logout()
                except Exception as exc:
                    logger.warning("Camera %s: logout failed (%s)", self.label, type(exc).__name__)

    async def snapshot(self) -> Snapshot:
        if not self._ready:
            return Snapshot(self.name, error="aún no conectada")
        # Serialize snapshot requests against startup/cleanup of the shared Host.
        # TCP push callbacks do not need this lock.
        async with self._lock:
            if not self._ready or self._client is None:
                return Snapshot(self.name, error="aún no conectada")
            try:
                async with asyncio.timeout(20):
                    image = await self._client.get_snapshot(0)
                if not image or not image.startswith(b"\xff\xd8\xff"):
                    error = "la cámara no devolvió un JPEG"
                elif len(image) > MAX_IMAGE_BYTES:
                    error = "la foto es demasiado grande para enviarla"
                else:
                    return Snapshot(self.name, image=image)
            except Exception as exc:
                error = f"falló la foto ({type(exc).__name__}); revisa el acceso y las credenciales de la cámara"
            logger.warning("Camera %s: %s", self.label, error)
            return Snapshot(self.name, error=error)


class Cameras:
    def __init__(self, config: Config, events: EventBus, archive: PhotoArchive | None = None) -> None:
        self._config = config
        self._cameras = [_Camera(camera, config, events) for camera in config.cameras]
        self._archive = archive

    @property
    def configured(self) -> bool:
        return bool(self._cameras and self._config.camera_username and self._config.camera_password)

    async def run(self) -> None:
        # Keep library debug output off: it can include API URLs and payloads.
        # Our adapter reports camera addresses and exception types instead.
        logging.getLogger("reolink_aio").setLevel(logging.CRITICAL)
        if not self._cameras:
            logger.warning("Cameras disabled: no cameras in cameras.yaml")
            await asyncio.Future()
            return
        if not self.configured:
            logger.warning("Cameras disabled: set CAMERA_USERNAME and CAMERA_PASSWORD in .env")
            await asyncio.Future()
            return
        async with asyncio.TaskGroup() as tasks:
            for camera in self._cameras:
                tasks.create_task(camera.run(), name=f"camera-{camera.address}")
                tasks.create_task(camera.monitor(), name=f"camera-health-{camera.address}")

    async def capture(self, name: str) -> Snapshot:
        if not self.configured:
            raise ValueError("Cameras need cameras.yaml plus CAMERA_USERNAME and CAMERA_PASSWORD in .env")
        for camera in self._cameras:
            if camera.name == name:
                return await self._keep(await camera.snapshot())
        raise KeyError(name)

    async def capture_all(self) -> list[Snapshot]:
        if not self.configured:
            raise ValueError("Cameras need cameras.yaml plus CAMERA_USERNAME and CAMERA_PASSWORD in .env")
        snapshots = await asyncio.gather(*(camera.snapshot() for camera in self._cameras))
        return list(await asyncio.gather(*(self._keep(snapshot) for snapshot in snapshots)))

    async def _keep(self, snapshot: Snapshot) -> Snapshot:
        # Every full-resolution photo is archived before any front end downscales it.
        if self._archive is not None and snapshot.image is not None:
            await self._archive.save(snapshot.camera, snapshot.image)
        return snapshot

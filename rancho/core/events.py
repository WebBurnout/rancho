"""Small, in-process event bus; use only from the service's asyncio loop."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class Event:
    name: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, kw_only=True)
class PersonDetected(Event):
    camera: str
    name: str = field(default="person_detected", init=False)


@dataclass(frozen=True, kw_only=True)
class PersonCleared(Event):
    """The camera no longer reports a person, or its connection dropped."""

    camera: str
    name: str = field(default="person_cleared", init=False)


@dataclass(frozen=True, kw_only=True)
class PersonPhoto(Event):
    """A follow-up photo while a person stays in view, for the front ends."""

    camera: str
    image: bytes = field(repr=False)
    seconds: int
    name: str = field(default="person_photo", init=False)


@dataclass(frozen=True, kw_only=True)
class ObjectDetected(Event):
    """A camera started detecting a vehicle or animal (persons use PersonDetected)."""

    camera: str
    kind: str  # "vehicle" or "animal"
    name: str = field(default="object_detected", init=False)


@dataclass(frozen=True, kw_only=True)
class Sighting(Event):
    """An ObjectDetected outside its cooldown: send a photo, never the siren."""

    camera: str
    kind: str
    name: str = field(default="sighting", init=False)


@dataclass(frozen=True, kw_only=True)
class Intrusion(Event):
    """A person detected while armed, outside the cooldown."""

    camera: str
    # Whether this detection sounded the siren, or is waiting for night confirmation.
    siren: bool = False
    unconfirmed: bool = False
    name: str = field(default="intrusion", init=False)


@dataclass(frozen=True, kw_only=True)
class SirenTriggered(Event):
    source: str  # the camera or sensor
    seconds: float
    name: str = field(default="siren_triggered", init=False)


@dataclass(frozen=True, kw_only=True)
class PowerLost(Event):
    """Mains has been off for the configured delay; published once per outage."""

    since: datetime
    name: str = field(default="power_lost", init=False)


@dataclass(frozen=True, kw_only=True)
class PowerRestored(Event):
    """Mains is back after an outage that was reported."""

    down_seconds: float
    name: str = field(default="power_restored", init=False)


@dataclass(frozen=True, kw_only=True)
class CameraOffline(Event):
    """A camera has been unreachable for OFFLINE_AFTER; published once per outage."""

    camera: str
    since: datetime
    name: str = field(default="camera_offline", init=False)


@dataclass(frozen=True, kw_only=True)
class CameraOnline(Event):
    """A camera that was reported offline is reachable again."""

    camera: str
    down_seconds: float
    name: str = field(default="camera_online", init=False)


@dataclass(frozen=True, kw_only=True)
class StarlinkOffline(Event):
    """The dish has been unreachable with mains present; published once per outage."""

    since: datetime
    name: str = field(default="starlink_offline", init=False)


@dataclass(frozen=True, kw_only=True)
class StarlinkOnline(Event):
    """A dish that was reported offline answers again."""

    down_seconds: float
    name: str = field(default="starlink_online", init=False)


@dataclass(frozen=True, kw_only=True)
class SensorOpened(Event):
    """A door/window sensor went from closed to open."""

    sensor: str
    name: str = field(default="sensor_opened", init=False)


@dataclass(frozen=True, kw_only=True)
class SensorClosed(Event):
    sensor: str
    name: str = field(default="sensor_closed", init=False)


@dataclass(frozen=True, kw_only=True)
class SensorIntrusion(Event):
    """A sensor opened while armed; the siren sounds."""

    sensor: str
    name: str = field(default="sensor_intrusion", init=False)


@dataclass(frozen=True, kw_only=True)
class SensorTamper(Event):
    """A sensor's cover was removed; published whether armed or not."""

    sensor: str
    name: str = field(default="sensor_tamper", init=False)


@dataclass(frozen=True, kw_only=True)
class SensorSilent(Event):
    """A sensor missed its wake-ups; published once until it is heard again."""

    sensor: str
    # When it was last heard; None if never since Rancho started.
    since: datetime | None
    name: str = field(default="sensor_silent", init=False)


@dataclass(frozen=True, kw_only=True)
class SensorHeard(Event):
    """A sensor reported as silent was heard again."""

    sensor: str
    silent_seconds: float
    name: str = field(default="sensor_heard", init=False)


@dataclass(frozen=True, kw_only=True)
class ZwaveOffline(Event):
    """zwave.service has been unreachable for OFFLINE_AFTER; sensors are blind."""

    since: datetime
    name: str = field(default="zwave_offline", init=False)


@dataclass(frozen=True, kw_only=True)
class ZwaveOnline(Event):
    down_seconds: float
    name: str = field(default="zwave_online", init=False)


@dataclass(frozen=True, kw_only=True)
class ModeChanged(Event):
    mode: str  # an AlarmMode value
    by: str
    name: str = field(default="mode_changed", init=False)


class EventBus:
    """Broadcast to independent queues; queues are intentionally unbounded for now."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: Event) -> None:
        for queue in self._subscribers:
            queue.put_nowait(event)

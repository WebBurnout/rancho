"""Load settings from the checkout's .env and config.yaml, without logging secrets."""

from dataclasses import dataclass, field
from datetime import time
from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal, NamedTuple

import yaml
from dotenv import dotenv_values


# What a camera can alert on (config.yaml `alerts`), and reolink-aio's name for each.
DETECTION_TYPES = {"person": "person", "vehicle": "vehicle", "animal": "pet"}


@dataclass(frozen=True)
class CameraConfig:
    name: str
    host: str
    alerts: tuple[str, ...] = ("person",)


@dataclass(frozen=True)
class SensorConfig:
    name: str
    node: int


# A light's `on_at`/`off_at`: a time of day, or civil dusk at `location`.
Moment = time | Literal["dusk"]


@dataclass(frozen=True)
class LightConfig:
    name: str
    node: int
    on: Moment
    off: Moment
    # Each switch time shifts randomly by up to this many minutes either way.
    jitter: int = 30


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float


class Devices(NamedTuple):
    cameras: tuple[CameraConfig, ...] = ()
    sensors: tuple[SensorConfig, ...] = ()
    lights: tuple[LightConfig, ...] = ()
    location: Location | None = None


@dataclass(frozen=True)
class Config:
    state_file: Path
    telegram_bot_token: str = field(repr=False)
    telegram_allowed_chat_ids: frozenset[int]
    cameras: tuple[CameraConfig, ...] = ()
    sensors: tuple[SensorConfig, ...] = ()
    lights: tuple[LightConfig, ...] = ()
    location: Location | None = None
    camera_username: str = ""
    camera_password: str = field(default="", repr=False)
    siren_active_low: bool = False


def _parse_bool(name: str, value: str | None) -> bool:
    text = (value or "false").strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no", ""}:
        return False
    raise ValueError(f"{name} must be true or false")


SECTIONS = ("cameras", "sensors", "lights", "location")


def load_devices(path: Path) -> Devices:
    """Devices from config.yaml; a missing file means none."""
    if not path.is_file():
        return Devices()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name} is not valid YAML: {exc}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict) or not set(data) <= set(SECTIONS):
        raise ValueError(f"{path.name} may only contain {', '.join(repr(s) for s in SECTIONS)}")
    for key in ("cameras", "sensors", "lights"):
        if not isinstance(data.get(key) or [], list):
            raise ValueError(f"{path.name}: '{key}' must be a list")
    sensors = _sensors(path, data.get("sensors") or [])
    lights = _lights(path, data.get("lights") or [])
    location = _location(path, data.get("location"))
    if {light.node for light in lights} & {sensor.node for sensor in sensors}:
        raise ValueError(f"{path.name}: a node cannot be both a sensor and a light")
    if location is None and any("dusk" in (light.on, light.off) for light in lights):
        raise ValueError(f"{path.name}: lights using 'dusk' need a 'location'")
    return Devices(_cameras(path, data.get("cameras") or []), sensors, lights, location)


def _cameras(path: Path, items: list) -> tuple[CameraConfig, ...]:
    cameras = []
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not {"name", "host"} <= set(item) <= {"name", "host", "alerts"}:
            raise ValueError(f"{path.name}: camera {number} must have 'name' and 'host' (and optionally 'alerts')")
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path.name}: camera {number} needs a non-empty name")
        try:
            host = str(IPv4Address(str(item["host"])))
        except ValueError:
            raise ValueError(f"{path.name}: camera {number} host must be an IPv4 address") from None
        alerts = item.get("alerts", ["person"])
        if (not isinstance(alerts, list) or not all(isinstance(a, str) for a in alerts)
                or not set(alerts) <= set(DETECTION_TYPES) or len(set(alerts)) != len(alerts)):
            raise ValueError(f"{path.name}: camera {number} alerts must be a list of {', '.join(DETECTION_TYPES)}")
        cameras.append(CameraConfig(name=name.strip(), host=host, alerts=tuple(alerts)))
    names = [camera.name.casefold() for camera in cameras]
    hosts = [camera.host for camera in cameras]
    if len(set(names)) != len(names) or len(set(hosts)) != len(hosts):
        raise ValueError(f"{path.name}: camera names and hosts must be unique")
    return tuple(cameras)


def _name_and_node(path: Path, kind: str, number: int, item: dict) -> tuple[str, int]:
    name, node = item["name"], item["node"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{path.name}: {kind} {number} needs a non-empty name")
    # 1 is the controller; Long Range nodes go up to 4000.
    if not isinstance(node, int) or isinstance(node, bool) or not 2 <= node <= 4000:
        raise ValueError(f"{path.name}: {kind} {number} node must be a Z-Wave node number (2-4000)")
    return name.strip(), node


def _sensors(path: Path, items: list) -> tuple[SensorConfig, ...]:
    sensors = []
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict) or set(item) != {"name", "node"}:
            raise ValueError(f"{path.name}: sensor {number} must have 'name' and 'node'")
        name, node = _name_and_node(path, "sensor", number, item)
        sensors.append(SensorConfig(name=name, node=node))
    names = [sensor.name.casefold() for sensor in sensors]
    nodes = [sensor.node for sensor in sensors]
    if len(set(names)) != len(names) or len(set(nodes)) != len(nodes):
        raise ValueError(f"{path.name}: sensor names and nodes must be unique")
    return tuple(sensors)


def _moment(value: object) -> Moment | None:
    if value == "dusk":
        return "dusk"
    # YAML reads an unquoted 19:00 as a sexagesimal integer (1140); require a string.
    if not isinstance(value, str):
        return None
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except ValueError:
        return None


def _lights(path: Path, items: list) -> tuple[LightConfig, ...]:
    lights = []
    # Not `on`/`off`: YAML 1.1 reads those keys as booleans.
    required = {"name", "node", "on_at", "off_at"}
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not required <= set(item) <= required | {"jitter"}:
            raise ValueError(
                f"{path.name}: light {number} must have 'name', 'node', 'on_at' and 'off_at' (and optionally 'jitter')"
            )
        name, node = _name_and_node(path, "light", number, item)
        on, off = _moment(item["on_at"]), _moment(item["off_at"])
        if on is None or off is None:
            raise ValueError(f"{path.name}: light {number} 'on_at' and 'off_at' must be \"HH:MM\" (quoted) or dusk")
        if on == off:
            raise ValueError(f"{path.name}: light {number} 'on_at' and 'off_at' must differ")
        jitter = item.get("jitter", 30)
        if not isinstance(jitter, int) or isinstance(jitter, bool) or not 0 <= jitter <= 120:
            raise ValueError(f"{path.name}: light {number} jitter must be minutes (0-120)")
        lights.append(LightConfig(name=name, node=node, on=on, off=off, jitter=jitter))
    names = [light.name.casefold() for light in lights]
    nodes = [light.node for light in lights]
    if len(set(names)) != len(names) or len(set(nodes)) != len(nodes):
        raise ValueError(f"{path.name}: light names and nodes must be unique")
    return tuple(lights)


def _location(path: Path, item: object) -> Location | None:
    if item is None:
        return None
    if not isinstance(item, dict) or set(item) != {"latitude", "longitude"}:
        raise ValueError(f"{path.name}: 'location' must have 'latitude' and 'longitude'")
    latitude, longitude = item["latitude"], item["longitude"]
    if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (latitude, longitude))
            or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
        raise ValueError(f"{path.name}: 'location' latitude and longitude must be degrees")
    return Location(float(latitude), float(longitude))


def load_config(path: Path = Path(".env")) -> Config:
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"Configuration file missing: {path}")
    values = dotenv_values(path, interpolate=False)
    try:
        chat_ids = frozenset(
            int(value.strip())
            for value in (values.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").split(",")
            if value.strip()
        )
    except ValueError:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS must contain comma-separated integers") from None
    devices = load_devices(path.parent / "config.yaml")
    return Config(
        state_file=path.parent / "state.json",
        telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN") or "",
        telegram_allowed_chat_ids=chat_ids,
        cameras=devices.cameras,
        sensors=devices.sensors,
        lights=devices.lights,
        location=devices.location,
        camera_username=values.get("CAMERA_USERNAME") or "",
        camera_password=values.get("CAMERA_PASSWORD") or "",
        siren_active_low=_parse_bool("SIREN_ACTIVE_LOW", values.get("SIREN_ACTIVE_LOW")),
    )

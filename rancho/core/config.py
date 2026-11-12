"""Load settings from the checkout's .env and config.yaml, without logging secrets."""

from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path

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


@dataclass(frozen=True)
class Config:
    state_file: Path
    telegram_bot_token: str = field(repr=False)
    telegram_allowed_chat_ids: frozenset[int]
    cameras: tuple[CameraConfig, ...] = ()
    sensors: tuple[SensorConfig, ...] = ()
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


def load_devices(path: Path) -> tuple[tuple[CameraConfig, ...], tuple[SensorConfig, ...]]:
    """Cameras and sensors from config.yaml; a missing file means neither."""
    if not path.is_file():
        return (), ()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name} is not valid YAML: {exc}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict) or not set(data) <= {"cameras", "sensors"}:
        raise ValueError(f"{path.name} may only contain 'cameras' and 'sensors'")
    for key in ("cameras", "sensors"):
        if not isinstance(data.get(key) or [], list):
            raise ValueError(f"{path.name}: '{key}' must be a list")
    return _cameras(path, data.get("cameras") or []), _sensors(path, data.get("sensors") or [])


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


def _sensors(path: Path, items: list) -> tuple[SensorConfig, ...]:
    sensors = []
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict) or set(item) != {"name", "node"}:
            raise ValueError(f"{path.name}: sensor {number} must have 'name' and 'node'")
        name, node = item["name"], item["node"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path.name}: sensor {number} needs a non-empty name")
        # 1 is the controller; Long Range nodes go up to 4000.
        if not isinstance(node, int) or isinstance(node, bool) or not 2 <= node <= 4000:
            raise ValueError(f"{path.name}: sensor {number} node must be a Z-Wave node number (2-4000)")
        sensors.append(SensorConfig(name=name.strip(), node=node))
    names = [sensor.name.casefold() for sensor in sensors]
    nodes = [sensor.node for sensor in sensors]
    if len(set(names)) != len(names) or len(set(nodes)) != len(nodes):
        raise ValueError(f"{path.name}: sensor names and nodes must be unique")
    return tuple(sensors)


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
    cameras, sensors = load_devices(path.parent / "config.yaml")
    return Config(
        state_file=path.parent / "state.json",
        telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN") or "",
        telegram_allowed_chat_ids=chat_ids,
        cameras=cameras,
        sensors=sensors,
        camera_username=values.get("CAMERA_USERNAME") or "",
        camera_password=values.get("CAMERA_PASSWORD") or "",
        siren_active_low=_parse_bool("SIREN_ACTIVE_LOW", values.get("SIREN_ACTIVE_LOW")),
    )

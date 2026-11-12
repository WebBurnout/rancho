"""Load settings from the checkout's .env and cameras.yaml, without logging secrets."""

from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path

import yaml
from dotenv import dotenv_values


# What a camera can alert on (cameras.yaml `alerts`), and reolink-aio's name for each.
DETECTION_TYPES = {"person": "person", "vehicle": "vehicle", "animal": "pet"}


@dataclass(frozen=True)
class CameraConfig:
    name: str
    host: str
    alerts: tuple[str, ...] = ("person",)


@dataclass(frozen=True)
class Config:
    state_file: Path
    telegram_bot_token: str = field(repr=False)
    telegram_allowed_chat_ids: frozenset[int]
    cameras: tuple[CameraConfig, ...] = ()
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


def load_cameras(path: Path) -> tuple[CameraConfig, ...]:
    if not path.is_file():
        return ()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name} is not valid YAML: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("cameras"), list):
        raise ValueError(f"{path.name} must contain a 'cameras' list")
    cameras = []
    for number, item in enumerate(data["cameras"], start=1):
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
    return Config(
        state_file=path.parent / "state.json",
        telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN") or "",
        telegram_allowed_chat_ids=chat_ids,
        cameras=load_cameras(path.parent / "cameras.yaml"),
        camera_username=values.get("CAMERA_USERNAME") or "",
        camera_password=values.get("CAMERA_PASSWORD") or "",
        siren_active_low=_parse_bool("SIREN_ACTIVE_LOW", values.get("SIREN_ACTIVE_LOW")),
    )

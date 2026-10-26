"""Load settings from the checkout's .env and cameras.yaml, without logging secrets."""

from dataclasses import dataclass, field
from ipaddress import IPv4Address
from pathlib import Path

import yaml
from dotenv import dotenv_values


@dataclass(frozen=True)
class CameraConfig:
    name: str
    host: str


@dataclass(frozen=True)
class Config:
    state_file: Path
    telegram_bot_token: str = field(repr=False)
    telegram_allowed_chat_ids: frozenset[int]
    cameras: tuple[CameraConfig, ...] = ()
    camera_username: str = ""
    camera_password: str = field(default="", repr=False)


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
        if not isinstance(item, dict) or set(item) != {"name", "host"}:
            raise ValueError(f"{path.name}: camera {number} must have exactly 'name' and 'host'")
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{path.name}: camera {number} needs a non-empty name")
        try:
            host = str(IPv4Address(str(item["host"])))
        except ValueError:
            raise ValueError(f"{path.name}: camera {number} host must be an IPv4 address") from None
        cameras.append(CameraConfig(name=name.strip(), host=host))
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
    )

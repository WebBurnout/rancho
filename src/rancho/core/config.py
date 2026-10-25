"""Load settings from the checkout's .env, without logging secrets."""

from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


@dataclass(frozen=True)
class Config:
    log_level: str
    state_file: Path
    telegram_bot_token: str = field(repr=False)
    telegram_allowed_chat_ids: frozenset[int]


def load_config(path: Path = Path(".env")) -> Config:
    path = path.resolve()
    if not path.is_file():
        raise ValueError(f"Configuration file missing: {path}")
    values = dotenv_values(path, interpolate=False)
    level = (values.get("RANCHO_LOG_LEVEL") or "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("RANCHO_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    state_file = Path(values.get("RANCHO_STATE_FILE") or "state.json")
    if not state_file.is_absolute():
        state_file = path.parent / state_file
    try:
        chat_ids = frozenset(
            int(value.strip())
            for value in (values.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").split(",")
            if value.strip()
        )
    except ValueError:
        raise ValueError("TELEGRAM_ALLOWED_CHAT_IDS must contain comma-separated integers") from None
    return Config(
        log_level=level,
        state_file=state_file,
        telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN") or "",
        telegram_allowed_chat_ids=chat_ids,
    )

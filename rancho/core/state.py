"""State types only; transitions and persistence are not implemented yet."""

from dataclasses import dataclass
from enum import Enum


class AlarmMode(str, Enum):
    DISARMED = "disarmed"
    ARMING = "arming"
    ARMED = "armed"


@dataclass
class AlarmState:
    mode: AlarmMode = AlarmMode.DISARMED

"""Hardware contracts only. No GPIO or network access in Step 1.

A real siren adapter must guarantee OFF on start/stop. Crash/boot safety also
requires appropriate relay wiring and GPIO biasing; this protocol cannot do that.
"""

from typing import Protocol


class PowerSensor(Protocol):
    async def is_power_present(self) -> bool: ...


class Siren(Protocol):
    async def set_enabled(self, enabled: bool) -> None: ...

    async def close(self) -> None:
        """Switch OFF and release resources."""
        ...


class Camera(Protocol):
    async def snapshot(self) -> bytes: ...

    async def is_person_detected(self) -> bool: ...

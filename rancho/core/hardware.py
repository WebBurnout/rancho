"""GPIO adapters: siren MOSFET on GPIO25, mains relay contact on GPIO23.

gpiod is imported only when a line is opened, so offline tests never touch
/dev/gpiochip0. This kernel keeps outputs driven after the process exits
(pinctrl_bcm2835.persist_gpio_outputs=Y): a crash with the siren on would leave
it on, so deploy/rancho.service runs `python -m rancho.siren_off` after every stop.
"""

from typing import Protocol

CHIP = "/dev/gpiochip0"
SIREN_LINE = 25
POWER_LINE = 23


class Siren(Protocol):
    def open(self) -> None:
        """Claim the line, driven OFF."""
        ...

    def set(self, on: bool) -> None: ...

    def close(self) -> None:
        """Switch OFF and release the line."""
        ...


class PowerSensor(Protocol):
    def open(self) -> None: ...

    def power_present(self) -> bool: ...

    def close(self) -> None: ...


class GpioSiren:
    def __init__(self, active_low: bool) -> None:
        self._active_low = active_low
        self._request = None

    def open(self) -> None:
        import gpiod
        from gpiod.line import Direction, Value

        # Requested as an output that starts INACTIVE: it never pulses on at start.
        self._request = gpiod.request_lines(CHIP, consumer="rancho-siren", config={
            SIREN_LINE: gpiod.LineSettings(
                direction=Direction.OUTPUT, active_low=self._active_low, output_value=Value.INACTIVE,
            ),
        })

    def set(self, on: bool) -> None:
        from gpiod.line import Value

        assert self._request is not None
        self._request.set_value(SIREN_LINE, Value.ACTIVE if on else Value.INACTIVE)

    def close(self) -> None:
        request, self._request = self._request, None
        if request is None:
            return
        try:
            from gpiod.line import Value

            request.set_value(SIREN_LINE, Value.INACTIVE)
        finally:
            request.release()


class GpioPowerSensor:
    """Relay contact closed (mains present) pulls the line to 0; open or cut wire reads 1."""

    def __init__(self) -> None:
        self._request = None

    def open(self) -> None:
        import gpiod
        from gpiod.line import Bias, Direction

        # Input only, never an output: the contact shorts the line to GND.
        # The 220 ohm external pull-up replaces the internal bias.
        self._request = gpiod.request_lines(CHIP, consumer="rancho-power", config={
            POWER_LINE: gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.DISABLED),
        })

    def power_present(self) -> bool:
        from gpiod.line import Value

        assert self._request is not None
        return self._request.get_value(POWER_LINE) == Value.INACTIVE

    def close(self) -> None:
        request, self._request = self._request, None
        if request is not None:
            request.release()

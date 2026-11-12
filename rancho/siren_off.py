"""Force the siren OFF; run by systemd after every service stop, including crashes.

The kernel keeps GPIO outputs driven after a process dies, so this is what turns
off a siren left on by a crash or SIGKILL.
"""

import logging
import sys

from rancho.core.config import load_config
from rancho.core.hardware import GpioSiren


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logger = logging.getLogger("rancho.siren_off")
    try:
        active_low = load_config().siren_active_low
    except (OSError, ValueError) as exc:
        # Without a readable .env the service cannot have run; assume the default.
        logger.warning("Configuration error (%s); assuming active-high siren", exc)
        active_low = False
    siren = GpioSiren(active_low)
    siren.open()
    siren.close()
    logger.info("Siren forced off")


if __name__ == "__main__":
    main()

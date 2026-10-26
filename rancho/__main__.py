"""Run with python -m rancho or the rancho console script."""

import asyncio
import logging
import sys

from rancho.app import run
from rancho.core.config import load_config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).error("Configuration error: %s", exc)
        raise SystemExit(1) from None
    asyncio.run(run(config))


if __name__ == "__main__":
    main()

"""Rancho sentinel: runs on the homelab and alerts Telegram when the Pi stops answering ping.

Standalone and stdlib-only (Python 3.10+). It only calls sendMessage and never
getUpdates: the Pi is the bot token's sole polling consumer.
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

logger = logging.getLogger("rancho-sentinel")

RANCHO_HOST = "rancho"  # Tailscale MagicDNS name
# If this also fails, the homelab's own uplink is down and the round is skipped.
REFERENCE_HOST = "1.1.1.1"
CHECK_INTERVAL = 30  # seconds between checks
# ~3 minutes of failures: rides out Starlink/Telcel failover and Tailscale reconnects.
FAIL_THRESHOLD = 6
SEND_ATTEMPTS = 5  # backoff 2+4+8+16s, ~30s in total


def ping(host: str) -> bool:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "3", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode > 1:
        # 1 is "no reply"; anything else (DNS failure, socket permission) is worth seeing.
        logger.warning("ping %s failed: %s", host, result.stderr.strip())
    return result.returncode == 0


def format_duration(seconds: float) -> str:
    minutes = int(seconds / 60 + 0.5)
    if minutes < 1:
        return "menos de 1 min"
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


class TelegramSender:
    def __init__(self, token: str, chat_id: str) -> None:
        # The URL contains the token. Never log it.
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    def __call__(self, text: str) -> bool:
        body = json.dumps({"chat_id": self._chat_id, "text": text}).encode()
        request = urllib.request.Request(
            self._url, data=body, headers={"Content-Type": "application/json"}
        )
        for attempt in range(SEND_ATTEMPTS):
            try:
                with urllib.request.urlopen(request, timeout=15):
                    return True
            except urllib.error.HTTPError as exc:
                # A rejected request (bad token or chat) will not improve by retrying.
                if exc.code != 429 and exc.code < 500:
                    logger.error("Telegram rejected the message (HTTP %s)", exc.code)
                    return False
                logger.warning("Telegram send failed (HTTP %s)", exc.code)
            except (OSError, ValueError) as exc:
                # Log the type only: exception text can contain the URL.
                logger.warning("Telegram send failed (%s)", type(exc).__name__)
            if attempt < SEND_ATTEMPTS - 1:
                time.sleep(2 ** (attempt + 1))
        return False


class Sentinel:
    def __init__(
        self,
        ping: Callable[[str], bool],
        send: Callable[[str], bool],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ping = ping
        self._send = send
        self._clock = clock
        self._failures = 0
        self._first_failure = 0.0  # start of the current failure streak
        self._reported_down = False
        self._outage_start = 0.0
        self._outage_end = 0.0

    def check(self) -> None:
        now = self._clock()
        if self._ping(RANCHO_HOST):
            if self._failures:
                logger.info("Rancho answers again after %d failed pings", self._failures)
            if self._reported_down and not self._outage_end:
                self._outage_end = now
            self._failures = 0
        elif not self._ping(REFERENCE_HOST):
            logger.warning("%s does not answer either; homelab uplink down, skipping", REFERENCE_HOST)
            return
        else:
            if not self._failures:
                self._first_failure = now
            self._failures += 1
            logger.info("Rancho does not answer (%d/%d)", self._failures, FAIL_THRESHOLD)
        self._report(now)

    def _report(self, now: float) -> None:
        # Unsent messages are retried on the next check until they go through.
        down = self._failures >= FAIL_THRESHOLD
        if down and not self._reported_down:
            since = format_duration(now - self._first_failure)
            if self._send(f"⚠️ Rancho sin conexión desde hace {since}"):
                self._reported_down = True
                self._outage_start = self._first_failure
                self._outage_end = 0.0
        elif not down and self._reported_down and self._outage_end:
            duration = format_duration(self._outage_end - self._outage_start)
            if self._send(f"✅ Rancho conectado de nuevo (caído {duration})"):
                self._reported_down = False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        raise SystemExit(1)
    sentinel = Sentinel(ping, TelegramSender(token, chat_id))
    logger.info("Watching %s every %ds", RANCHO_HOST, CHECK_INTERVAL)
    try:
        while True:
            sentinel.check()
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

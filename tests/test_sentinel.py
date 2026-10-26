"""Offline tests: ping, clock, and Telegram sending are faked."""

from unittest import TestCase

from sentinel.rancho_sentinel import (
    CHECK_INTERVAL,
    FAIL_THRESHOLD,
    REFERENCE_HOST,
    RANCHO_HOST,
    Sentinel,
    format_duration,
)


class SentinelTests(TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.up = {RANCHO_HOST: True, REFERENCE_HOST: True}
        self.sent: list[str] = []
        self.send_ok = True
        self.sentinel = Sentinel(lambda host: self.up[host], self.send, lambda: self.now)

    def send(self, text: str) -> bool:
        if self.send_ok:
            self.sent.append(text)
        return self.send_ok

    def checks(self, count: int) -> None:
        for _ in range(count):
            self.sentinel.check()
            self.now += CHECK_INTERVAL

    def test_healthy_start_is_silent(self) -> None:
        self.checks(10)
        self.assertEqual(self.sent, [])

    def test_short_outage_is_silent(self) -> None:
        self.up[RANCHO_HOST] = False
        self.checks(FAIL_THRESHOLD - 1)
        self.up[RANCHO_HOST] = True
        self.checks(1)
        self.assertEqual(self.sent, [])

    def test_down_then_up_alerts_once_each(self) -> None:
        self.up[RANCHO_HOST] = False
        self.checks(FAIL_THRESHOLD + 20)
        self.up[RANCHO_HOST] = True
        self.checks(5)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("sin conexión", self.sent[0])
        self.assertIn("3 min", self.sent[0])
        self.assertIn("conectado de nuevo", self.sent[1])
        self.assertIn("13 min", self.sent[1])

    def test_uplink_down_does_not_count(self) -> None:
        self.up[RANCHO_HOST] = False
        self.up[REFERENCE_HOST] = False
        self.checks(FAIL_THRESHOLD * 3)
        self.assertEqual(self.sent, [])

    def test_failed_send_is_retried_on_next_check(self) -> None:
        self.up[RANCHO_HOST] = False
        self.send_ok = False
        self.checks(FAIL_THRESHOLD + 2)
        self.assertEqual(self.sent, [])
        self.send_ok = True
        self.checks(1)
        self.assertEqual(len(self.sent), 1)
        self.checks(5)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_duration_ends_at_recovery_not_at_send(self) -> None:
        self.up[RANCHO_HOST] = False
        self.checks(FAIL_THRESHOLD)
        self.up[RANCHO_HOST] = True
        self.send_ok = False
        self.checks(20)
        self.send_ok = True
        self.checks(1)
        self.assertIn("3 min", self.sent[-1])

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(10), "menos de 1 min")
        self.assertEqual(format_duration(150), "3 min")
        self.assertEqual(format_duration(3900), "1 h 5 min")

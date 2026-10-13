"""Unit tests for gateway.should_stop, the idle-stop decision function.

gateway.py builds its Docker client at import time (`docker.from_env()`), and constructing
a client pings the Docker daemon for its API version. To keep these tests dependency-free
(no daemon, no /var/run/docker.sock) we stub `docker.from_env` before importing the module;
the real `should_stop` implementation is what gets tested. Run them with:

    docker run --rm -v "$PWD:/app:ro" -w /app -e PYTHONDONTWRITEBYTECODE=1 \
      python:3.12-slim \
      sh -c 'pip install -q "docker>=7,<8" && python -m unittest discover -s tests -v'
"""
import unittest
from unittest import mock

with mock.patch("docker.from_env"):
    # gateway._client becomes a Mock; the tests below only exercise the pure function.
    import gateway

IDLE = gateway.IDLE_STOP_SECONDS


class ShouldStopTests(unittest.TestCase):
    """Spec: stop only when inflight == 0 AND (now - last_activity) > IDLE_STOP_SECONDS."""

    def test_inflight_request_prevents_stop(self) -> None:
        # A request is in flight; no amount of idleness may trigger a stop.
        self.assertFalse(gateway.should_stop(now=IDLE * 100.0, last_activity=0.0, inflight=1))

    def test_exactly_at_threshold_does_not_stop(self) -> None:
        # Strict greater-than: idling for exactly IDLE_STOP_SECONDS is not enough.
        self.assertFalse(gateway.should_stop(now=float(IDLE), last_activity=0.0, inflight=0))

    def test_just_over_threshold_stops(self) -> None:
        self.assertTrue(
            gateway.should_stop(now=float(IDLE) + 0.001, last_activity=0.0, inflight=0)
        )

    def test_far_over_threshold_stops(self) -> None:
        self.assertTrue(gateway.should_stop(now=IDLE * 10.0, last_activity=0.0, inflight=0))

    def test_no_elapsed_since_activity_does_not_stop(self) -> None:
        # now == last_activity (activity just recorded) -> idle is 0 -> no stop.
        self.assertFalse(gateway.should_stop(now=1000.0, last_activity=1000.0, inflight=0))

    def test_just_under_threshold_does_not_stop(self) -> None:
        self.assertFalse(
            gateway.should_stop(now=float(IDLE) - 0.001, last_activity=0.0, inflight=0)
        )


if __name__ == "__main__":
    unittest.main()

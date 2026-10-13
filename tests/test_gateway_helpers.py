"""Hermetic unit tests for gateway.py's Docker/wake helpers, idle monitor and entry point.

Like tests/test_gateway.py, these tests stub `docker.from_env()` before importing the
module, so no Docker daemon or /var/run/docker.sock is needed. All sleeps and clocks are
patched out so nothing waits on real time. Run them with:

    docker run --rm -v "$PWD:/app:ro" -w /app -e PYTHONDONTWRITEBYTECODE=1 \
      python:3.12-slim \
      sh -c 'pip install -q -r requirements-dev.txt && python -m unittest discover -s tests -v'
"""
import runpy
import socket
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

import docker

with mock.patch("docker.from_env"):
    # gateway._client becomes a Mock; each test patches what it exercises.
    import gateway

GATEWAY_PATH = Path(gateway.__file__).resolve()


class BackendRunningTests(unittest.TestCase):
    """backend_running(): container status lookup, with NotFound treated as not running."""

    def test_running_container_is_true(self) -> None:
        client = mock.Mock()
        client.containers.get.return_value = mock.Mock(status="running")
        with mock.patch.object(gateway, "_client", client):
            self.assertTrue(gateway.backend_running())
        client.containers.get.assert_called_once_with(gateway.BACKEND_CONTAINER)

    def test_stopped_container_is_false(self) -> None:
        client = mock.Mock()
        client.containers.get.return_value = mock.Mock(status="exited")
        with mock.patch.object(gateway, "_client", client):
            self.assertFalse(gateway.backend_running())

    def test_missing_container_is_false(self) -> None:
        client = mock.Mock()
        client.containers.get.side_effect = docker.errors.NotFound("no such container")
        with mock.patch.object(gateway, "_client", client):
            self.assertFalse(gateway.backend_running())


class BackendReadyTests(unittest.TestCase):
    """backend_ready(): True only for HTTP 200 on /status; any error -> False."""

    @staticmethod
    def _mock_connection(status: int) -> mock.Mock:
        conn = mock.Mock()
        conn.getresponse.return_value = mock.Mock(status=status)
        return conn

    def test_http_200_is_true(self) -> None:
        conn = self._mock_connection(200)
        with mock.patch.object(
            gateway.http.client, "HTTPConnection", return_value=conn
        ) as conn_cls:
            self.assertTrue(gateway.backend_ready())
        conn_cls.assert_called_once_with(gateway.BACKEND_HOST, gateway.BACKEND_PORT, timeout=3)
        conn.request.assert_called_once_with("GET", "/status")
        conn.getresponse.return_value.read.assert_called_once_with()
        conn.close.assert_called_once_with()

    def test_http_503_is_false(self) -> None:
        conn = self._mock_connection(503)
        with mock.patch.object(gateway.http.client, "HTTPConnection", return_value=conn):
            self.assertFalse(gateway.backend_ready())

    def test_connection_error_is_false(self) -> None:
        conn = self._mock_connection(200)
        conn.getresponse.side_effect = OSError("connection refused")
        with mock.patch.object(gateway.http.client, "HTTPConnection", return_value=conn):
            self.assertFalse(gateway.backend_ready())

    def test_real_connection_refused_is_false(self) -> None:
        # Bind+close an ephemeral loopback port so nothing can answer on it.
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        finally:
            sock.close()
        with mock.patch.object(gateway, "BACKEND_HOST", "127.0.0.1"), \
                mock.patch.object(gateway, "BACKEND_PORT", port):
            self.assertFalse(gateway.backend_ready())


class StartBackendTests(unittest.TestCase):
    """start_backend(): early return, docker start, readiness poll, timeout."""

    def test_already_ready_returns_without_touching_docker(self) -> None:
        with mock.patch.object(gateway, "backend_ready", return_value=True), \
                mock.patch.object(gateway, "backend_running") as running, \
                mock.patch.object(gateway, "_container") as container:
            gateway.start_backend()
        running.assert_not_called()
        container.assert_not_called()

    def test_starts_container_then_polls_until_ready(self) -> None:
        container = mock.Mock()
        log = mock.Mock()
        with mock.patch.object(gateway, "_container", return_value=container), \
                mock.patch.object(gateway, "backend_running", return_value=False), \
                mock.patch.object(gateway, "backend_ready", side_effect=[False, False, True]), \
                mock.patch.object(gateway, "log", log), \
                mock.patch.object(gateway.time, "sleep") as sleep:
            gateway.start_backend()
        container.start.assert_called_once_with()
        sleep.assert_called_once_with(1)
        log.assert_any_call(f"waking {gateway.BACKEND_CONTAINER}")
        log.assert_any_call(f"{gateway.BACKEND_CONTAINER} is ready")

    def test_running_but_not_ready_does_not_start_container(self) -> None:
        container = mock.Mock()
        with mock.patch.object(gateway, "_container", return_value=container), \
                mock.patch.object(gateway, "backend_running", return_value=True), \
                mock.patch.object(gateway, "backend_ready", side_effect=[False, True]), \
                mock.patch.object(gateway.time, "sleep") as sleep:
            gateway.start_backend()
        container.start.assert_not_called()
        sleep.assert_not_called()

    def test_timeout_raises_runtime_error(self) -> None:
        container = mock.Mock()
        with mock.patch.object(gateway, "_container", return_value=container), \
                mock.patch.object(gateway, "backend_running", return_value=False), \
                mock.patch.object(gateway, "backend_ready", return_value=False), \
                mock.patch.object(gateway, "WAKE_TIMEOUT_SECONDS", 0), \
                mock.patch.object(gateway.time, "time", return_value=1000.0), \
                mock.patch.object(gateway.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError) as ctx:
                gateway.start_backend()
        self.assertIn("did not become ready", str(ctx.exception))
        container.start.assert_called_once_with()
        sleep.assert_not_called()


class StopBackendTests(unittest.TestCase):
    """stop_backend(): stops only running containers, always resets _last_activity."""

    def setUp(self) -> None:
        self.addCleanup(setattr, gateway, "_last_activity", gateway._last_activity)
        gateway._last_activity = 123.0  # ancient timestamp; must be refreshed

    def test_running_container_is_stopped_and_activity_reset(self) -> None:
        container = mock.Mock()
        log = mock.Mock()
        with mock.patch.object(gateway, "_container", return_value=container), \
                mock.patch.object(gateway, "backend_running", return_value=True), \
                mock.patch.object(gateway, "log", log):
            gateway.stop_backend()
        container.stop.assert_called_once_with(timeout=30)
        self.assertGreater(gateway._last_activity, 123.0)
        log.assert_called_once_with(f"stopping {gateway.BACKEND_CONTAINER} (idle)")

    def test_not_running_skips_stop_but_resets_activity(self) -> None:
        container = mock.Mock()
        with mock.patch.object(gateway, "_container", return_value=container), \
                mock.patch.object(gateway, "backend_running", return_value=False):
            gateway.stop_backend()
        container.stop.assert_not_called()
        self.assertGreater(gateway._last_activity, 123.0)


class _BreakLoop(Exception):
    """Sentinel raised from the patched sleep to end _idle_monitor's infinite loop."""


class IdleMonitorTests(unittest.TestCase):
    """_idle_monitor(): one deterministic iteration per test, loop broken via sentinel."""

    def setUp(self) -> None:
        self.addCleanup(setattr, gateway, "_inflight", gateway._inflight)
        self.addCleanup(setattr, gateway, "_last_activity", gateway._last_activity)
        gateway._inflight = 0
        gateway._last_activity = 0.0  # ancient -> should_stop() True under patched clock

    def _run_one_iteration(self, *, running: bool, stop_backend: mock.Mock) -> mock.Mock:
        """Run _idle_monitor until the patched sleep raises _BreakLoop on its 2nd call."""
        with mock.patch.object(gateway, "backend_running", return_value=running), \
                mock.patch.object(gateway, "stop_backend", stop_backend), \
                mock.patch.object(gateway, "log") as log, \
                mock.patch.object(gateway.time, "sleep",
                                  side_effect=[None, _BreakLoop("stop the loop")]), \
                mock.patch.object(gateway.time, "time", return_value=1_000_000_000.0):
            with self.assertRaises(_BreakLoop):
                gateway._idle_monitor()
        return log

    def test_stop_backend_called_when_idle_and_running(self) -> None:
        stop = mock.Mock()
        log = self._run_one_iteration(running=True, stop_backend=stop)
        stop.assert_called_once_with()
        log.assert_not_called()

    def test_no_stop_while_requests_inflight(self) -> None:
        gateway._inflight = 1
        stop = mock.Mock()
        self._run_one_iteration(running=True, stop_backend=stop)
        stop.assert_not_called()

    def test_no_stop_when_backend_not_running(self) -> None:
        stop = mock.Mock()
        self._run_one_iteration(running=False, stop_backend=stop)
        stop.assert_not_called()

    def test_stop_failure_is_logged_and_loop_continues(self) -> None:
        stop = mock.Mock(side_effect=RuntimeError("boom"))
        log = self._run_one_iteration(running=True, stop_backend=stop)
        # The RuntimeError was swallowed (else it would have escaped instead of
        # _BreakLoop), and the loop survived long enough to hit the 2nd sleep.
        stop.assert_called_once_with()
        log.assert_called_once_with("idle stop failed: boom")


class MainTests(unittest.TestCase):
    """main(): starts the daemon idle monitor and serves forever."""

    def test_main_starts_daemon_monitor_and_serves_forever(self) -> None:
        with mock.patch.object(gateway.threading, "Thread") as thread_cls, \
                mock.patch.object(gateway, "ThreadingHTTPServer") as server_cls, \
                mock.patch.object(gateway, "log"):
            gateway.main()
        thread_cls.assert_called_once_with(target=gateway._idle_monitor, daemon=True)
        thread_cls.return_value.start.assert_called_once_with()
        server_cls.assert_called_once_with(("0.0.0.0", gateway.LISTEN_PORT), gateway.Handler)
        server_cls.return_value.serve_forever.assert_called_once_with()


class MainGuardTests(unittest.TestCase):
    """Cover the `if __name__ == "__main__": main()` guard without starting anything.

    runpy re-executes gateway.py as a fresh script with every side effect patched:
    docker.from_env, threading.Thread and http.server.ThreadingHTTPServer are all stubs,
    so nothing is created and no port is bound.
    """

    def test_runpy_executes_main_guard_with_everything_patched(self) -> None:
        with mock.patch("docker.from_env") as docker_from_env, \
                mock.patch("threading.Thread") as thread_cls, \
                mock.patch("http.server.ThreadingHTTPServer") as server_cls:
            runpy.run_path(str(GATEWAY_PATH), run_name="__main__")
        docker_from_env.assert_called_once_with()
        thread_cls.assert_called_once()
        self.assertEqual(thread_cls.call_args.kwargs.get("target").__name__, "_idle_monitor")
        self.assertIs(thread_cls.call_args.kwargs.get("daemon"), True)
        thread_cls.return_value.start.assert_called_once_with()
        server_cls.assert_called_once()
        address, handler = server_cls.call_args.args
        self.assertEqual(address, ("0.0.0.0", gateway.LISTEN_PORT))
        self.assertTrue(issubclass(handler, BaseHTTPRequestHandler))
        server_cls.return_value.serve_forever.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

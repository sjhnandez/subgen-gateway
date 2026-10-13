"""Integration-style tests for gateway.Handler: the status endpoint and the proxy path.

Each test starts a real gateway.ThreadingHTTPServer on an ephemeral 127.0.0.1 port and
talks to it with http.client. For proxying, a tiny in-process fake backend is started the
same way and gateway.BACKEND_HOST/BACKEND_PORT are patched to point at it; Docker is
stubbed, so nothing here needs a Docker daemon or the network. All servers are shut down
and joined in cleanup.
"""
import http.client
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

with mock.patch("docker.from_env"):
    # gateway._client becomes a Mock; no Docker daemon needed at import time.
    import gateway

# 307_200 bytes = 300 KiB, just over gateway.CHUNK (256 KiB) -> exercises the chunk loop.
LARGE_BODY = bytes(range(256)) * 1200


class _FakeBackendHandler(BaseHTTPRequestHandler):
    """Records every request on the server object and answers with its configured response."""

    protocol_version = "HTTP/1.1"

    def _respond(self) -> None:
        remaining = int(self.headers.get("Content-Length") or 0)
        body = b""
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            body += chunk
            remaining -= len(chunk)
        self.server.record(
            method=self.command,
            path=self.path,
            headers=list(self.headers.items()),
            body=body,
        )
        self.send_response(self.server.response_status)
        for name, value in self.server.response_headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(self.server.response_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(self.server.response_body)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _respond

    def log_message(self, fmt, *args):  # keep test output clean
        pass


class FakeBackend(ThreadingHTTPServer):
    """ThreadingHTTPServer that records requests and serves a configurable response."""

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []
        self.response_status = 200
        self.response_body = b"backend-ok"
        self.response_headers = []

    def record(self, *, method, path, headers, body) -> None:
        self.requests.append(
            {"method": method, "path": path, "headers": headers, "body": body}
        )


def _closed_loopback_port() -> int:
    """An ephemeral loopback port that is guaranteed to be unbound right now."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class ServerTestCase(unittest.TestCase):
    """Starts ThreadingHTTPServers on ephemeral 127.0.0.1 ports; tears them down cleanly."""

    def setUp(self) -> None:
        super().setUp()
        self._servers = []
        self.addCleanup(self._stop_servers)
        # Fresh gateway accounting state for every test.
        self.addCleanup(setattr, gateway, "_inflight", gateway._inflight)
        self.addCleanup(setattr, gateway, "_last_activity", gateway._last_activity)
        gateway._inflight = 0
        gateway._last_activity = time.time()

    def start_server(self, server: ThreadingHTTPServer) -> int:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append((server, thread))
        return server.server_address[1]

    def _stop_servers(self) -> None:
        for server, thread in reversed(self._servers):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "server thread did not stop")

    def start_gateway(self) -> None:
        self.gateway_port = self.start_server(
            gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
        )

    def start_fake_backend(self) -> None:
        self.backend = FakeBackend(("127.0.0.1", 0), _FakeBackendHandler)
        self.backend_port = self.start_server(self.backend)

    def gw_request(self, method, path, body=None, headers=None):
        """Send one request to the gateway; always closes the client connection."""
        conn = http.client.HTTPConnection("127.0.0.1", self.gateway_port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, list(resp.getheaders()), data
        finally:
            conn.close()

    def wait_for_gateway_idle(self, timeout: float = 0.1) -> None:
        """Let the handler thread's finally block settle its accounting.

        The response reaches the client just before the server-side `finally` runs, so
        _inflight/_last_activity may not be updated yet when gw_request returns. Poll with
        1 ms granularity; in practice the first check already passes.
        """
        deadline = time.monotonic() + timeout
        while gateway._inflight != 0 and time.monotonic() < deadline:
            time.sleep(0.001)


class GatewayStatusTests(ServerTestCase):
    """GET /gateway/status: JSON state, never wakes the backend."""

    def setUp(self) -> None:
        super().setUp()
        self.start_gateway()
        for target, attr in (
            ("backend_running", "backend_running"),
            ("backend_ready", "backend_ready"),
            ("start_backend", "start_backend"),
        ):
            patch = mock.patch.object(gateway, target)
            setattr(self, attr, patch.start())
            self.addCleanup(patch.stop)
        self.backend_running.return_value = False

    def test_status_reports_state_without_waking(self) -> None:
        gateway._last_activity = time.time() - 2.0
        status, headers, body = self.gw_request("GET", "/gateway/status")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["backend_container"], gateway.BACKEND_CONTAINER)
        self.assertIs(payload["backend_running"], False)
        self.assertIs(payload["backend_ready"], False)
        self.assertEqual(payload["inflight"], 0)
        self.assertEqual(payload["idle_stop_seconds"], gateway.IDLE_STOP_SECONDS)
        self.assertGreaterEqual(payload["idle_seconds"], 0.5)
        self.assertLessEqual(payload["idle_seconds"], 10.0)
        received = {k.lower(): v for k, v in headers}
        self.assertEqual(received["content-type"], "application/json")
        self.assertEqual([v for k, v in headers if k.lower() == "content-length"],
                         [str(len(body))])
        # Not running -> backend_ready is never probed, and status never wakes.
        self.backend_ready.assert_not_called()
        self.start_backend.assert_not_called()
        self.assertEqual(gateway._inflight, 0)

    def test_status_with_query_string_is_still_status(self) -> None:
        status, _headers, body = self.gw_request("GET", "/gateway/status?probe=1")
        self.assertEqual(status, 200)
        self.assertIn("backend_container", json.loads(body))
        self.start_backend.assert_not_called()

    def test_status_probes_backend_only_when_running(self) -> None:
        self.backend_running.return_value = True
        self.backend_ready.return_value = True
        status, _headers, body = self.gw_request("GET", "/gateway/status")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIs(payload["backend_running"], True)
        self.assertIs(payload["backend_ready"], True)
        self.backend_ready.assert_called()  # probed because running
        self.start_backend.assert_not_called()


class ProxyTests(ServerTestCase):
    """Everything else: wake, stream the request, relay the response."""

    def setUp(self) -> None:
        super().setUp()
        self.start_gateway()
        self.start_fake_backend()
        for target, value in (
            ("BACKEND_HOST", "127.0.0.1"),
            ("BACKEND_PORT", self.backend_port),
        ):
            patch = mock.patch.object(gateway, target, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(gateway, "start_backend")
        self.start_backend = patch.start()
        self.addCleanup(patch.stop)

    def test_get_with_query_string_proxied_verbatim(self) -> None:
        self.backend.response_status = 201
        self.backend.response_body = b"transcription done"
        self.backend.response_headers = [
            ("Content-Type", "text/plain"),
            ("X-Backend-Marker", "from-fake-backend"),
            ("Connection", "keep-alive"),
            ("Keep-Alive", "timeout=5"),
        ]
        status, headers, body = self.gw_request(
            "GET", "/asr?model=small&lang=en",
            headers={"X-Client": "bazarr", "Connection": "close"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body, b"transcription done")
        received = {k.lower(): v for k, v in headers}
        self.assertEqual(received["x-backend-marker"], "from-fake-backend")
        self.assertEqual(received["content-type"], "text/plain")
        self.assertNotIn("keep-alive", received)  # hop-by-hop header dropped
        self.assertNotEqual(received.get("connection"), "keep-alive")
        self.assertNotIn("transfer-encoding", received)
        # Exact Content-Length, exactly once.
        self.assertEqual([v for k, v in headers if k.lower() == "content-length"],
                         [str(len(body))])

        self.start_backend.assert_called_once_with()
        self.assertEqual(len(self.backend.requests), 1)
        req = self.backend.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], "/asr?model=small&lang=en")  # verbatim, incl. query
        sent = {k.lower(): v for k, v in req["headers"]}
        self.assertEqual(sent["host"], f"127.0.0.1:{self.backend_port}")
        self.assertEqual(sent["x-client"], "bazarr")
        self.assertNotIn("connection", sent)  # client's Connection: close stripped
        self.wait_for_gateway_idle()
        self.assertEqual(gateway._inflight, 0)
        self.assertGreater(gateway._last_activity, 0.0)

    def test_large_post_body_arrives_byte_identical(self) -> None:
        self.backend.response_body = b"ok"
        status, headers, body = self.gw_request(
            "POST", "/asr", body=LARGE_BODY,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")
        self.assertEqual([v for k, v in headers if k.lower() == "content-length"],
                         [str(len(body))])
        self.assertEqual(len(self.backend.requests), 1)
        req = self.backend.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/asr")
        self.assertEqual(len(req["body"]), len(LARGE_BODY))
        self.assertEqual(req["body"], LARGE_BODY)  # byte-identical through the chunk loop
        # Content-Length re-added exactly once; nothing duplicated.
        self.assertEqual([v for k, v in req["headers"] if k.lower() == "content-length"],
                         [str(len(LARGE_BODY))])
        self.wait_for_gateway_idle()
        self.assertEqual(gateway._inflight, 0)

    def test_head_relays_headers_without_body(self) -> None:
        self.backend.response_headers = [("X-Backend-Marker", "head-ok")]
        self.backend.response_body = b"never sent for HEAD"
        status, headers, body = self.gw_request("HEAD", "/asr")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        received = {k.lower(): v for k, v in headers}
        self.assertEqual(received["x-backend-marker"], "head-ok")
        self.assertEqual([v for k, v in headers if k.lower() == "content-length"], ["0"])
        self.assertEqual(self.backend.requests[0]["method"], "HEAD")

    def test_wake_failure_returns_503_without_contacting_backend(self) -> None:
        self.start_backend.side_effect = RuntimeError("boom")
        status, headers, body = self.gw_request("POST", "/asr", body=b"pcm")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "subgen wake failed: boom")
        self.assertEqual({k.lower(): v for k, v in headers}["content-type"],
                         "application/json")
        self.assertEqual(self.backend.requests, [])  # fake backend never contacted
        self.wait_for_gateway_idle()
        self.assertEqual(gateway._inflight, 0)  # decremented in finally

    def test_unreachable_backend_returns_502(self) -> None:
        closed_port = _closed_loopback_port()
        with mock.patch.object(gateway, "BACKEND_PORT", closed_port), \
                mock.patch.object(gateway, "log") as log:
            status, _headers, _body = self.gw_request("GET", "/asr")
        self.assertEqual(status, 502)
        self.assertEqual(log.call_count, 1)
        self.assertEqual(log.call_args.args[0][:16], "proxy error for ")
        self.wait_for_gateway_idle()
        self.assertEqual(gateway._inflight, 0)  # decremented in finally


if __name__ == "__main__":
    unittest.main()

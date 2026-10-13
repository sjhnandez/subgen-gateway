"""Wake-on-demand gateway in front of Subgen.

Why: Subgen (faster-whisper) held ~1.4 GiB of RAM even when completely idle, and this host
runs with almost no free memory. Bazarr still needs to reach it automatically, so this
gateway:

  * accepts the request, starts the `subgen` container if it isn't running, waits for its
    /status to answer, then streams the request through;
  * stops `subgen` again after IDLE_STOP_SECONDS with no requests.

Bazarr's whisper endpoint points here (http://subgen-gateway:9000) instead of
http://subgen:9000. Request bodies can be hundreds of MB (raw PCM uploads) so both directions
are streamed; response bodies here are small (SRT/JSON), so they are buffered to set an exact
Content-Length.

Ops endpoint: GET /gateway/status -> JSON state (does NOT wake the backend).
"""
from __future__ import annotations

import http.client
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import docker

BACKEND_CONTAINER = os.environ.get("BACKEND_CONTAINER", "subgen")
BACKEND_HOST = os.environ.get("BACKEND_HOST", "subgen")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "9000"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9000"))
IDLE_STOP_SECONDS = int(os.environ.get("IDLE_STOP_SECONDS", "900"))
CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "30"))
WAKE_TIMEOUT_SECONDS = int(os.environ.get("WAKE_TIMEOUT_SECONDS", "300"))
READ_TIMEOUT_SECONDS = int(os.environ.get("READ_TIMEOUT_SECONDS", "7200"))
CHUNK = 256 * 1024

_client = docker.from_env()
_lock = threading.Lock()          # guards _inflight/_last_activity accounting
_wake_lock = threading.Lock()     # serializes wake/stop transitions
_inflight = 0
_last_activity = time.time()


def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def _container():
    return _client.containers.get(BACKEND_CONTAINER)


def backend_running() -> bool:
    try:
        return _container().status == "running"
    except docker.errors.NotFound:
        return False


def backend_ready() -> bool:
    try:
        conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=3)
        conn.request("GET", "/status")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status == 200
    except Exception:
        return False


def start_backend() -> None:
    """Start the container (if needed) and wait until /status answers."""
    with _wake_lock:
        if backend_ready():
            return
        if not backend_running():
            log(f"waking {BACKEND_CONTAINER}")
            _container().start()
        deadline = time.time() + WAKE_TIMEOUT_SECONDS
        while time.time() < deadline:
            if backend_ready():
                log(f"{BACKEND_CONTAINER} is ready")
                return
            time.sleep(1)
        raise RuntimeError(f"{BACKEND_CONTAINER} did not become ready within {WAKE_TIMEOUT_SECONDS}s")


def stop_backend() -> None:
    global _last_activity
    with _wake_lock:
        if backend_running():
            log(f"stopping {BACKEND_CONTAINER} (idle)")
            _container().stop(timeout=30)
        _last_activity = time.time()


def should_stop(now: float, last_activity: float, inflight: int) -> bool:
    """Pure decision function (unit-testable): only when nothing is in flight and the backend
    has been idle past IDLE_STOP_SECONDS."""
    return inflight == 0 and (now - last_activity) > IDLE_STOP_SECONDS


def _idle_monitor() -> None:
    while True:
        time.sleep(CHECK_INTERVAL_SECONDS)
        with _lock:
            inflight, last = _inflight, _last_activity
        if should_stop(time.time(), last, inflight) and backend_running():
            try:
                stop_backend()
            except Exception as e:
                log(f"idle stop failed: {e}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "subgen-gateway"

    def _gateway_status(self) -> None:
        with _lock:
            inflight, last = _inflight, _last_activity
        body = json.dumps({
            "backend_container": BACKEND_CONTAINER,
            "backend_running": backend_running(),
            "backend_ready": backend_ready() if backend_running() else False,
            "inflight": inflight,
            "idle_seconds": round(time.time() - last, 1),
            "idle_stop_seconds": IDLE_STOP_SECONDS,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        global _inflight, _last_activity
        if self.path.split("?")[0] == "/gateway/status":
            self._gateway_status()
            return
        with _lock:
            _inflight += 1
        try:
            try:
                start_backend()
            except Exception as e:
                body = json.dumps({"error": f"subgen wake failed: {e}"}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            length = int(self.headers.get("Content-Length") or 0)
            fwd_headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in ("host", "connection", "content-length")}
            conn = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=READ_TIMEOUT_SECONDS)
            conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", f"{BACKEND_HOST}:{BACKEND_PORT}")
            for k, v in fwd_headers.items():
                conn.putheader(k, v)
            if length:
                conn.putheader("Content-Length", str(length))
            conn.endheaders()
            sent = 0
            while sent < length:
                chunk = self.rfile.read(min(CHUNK, length - sent))
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()

            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("connection", "content-length", "transfer-encoding", "keep-alive"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except Exception as e:
            log(f"proxy error for {self.command} {self.path}: {e!r}")
            try:
                self.send_error(502, "subgen-gateway proxy error")
            except Exception:
                pass
        finally:
            with _lock:
                _inflight -= 1
                _last_activity = time.time()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

    def log_message(self, fmt, *args):  # keep stdout for our own lines only
        pass


def main() -> None:
    log(f"subgen-gateway on :{LISTEN_PORT} -> {BACKEND_HOST}:{BACKEND_PORT} "
        f"(idle stop after {IDLE_STOP_SECONDS}s, check every {CHECK_INTERVAL_SECONDS}s)")
    threading.Thread(target=_idle_monitor, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

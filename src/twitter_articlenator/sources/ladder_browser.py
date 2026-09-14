"""Isolated Botasaurus fallback with public-only, DNS-pinned browser egress."""

import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import structlog

from . import ladder_http

log = structlog.get_logger()
BROWSER_TIMEOUT = 35


class PublicProxyHandler(BaseHTTPRequestHandler):
    """Validate each tunnel/subresource destination before opening its socket."""

    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        self._forward("https://" + self.path, tunnel=True)

    def do_GET(self):
        self._forward(self.path, tunnel=False)

    def do_POST(self):
        self._forward(self.path, tunnel=False)

    def _forward(self, url, *, tunnel):
        upstream = None
        try:
            _, host, port, address = ladder_http.validate_url(url)
            upstream = socket.create_connection((address, port), timeout=8)
            if tunnel:
                self.send_response(200, "Connection established")
                self.end_headers()
            else:
                parsed = urlsplit(url)
                path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in {"proxy-connection", "proxy-authorization", "host"}
                }
                headers["Host"] = host if port == 80 else f"{host}:{port}"
                headers["Connection"] = "close"
                start = f"{self.command} {path} HTTP/1.1\r\n"
                start += "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
                upstream.sendall(start.encode("latin-1"))
                length = int(self.headers.get("Content-Length", "0"))
                if length > 1024 * 1024:
                    raise ValueError("Request too large")
                if length:
                    upstream.sendall(self.rfile.read(length))
            # Connections are bounded independently of the browser worker.
            end = time.monotonic() + BROWSER_TIMEOUT
            transferred = 0
            while time.monotonic() < end and not self.server.stopping.is_set():
                ready, _, _ = select.select([self.connection, upstream], [], [], 0.25)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    transferred += len(data)
                    if transferred > 20 * 1024 * 1024:
                        return
                    target = upstream if source is self.connection else self.connection
                    target.sendall(data)
        except (ValueError, OSError):
            if upstream is None:
                self.send_error(403, "Only public websites can be opened")
        finally:
            if upstream:
                upstream.close()
            self.close_connection = True


@contextmanager
def public_proxy():
    server = ThreadingHTTPServer(("127.0.0.1", 0), PublicProxyHandler)
    server.stopping = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def fetch_via_browser(url, job_id=None):
    """Return rendered HTML, or let archive retrieval continue on browser failure."""
    remaining = (ladder_http.DEADLINE.get() or time.monotonic() + 60) - time.monotonic()
    timeout = min(BROWSER_TIMEOUT, remaining - 5)
    if timeout < 3:
        return None, None
    process = None
    try:
        ladder_http.validate_url(url)
        with tempfile.TemporaryDirectory(prefix="articlenator-browser-") as directory:
            result_path = Path(directory) / "result.json"
            with public_proxy() as proxy:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "twitter_articlenator.sources.ladder_browser_worker",
                        url,
                        str(result_path),
                        proxy,
                    ],
                    # Nix's installed entry point extends sys.path in-process.
                    # Preserve that environment for the disposable interpreter.
                    env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
                    cwd=directory,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    process.wait(timeout=timeout)
                finally:
                    # Kill the dedicated process group, including any browser or
                    # Xvfb child left behind by an interrupted driver startup.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                if process.returncode or not result_path.is_file():
                    return None, None
                if result_path.stat().st_size > ladder_http.MAX_BYTES:
                    return None, None
                result = json.loads(result_path.read_text())
                ladder_http.validate_url(result["url"])
                return result["html"], result["url"]
    except Exception as exc:
        log.info("ladder_browser_unavailable", reason=type(exc).__name__)
        return None, None

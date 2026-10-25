"""http.server adapter for App. Loopback-only, threaded, bounded bodies."""

from __future__ import annotations

import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .. import logs
from .app import MAX_BODY, App, Request

log = logs.get("http")


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CouponHunter"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def _serve(self) -> None:
            start = time.perf_counter()
            parts = urlsplit(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self.send_error(413 if length > MAX_BODY else 400)
                return
            body = self.rfile.read(length) if length else b""
            req = Request(self.command, parts.path, parse_qs(parts.query, max_num_fields=50),
                          {k.lower(): v for k, v in self.headers.items()}, body)
            resp = app.handle(req)
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp.body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp.body)
            logs.event(log, "request", method=self.command, path=parts.path, status=resp.status,
                       ms=round((time.perf_counter() - start) * 1000, 1))

        do_GET = do_POST = do_DELETE = do_HEAD = _serve

        def log_message(self, format: str, *args) -> None:  # replaced by structured logging above
            pass

    return Handler


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd

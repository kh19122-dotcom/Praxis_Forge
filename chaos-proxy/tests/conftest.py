from __future__ import annotations

from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest

from chaos_proxy.controller import FaultController
from chaos_proxy.proxy import bind_servers
from chaos_proxy.settings import Settings


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def do_PUT(self) -> None:
        self._respond()

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "idempotency_key": self.headers.get("Idempotency-Key"),
            }
        )
        entered = getattr(self.server, "entered", None)
        if entered is not None:
            entered.set()
        hold = getattr(self.server, "hold", None)
        if hold is not None:
            hold.wait()
        if self.path.startswith("/v1/bookings") and self.command == "POST":
            payload = b'{"id":"bkg_1","status":"confirmed"}'
            status = 201
        elif self.path.startswith("/v1/tasks") and self.command == "POST":
            payload = b'{"id":"tsk_1","status":"open"}'
            status = 201
        elif self.path == "/healthz":
            payload = b'{"status":"ok","service":"upstream"}'
            status = 200
        else:
            payload = b'{"ok":true}'
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True


@pytest.fixture
def upstream_server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    server.requests = []  # type: ignore[attr-defined]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def chaos(upstream_server: ThreadingHTTPServer) -> Iterator[dict[str, object]]:
    host, port = upstream_server.server_address[:2]
    settings = Settings(
        upstream_url=f"http://{host}:{port}",
        proxy_host="127.0.0.1",
        proxy_port=0,
        admin_host="127.0.0.1",
        admin_port=0,
        service_name="chaos-test",
    )
    controller = FaultController()
    proxy, admin, client = bind_servers(settings, controller)
    proxy_thread = Thread(target=proxy.serve_forever, daemon=True)
    admin_thread = Thread(target=admin.serve_forever, daemon=True)
    proxy_thread.start()
    admin_thread.start()
    proxy_host, proxy_port = proxy.server_address[:2]
    admin_host, admin_port = admin.server_address[:2]
    try:
        yield {
            "proxy_url": f"http://{proxy_host}:{proxy_port}",
            "admin_url": f"http://{admin_host}:{admin_port}",
            "controller": controller,
            "upstream": upstream_server,
        }
    finally:
        proxy.shutdown()
        admin.shutdown()
        proxy.server_close()
        admin.server_close()
        client.close()


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=2.0) as client:
        yield client

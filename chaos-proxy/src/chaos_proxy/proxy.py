from __future__ import annotations

import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from chaos_proxy.controller import FaultController
from chaos_proxy.httputil import (
    header_map,
    idempotency_key,
    read_body,
    request_path,
    request_target,
    send_json,
    send_raw,
)
from chaos_proxy.settings import Settings

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


class _ChaosServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    owns_upstream = False

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        controller: FaultController,
        settings: Settings,
        upstream: httpx.Client | None = None,
    ) -> None:
        self.controller = controller
        self.settings = settings
        self.upstream = upstream
        super().__init__(server_address, request_handler_class)


class _BaseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        return

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
        ):
            self.close_connection = True
        except OSError:
            self.close_connection = True


def _install_methods(handler_cls: type[_BaseHandler], impl_name: str) -> None:
    impl = getattr(handler_cls, impl_name)

    def _make(name: str):
        def _do(self: _BaseHandler) -> None:
            impl(self)

        _do.__name__ = name
        return _do

    for method in METHODS:
        setattr(handler_cls, f"do_{method}", _make(f"do_{method}"))


class ProxyHandler(_BaseHandler):
    server: _ChaosServer

    def handle_proxy(self) -> None:
        controller = self.server.controller
        path = request_path(self)
        method = self.command.upper()
        key = idempotency_key(self)
        body = read_body(self)
        headers = header_map(self)
        target = request_target(self)
        epoch = controller.begin()
        controller.record(
            "request_received",
            epoch=epoch,
            method=method,
            path=path,
            idempotency_key=key,
        )
        fault = None
        if not (method == "GET" and path == "/healthz"):
            fault = controller.consume(method, path, key, epoch=epoch)
        if fault and fault.mode == "drop_before_upstream":
            controller.record(
                "dropped_before_upstream",
                epoch=epoch,
                method=method,
                path=path,
                idempotency_key=key,
            )
            _drop_connection(self)
            return
        try:
            upstream = self.server.upstream
            if upstream is None:
                raise RuntimeError("upstream client is not configured")
            response = upstream.request(method, target, content=body, headers=headers)
        except httpx.HTTPError as exc:
            controller.record(
                "upstream_error",
                epoch=epoch,
                method=method,
                path=path,
                error=f"{type(exc).__name__}: {exc}",
            )
            send_json(
                self,
                502,
                {
                    "error": "upstream_unreachable",
                    "message": str(exc),
                    "service": self.server.settings.service_name,
                },
            )
            return
        controller.record(
            "upstream_completed",
            epoch=epoch,
            method=method,
            path=path,
            idempotency_key=key,
            upstream_status=response.status_code,
        )
        if fault and fault.mode == "drop_after_upstream":
            controller.record(
                "dropped_after_upstream",
                epoch=epoch,
                method=method,
                path=path,
                idempotency_key=key,
                upstream_status=response.status_code,
            )
            _drop_connection(self)
            return
        if fault and fault.mode == "delay":
            delay_ms = fault.delay_ms
            if delay_ms:
                time.sleep(delay_ms / 1000)
            controller.record(
                "response_delayed",
                epoch=epoch,
                method=method,
                path=path,
                delay_ms=delay_ms,
            )
        send_raw(self, response.status_code, list(response.headers.items()), response.content)


class AdminHandler(_BaseHandler):
    server: _ChaosServer

    def handle_admin(self) -> None:
        path = request_path(self)
        method = self.command.upper()
        controller = self.server.controller
        settings = self.server.settings
        if path == "/healthz" and method == "GET":
            send_json(
                self,
                200,
                {
                    "status": "ok",
                    "service": settings.service_name,
                    "upstream": settings.upstream_url,
                },
            )
            return
        if path == "/v1/admin/reset" and method == "POST":
            controller.reset()
            send_json(self, 200, {"status": "reset", "service": settings.service_name})
            return
        if path == "/v1/admin/faults" and method == "GET":
            send_json(self, 200, controller.snapshot().to_dict())
            return
        if path == "/v1/admin/faults" and method == "PUT":
            payload = _read_json(self)
            if payload is None:
                return
            try:
                fault = controller.configure(payload)
            except (TypeError, ValueError) as exc:
                send_json(self, 400, {"error": "invalid_fault", "message": str(exc)})
                return
            send_json(self, 200, fault.to_dict())
            return
        if path == "/v1/admin/events" and method == "GET":
            send_json(self, 200, {"service": settings.service_name, "events": controller.events()})
            return
        send_json(self, 404, {"error": "not_found", "message": "Unknown admin path."})


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    raw = read_body(handler)
    if not raw:
        send_json(handler, 400, {"error": "invalid_json", "message": "Request body is required."})
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        send_json(handler, 400, {"error": "invalid_json", "message": str(exc)})
        return None
    if not isinstance(payload, dict):
        send_json(handler, 400, {"error": "invalid_json", "message": "JSON object required."})
        return None
    return payload


def _drop_connection(handler: BaseHTTPRequestHandler) -> None:
    handler.close_connection = True
    conn = handler.connection
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


_install_methods(ProxyHandler, "handle_proxy")
_install_methods(AdminHandler, "handle_admin")


def bind_servers(
    settings: Settings,
    controller: FaultController | None = None,
    *,
    upstream: httpx.Client | None = None,
) -> tuple[_ChaosServer, _ChaosServer, httpx.Client]:
    owns_upstream = upstream is None
    client = upstream or httpx.Client(
        base_url=settings.upstream_url,
        timeout=settings.upstream_timeout_seconds,
        follow_redirects=False,
        headers={"Accept-Encoding": "identity"},
    )
    ctrl = controller or FaultController()
    proxy = _ChaosServer(
        (settings.proxy_host, settings.proxy_port),
        ProxyHandler,
        controller=ctrl,
        settings=settings,
        upstream=client,
    )
    admin = _ChaosServer(
        (settings.admin_host, settings.admin_port),
        AdminHandler,
        controller=ctrl,
        settings=settings,
        upstream=None,
    )
    proxy.owns_upstream = owns_upstream
    return proxy, admin, client

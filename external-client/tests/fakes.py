from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


class FakeVendorState:
    def __init__(self) -> None:
        self.slots = [
            {"id": "slot-alpha", "start": "2030-01-06T09:00:00Z"},
            {"id": "slot-beta", "start": "2030-01-06T10:00:00Z"},
        ]
        self.bookings: dict[str, dict[str, Any]] = {}
        self.booking_keys: dict[str, str] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.task_keys: dict[str, str] = {}
        self.drop_next_booking = False
        self.lock = threading.Lock()

    def reset(self) -> None:
        with self.lock:
            self.slots = [
            {"id": "slot-alpha", "start": "2030-01-06T09:00:00Z"},
            {"id": "slot-beta", "start": "2030-01-06T10:00:00Z"},
        ]
            self.bookings = {}
            self.booking_keys = {}
            self.tasks = {}
            self.task_keys = {}
            self.drop_next_booking = False


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length) if length else b"{}"
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("object required")
    return payload


def _drop(handler: BaseHTTPRequestHandler) -> None:
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


class FakeBookingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FakeVendorState

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            _send_json(self, 200, {"status": "ok", "service": "fake-booking", "seed": "obj-001"})
            return
        if path == "/v1/slots":
            with self.state.lock:
                slots = list(self.state.slots)
            _send_json(self, 200, {"slots": slots})
            return
        if path.startswith("/v1/bookings/"):
            booking_id = path.rsplit("/", 1)[-1]
            with self.state.lock:
                booking = self.state.bookings.get(booking_id)
            if booking is None:
                _send_json(self, 404, {"error": "not_found"})
                return
            _send_json(self, 200, booking)
            return
        _send_json(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/admin/reset":
            self.state.reset()
            _send_json(self, 200, {"status": "reset", "seed": "obj-001"})
            return
        if path == "/v1/bookings":
            with self.state.lock:
                drop = self.state.drop_next_booking
                if drop:
                    self.state.drop_next_booking = False
            if drop:
                _drop(self)
                return
            payload = _read_json(self)
            key = self.headers.get("Idempotency-Key") or ""
            slot_id = str(payload.get("slot_id"))
            with self.state.lock:
                existing = self.state.booking_keys.get(key)
                if existing:
                    _send_json(self, 200, self.state.bookings[existing])
                    return
                booking = {
                    "id": f"bkg_{len(self.state.bookings) + 1}",
                    "slot_id": slot_id,
                    "patient_ref": payload.get("patient_ref"),
                    "status": "confirmed",
                }
                self.state.bookings[booking["id"]] = booking
                self.state.booking_keys[key] = booking["id"]
                self.state.slots = [item for item in self.state.slots if item["id"] != slot_id]
            _send_json(self, 201, booking)
            return
        _send_json(self, 404, {"error": "not_found"})


class FakePvsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FakeVendorState

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            _send_json(self, 200, {"status": "ok", "service": "fake-pvs", "seed": "obj-002"})
            return
        if path == "/v1/patients":
            _send_json(self, 200, {"patients": [{"id": "synth-ada"}]})
            return
        if path == "/v1/patients/synth-ada":
            _send_json(self, 200, {"id": "synth-ada"})
            return
        if path == "/v1/patients/synth-ada/encounters":
            _send_json(self, 200, {"encounters": [{"id": "enc-1", "patient_id": "synth-ada"}]})
            return
        if path.startswith("/v1/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            with self.state.lock:
                task = self.state.tasks.get(task_id)
            if task is None:
                _send_json(self, 404, {"error": "not_found"})
                return
            _send_json(self, 200, task)
            return
        _send_json(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/admin/reset":
            self.state.reset()
            _send_json(self, 200, {"status": "reset", "seed": "obj-002"})
            return
        if path == "/v1/tasks":
            payload = _read_json(self)
            key = self.headers.get("Idempotency-Key") or ""
            with self.state.lock:
                existing = self.state.task_keys.get(key)
                if existing:
                    _send_json(self, 200, self.state.tasks[existing])
                    return
                task = {
                    "id": f"tsk_{len(self.state.tasks) + 1}",
                    "patient_id": payload.get("patient_id"),
                    "title": payload.get("title"),
                    "status": "open",
                }
                self.state.tasks[task["id"]] = task
                self.state.task_keys[key] = task["id"]
            _send_json(self, 201, task)
            return
        _send_json(self, 404, {"error": "not_found"})


class FakeChaosAdminHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FakeVendorState
    service_name: str

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            _send_json(self, 200, {"status": "ok", "service": self.service_name})
            return
        _send_json(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/admin/reset":
            with self.state.lock:
                self.state.drop_next_booking = False
            _send_json(self, 200, {"status": "reset", "service": self.service_name})
            return
        _send_json(self, 404, {"error": "not_found"})

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if path != "/v1/admin/faults":
            _send_json(self, 404, {"error": "not_found"})
            return
        payload = _read_json(self)
        mode = payload.get("mode")
        if mode == "drop_after_upstream":
            with self.state.lock:
                self.state.drop_next_booking = True
        _send_json(self, 200, {"mode": mode, "remaining": payload.get("remaining", 1)})


def serve(handler_cls: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"

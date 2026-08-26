from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import uvicorn

from fake_booking.app import app, store
from fake_booking.settings import Settings


def _start_uvicorn(app_obj: object) -> tuple[uvicorn.Server, threading.Thread, str, int]:
    config = uvicorn.Config(app_obj, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    host, port = server.servers[0].sockets[0].getsockname()[:2]
    return server, thread, str(host), int(port)


def _stop_uvicorn(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)


def _send_partial_post(
    host: str,
    port: int,
    path: str,
    headers: dict[str, str],
    body: bytes,
    first_bytes: int,
) -> socket.socket:
    header_lines = [
        f"POST {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    header_lines.extend(f"{key}: {value}" for key, value in headers.items())
    payload = ("\r\n".join(header_lines) + "\r\n\r\n").encode("ascii") + body[:first_bytes]
    sock = socket.create_connection((host, port), timeout=5)
    sock.sendall(payload)
    return sock


def _finish_body(sock: socket.socket, body: bytes, first_bytes: int) -> None:
    sock.sendall(body[first_bytes:])


def _read_http_response(sock: socket.socket) -> tuple[int, dict]:
    sock.settimeout(5)
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    header_blob, _, rest = bytes(buf).partition(b"\r\n\r\n")
    status_line = header_blob.split(b"\r\n", 1)[0]
    status = int(status_line.split()[1])
    headers: dict[bytes, bytes] = {}
    for line in header_blob.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        headers[name.lower()] = value.strip()
    length = int(headers.get(b"content-length", b"0") or b"0")
    body = rest
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    return status, json.loads(body.decode("utf-8"))


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def test_partial_body_request_cannot_commit_into_new_epoch() -> None:
    store.settings = Settings(seed="obj-001", state_path=None)
    store.reset()
    server, thread, host, port = _start_uvicorn(app)
    try:
        base = f"http://{host}:{port}"
        with httpx.Client(timeout=2.0) as client:
            slot = client.get(f"{base}/v1/slots").json()["slots"][0]
        body = json.dumps(
            {"slot_id": slot["id"], "patient_ref": "synth-ada"},
            separators=(",", ":"),
        ).encode("utf-8")
        first_bytes = max(1, len(body) // 3)
        sock = _send_partial_post(
            host,
            port,
            "/v1/bookings",
            {"Idempotency-Key": "raw-partial-booking"},
            body,
            first_bytes,
        )
        try:
            _wait_until(lambda: store.in_flight_total() >= 1)
            assert store.in_flight_total() >= 1
            late: dict[str, object] = {}
            reset_done = threading.Event()

            def _reset() -> None:
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(f"{base}/v1/admin/reset")
                    late["reset_status"] = response.status_code
                    late["reset_body"] = response.json()
                reset_done.set()

            reset_worker = threading.Thread(target=_reset)
            reset_worker.start()
            time.sleep(0.1)
            assert not reset_done.is_set()
            assert store.in_flight_total() >= 1
            _finish_body(sock, body, first_bytes)
            old_status, old_body = _read_http_response(sock)
            reset_worker.join(timeout=5)
            assert not reset_worker.is_alive()
            assert reset_done.is_set()
            assert late["reset_status"] == 200
            assert late["reset_body"]["status"] == "reset"
            assert old_status == 409
            assert old_body["error"] == "epoch_stale"
        finally:
            sock.close()

        with httpx.Client(timeout=2.0) as client:
            events = client.get(f"{base}/v1/admin/events").json()["events"]
            assert events == []
            remaining = {item["id"] for item in client.get(f"{base}/v1/slots").json()["slots"]}
            assert slot["id"] in remaining
            created = client.post(
                f"{base}/v1/bookings",
                headers={"Idempotency-Key": "raw-post-reset-booking"},
                json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
            )
            assert created.status_code == 201
            types = [
                event["type"]
                for event in client.get(f"{base}/v1/admin/events").json()["events"]
            ]
            assert "booking_requested" in types
            assert "booking_committed" in types
        assert store.in_flight_total() == 0
    finally:
        _stop_uvicorn(server, thread)
        store.reset()

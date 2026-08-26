from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import uvicorn

from fake_pvs.app import app, store
from fake_pvs.settings import Settings


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


def _send_partial_request(
    host: str,
    port: int,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    first_bytes: int,
) -> socket.socket:
    header_lines = [
        f"{method} {path} HTTP/1.1",
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


def _send_partial_post(
    host: str,
    port: int,
    path: str,
    headers: dict[str, str],
    body: bytes,
    first_bytes: int,
) -> socket.socket:
    return _send_partial_request(host, port, "POST", path, headers, body, first_bytes)


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
    store.settings = Settings(seed="obj-002", state_path=None)
    store.reset()
    server, thread, host, port = _start_uvicorn(app)
    try:
        base = f"http://{host}:{port}"
        body = json.dumps(
            {
                "patient_id": "synth-ada",
                "title": "synth-task",
                "priority": "normal",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        first_bytes = max(1, len(body) // 3)
        sock = _send_partial_post(
            host,
            port,
            "/v1/tasks",
            {"Idempotency-Key": "raw-partial-pvs"},
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
            created = client.post(
                f"{base}/v1/tasks",
                headers={"Idempotency-Key": "raw-post-reset-pvs"},
                json={
                    "patient_id": "synth-ada",
                    "title": "synth-task",
                    "priority": "normal",
                },
            )
            assert created.status_code == 201
            types = [
                event["type"]
                for event in client.get(f"{base}/v1/admin/events").json()["events"]
            ]
            assert "task_requested" in types
            assert "task_committed" in types
        assert store.in_flight_total() == 0
    finally:
        _stop_uvicorn(server, thread)
        store.reset()


def test_new_post_during_reset_does_not_block_event_loop() -> None:
    store.settings = Settings(seed="obj-002", state_path=None)
    store.reset()
    server, thread, host, port = _start_uvicorn(app)
    try:
        base = f"http://{host}:{port}"
        old_body = json.dumps(
            {
                "patient_id": "synth-ada",
                "title": "synth-task",
                "priority": "normal",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        first_bytes = max(1, len(old_body) // 3)
        old_sock = _send_partial_post(
            host,
            port,
            "/v1/tasks",
            {"Idempotency-Key": "raw-old-pvs"},
            old_body,
            first_bytes,
        )
        try:
            _wait_until(lambda: store.in_flight_total() >= 1)
            late: dict[str, object] = {}
            reset_done = threading.Event()

            def _reset() -> None:
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(f"{base}/v1/admin/reset")
                    late["reset_status"] = response.status_code
                reset_done.set()

            reset_worker = threading.Thread(target=_reset)
            reset_worker.start()
            _wait_until(store.is_resetting)
            assert not reset_done.is_set()

            new_done = threading.Event()

            def _new_post() -> None:
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(
                        f"{base}/v1/tasks",
                        headers={"Idempotency-Key": "raw-new-pvs"},
                        json={
                            "patient_id": "synth-ada",
                            "title": "synth-task",
                            "priority": "normal",
                        },
                    )
                    late["new_status"] = response.status_code
                    late["new_body"] = response.json()
                new_done.set()

            new_worker = threading.Thread(target=_new_post)
            new_worker.start()
            time.sleep(0.1)
            assert not new_done.is_set()
            assert store.is_resetting()
            assert not reset_done.is_set()
            _finish_body(old_sock, old_body, first_bytes)
            old_status, old_body_json = _read_http_response(old_sock)
            reset_worker.join(timeout=5)
            new_worker.join(timeout=5)
            assert not reset_worker.is_alive()
            assert not new_worker.is_alive()
            assert reset_done.is_set()
            assert new_done.is_set()
            assert late["reset_status"] == 200
            assert old_status == 409
            assert old_body_json["error"] == "epoch_stale"
            assert late["new_status"] == 201
            assert late["new_body"]["patient_id"] == "synth-ada"
        finally:
            old_sock.close()
        with httpx.Client(timeout=2.0) as client:
            types = [
                event["type"]
                for event in client.get(f"{base}/v1/admin/events").json()["events"]
            ]
            assert "task_requested" in types
            assert "task_committed" in types
            traces = {
                event["trace_id"]
                for event in client.get(f"{base}/v1/admin/events").json()["events"]
            }
            assert traces == {f"tr_{store.epoch():06d}_000001"}
    finally:
        _stop_uvicorn(server, thread)
        store.reset()


def test_partial_fault_put_cannot_configure_new_epoch() -> None:
    store.settings = Settings(seed="obj-002", state_path=None)
    store.reset()
    server, thread, host, port = _start_uvicorn(app)
    try:
        base = f"http://{host}:{port}"
        body = json.dumps(
            {"mode": "fail_before_commit", "remaining": 1},
            separators=(",", ":"),
        ).encode("utf-8")
        first_bytes = max(1, len(body) // 3)
        sock = _send_partial_request(
            host,
            port,
            "PUT",
            "/v1/admin/faults",
            {},
            body,
            first_bytes,
        )
        try:
            _wait_until(lambda: store.in_flight_total() >= 1)
            late: dict[str, object] = {}
            reset_done = threading.Event()

            def _reset() -> None:
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(f"{base}/v1/admin/reset")
                    late["reset_status"] = response.status_code
                reset_done.set()

            reset_worker = threading.Thread(target=_reset)
            reset_worker.start()
            _wait_until(store.is_resetting)
            assert not reset_done.is_set()
            _finish_body(sock, body, first_bytes)
            old_status, old_body = _read_http_response(sock)
            reset_worker.join(timeout=5)
            assert not reset_worker.is_alive()
            assert late["reset_status"] == 200
            assert old_status == 409
            assert old_body["error"] == "epoch_stale"
        finally:
            sock.close()
        with httpx.Client(timeout=2.0) as client:
            current = client.get(f"{base}/v1/admin/faults").json()
            assert current["mode"] == "none"
            assert current["remaining"] == 0
            events = client.get(f"{base}/v1/admin/events").json()["events"]
            assert events == []
            armed = client.put(
                f"{base}/v1/admin/faults",
                json={"mode": "fail_before_commit", "remaining": 1},
            )
            assert armed.status_code == 200
            assert armed.json()["mode"] == "fail_before_commit"
            types = [
                event["type"]
                for event in client.get(f"{base}/v1/admin/events").json()["events"]
            ]
            assert "fault_configured" in types
    finally:
        _stop_uvicorn(server, thread)
        store.reset()


def test_overlapping_resets_serialize_and_preserve_new_create() -> None:
    store.settings = Settings(seed="obj-002", state_path=None)
    store.reset()
    server, thread, host, port = _start_uvicorn(app)
    try:
        base = f"http://{host}:{port}"
        old_body = json.dumps(
            {
                "patient_id": "synth-ada",
                "title": "synth-task",
                "priority": "normal",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        first_bytes = max(1, len(old_body) // 3)
        old_sock = _send_partial_post(
            host,
            port,
            "/v1/tasks",
            {"Idempotency-Key": "raw-old-overlap-pvs"},
            old_body,
            first_bytes,
        )
        try:
            _wait_until(lambda: store.in_flight_total() >= 1)
            late: dict[str, object] = {}
            reset1_done = threading.Event()
            reset2_done = threading.Event()
            owner_order: list[int] = []

            def _reset(name: str, done: threading.Event) -> None:
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(f"{base}/v1/admin/reset")
                    late[f"{name}_status"] = response.status_code
                    late[f"{name}_body"] = response.json()
                done.set()

            reset1 = threading.Thread(target=_reset, args=("reset1", reset1_done))
            reset1.start()
            _wait_until(store.is_resetting)
            owner_order.append(store.reset_generation())
            assert not reset1_done.is_set()

            reset2 = threading.Thread(target=_reset, args=("reset2", reset2_done))
            reset2.start()
            _wait_until(lambda: store.pending_reset_count() >= 2)
            assert store.reset_generation() == owner_order[0]
            time.sleep(0.1)
            assert not reset1_done.is_set()
            assert not reset2_done.is_set()
            assert store.is_resetting()

            new_done = threading.Event()

            def _new_post() -> None:
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(
                        f"{base}/v1/tasks",
                        headers={"Idempotency-Key": "raw-new-overlap-pvs"},
                        json={
                            "patient_id": "synth-ada",
                            "title": "synth-task",
                            "priority": "normal",
                        },
                    )
                    late["new_status"] = response.status_code
                    late["new_body"] = response.json()
                new_done.set()

            new_worker = threading.Thread(target=_new_post)
            new_worker.start()
            time.sleep(0.1)
            assert not new_done.is_set()
            assert not reset1_done.is_set()
            assert not reset2_done.is_set()

            _finish_body(old_sock, old_body, first_bytes)
            old_status, old_body_json = _read_http_response(old_sock)
            reset1.join(timeout=5)
            reset2.join(timeout=5)
            new_worker.join(timeout=5)
            assert not reset1.is_alive()
            assert not reset2.is_alive()
            assert not new_worker.is_alive()
            assert reset1_done.is_set()
            assert reset2_done.is_set()
            assert new_done.is_set()
            assert late["reset1_status"] == 200
            assert late["reset2_status"] == 200
            assert owner_order == [store.reset_generation() - 1]
            assert store.reset_generation() == owner_order[0] + 1
            assert old_status == 409
            assert old_body_json["error"] == "epoch_stale"
            assert late["new_status"] == 201
            task_id = late["new_body"]["id"]
        finally:
            old_sock.close()

        with httpx.Client(timeout=2.0) as client:
            fetched = client.get(f"{base}/v1/tasks/{task_id}")
            assert fetched.status_code == 200
            assert fetched.json()["id"] == task_id
            events = client.get(f"{base}/v1/admin/events").json()["events"]
            types = [event["type"] for event in events]
            assert "task_requested" in types
            assert "task_committed" in types
            traces = {event["trace_id"] for event in events}
            assert traces == {f"tr_{store.epoch():06d}_000001"}
        assert store.in_flight_total() == 0
        assert store.pending_reset_count() == 0
        assert not store.is_resetting()
    finally:
        _stop_uvicorn(server, thread)
        store.reset()


def test_overlapping_resets_cannot_erase_acknowledged_fault() -> None:
    store.settings = Settings(seed="obj-002", state_path=None)
    store.reset()
    server, thread, host, port = _start_uvicorn(app)
    try:
        base = f"http://{host}:{port}"
        body = json.dumps(
            {"mode": "fail_before_commit", "remaining": 1},
            separators=(",", ":"),
        ).encode("utf-8")
        first_bytes = max(1, len(body) // 3)
        sock = _send_partial_request(
            host,
            port,
            "PUT",
            "/v1/admin/faults",
            {},
            body,
            first_bytes,
        )
        try:
            _wait_until(lambda: store.in_flight_total() >= 1)
            late: dict[str, object] = {}
            reset1_done = threading.Event()
            reset2_done = threading.Event()
            owner_order: list[int] = []

            def _reset(name: str, done: threading.Event) -> None:
                with httpx.Client(timeout=5.0) as client:
                    response = client.post(f"{base}/v1/admin/reset")
                    late[f"{name}_status"] = response.status_code
                done.set()

            reset1 = threading.Thread(target=_reset, args=("reset1", reset1_done))
            reset1.start()
            _wait_until(store.is_resetting)
            owner_order.append(store.reset_generation())
            reset2 = threading.Thread(target=_reset, args=("reset2", reset2_done))
            reset2.start()
            _wait_until(lambda: store.pending_reset_count() >= 2)
            assert store.reset_generation() == owner_order[0]
            time.sleep(0.1)
            assert not reset1_done.is_set()
            assert not reset2_done.is_set()

            new_done = threading.Event()

            def _new_fault() -> None:
                with httpx.Client(timeout=5.0) as client:
                    response = client.put(
                        f"{base}/v1/admin/faults",
                        json={"mode": "delay", "delay_ms": 5, "remaining": 1},
                    )
                    late["new_status"] = response.status_code
                    late["new_body"] = response.json()
                new_done.set()

            new_worker = threading.Thread(target=_new_fault)
            new_worker.start()
            time.sleep(0.1)
            assert not new_done.is_set()
            assert not reset1_done.is_set()
            assert not reset2_done.is_set()

            _finish_body(sock, body, first_bytes)
            old_status, old_body = _read_http_response(sock)
            reset1.join(timeout=5)
            reset2.join(timeout=5)
            new_worker.join(timeout=5)
            assert not reset1.is_alive()
            assert not reset2.is_alive()
            assert not new_worker.is_alive()
            assert late["reset1_status"] == 200
            assert late["reset2_status"] == 200
            assert owner_order == [store.reset_generation() - 1]
            assert store.reset_generation() == owner_order[0] + 1
            assert old_status == 409
            assert old_body["error"] == "epoch_stale"
            assert late["new_status"] == 200
            assert late["new_body"]["mode"] == "delay"
        finally:
            sock.close()

        with httpx.Client(timeout=2.0) as client:
            current = client.get(f"{base}/v1/admin/faults").json()
            assert current["mode"] == "delay"
            assert current["remaining"] == 1
            types = [
                event["type"]
                for event in client.get(f"{base}/v1/admin/events").json()["events"]
            ]
            assert "fault_configured" in types
        assert store.pending_reset_count() == 0
        assert not store.is_resetting()
    finally:
        _stop_uvicorn(server, thread)
        store.reset()


from __future__ import annotations

import json
import socket

import httpx


def _split_url(url: str) -> tuple[str, int]:
    parsed = httpx.URL(url)
    assert parsed.host is not None and parsed.port is not None
    return parsed.host, parsed.port


def encode_chunked(
    payload: bytes,
    sizes: list[int] | None = None,
    *,
    extension: str | None = None,
    trailers: list[str] | None = None,
) -> bytes:
    if sizes is None:
        sizes = [len(payload)] if payload else []
    if sum(sizes) != len(payload):
        raise AssertionError("chunk sizes must cover the payload exactly")
    out = bytearray()
    offset = 0
    for index, size in enumerate(sizes):
        chunk = payload[offset : offset + size]
        ext = f";{extension}" if extension and index == 0 else ""
        out.extend(f"{len(chunk):x}{ext}\r\n".encode("ascii"))
        out.extend(chunk)
        out.extend(b"\r\n")
        offset += size
    out.extend(b"0\r\n")
    for trailer in trailers or []:
        out.extend(f"{trailer}\r\n".encode("ascii"))
    out.extend(b"\r\n")
    return bytes(out)


def _read_http_response(sock: socket.socket) -> tuple[int, bytes, dict]:
    sock.settimeout(5)
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    header_blob, _, rest = bytes(buf).partition(b"\r\n\r\n")
    if not header_blob:
        raise AssertionError("no HTTP response received")
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
    return status, body[:length], {
        key.decode("latin1"): value.decode("latin1") for key, value in headers.items()
    }


def _raw_request(
    host: str,
    port: int,
    *,
    method: str,
    target: str,
    headers: list[str],
    body: bytes = b"",
    shutdown_write: bool = False,
) -> tuple[int, bytes, dict]:
    request = (
        f"{method} {target} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        + "".join(f"{line}\r\n" for line in headers)
        + "Connection: close\r\n"
        + "\r\n"
    ).encode("ascii") + body
    sock = socket.create_connection((host, port), timeout=5)
    try:
        sock.sendall(request)
        if shutdown_write:
            sock.shutdown(socket.SHUT_WR)
        return _read_http_response(sock)
    finally:
        sock.close()


def _arm_drop_before(http: httpx.Client, admin_url: str) -> None:
    armed = http.put(
        f"{admin_url}/v1/admin/faults",
        json={
            "mode": "drop_before_upstream",
            "remaining": 1,
            "method": "POST",
            "path": "/v1/bookings",
        },
    )
    assert armed.status_code == 200


def test_multi_chunk_json_reaches_upstream_byte_for_byte(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    host, port = _split_url(proxy_url)
    payload = b'{"slot_id":"slot-1","patient_ref":"synth-ada"}'
    status, body, _headers = _raw_request(
        host,
        port,
        method="POST",
        target="/v1/bookings",
        headers=[
            "Content-Type: application/json",
            "Transfer-Encoding: chunked",
            "Idempotency-Key: chunked-multi-0001",
        ],
        body=encode_chunked(payload, [12, 10, len(payload) - 22]),
    )
    assert status == 201
    assert json.loads(body)["id"] == "bkg_1"
    assert len(upstream.requests) == 1  # type: ignore[attr-defined]
    received = upstream.requests[0]  # type: ignore[attr-defined]
    assert received["body"] == payload
    assert received["idempotency_key"] == "chunked-multi-0001"
    assert received["path"] == "/v1/bookings"
    assert received["method"] == "POST"


def test_chunk_extensions_do_not_alter_decoded_bytes(
    chaos: dict[str, object],
) -> None:
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    host, port = _split_url(proxy_url)
    payload = b'{"slot_id":"slot-1","patient_ref":"synth-ada"}'
    status, _body, _headers = _raw_request(
        host,
        port,
        method="POST",
        target="/v1/bookings?resource=alpha",
        headers=[
            "Content-Type: application/json",
            "Transfer-Encoding: chunked",
            "Idempotency-Key: chunked-ext-0001",
        ],
        body=encode_chunked(payload, [8, len(payload) - 8], extension="ext=1;foo=bar"),
    )
    assert status == 201
    received = upstream.requests[0]  # type: ignore[attr-defined]
    assert received["body"] == payload
    assert received["path"] == "/v1/bookings?resource=alpha"


def test_zero_chunk_and_trailers_complete(
    chaos: dict[str, object],
) -> None:
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    host, port = _split_url(proxy_url)
    payload = b'{"slot_id":"slot-1","patient_ref":"synth-ada"}'
    status, _body, _headers = _raw_request(
        host,
        port,
        method="POST",
        target="/v1/bookings",
        headers=[
            "Content-Type: application/json",
            "Transfer-Encoding: chunked",
            "Idempotency-Key: chunked-trailer-0001",
        ],
        body=encode_chunked(payload, trailers=["X-Unused: ignore-me", "X-Also: 1"]),
    )
    assert status == 201
    assert upstream.requests[0]["body"] == payload  # type: ignore[attr-defined]


def test_malformed_chunk_size_is_4xx_without_upstream_or_fault(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    controller = chaos["controller"]
    _arm_drop_before(http, admin_url)
    host, port = _split_url(proxy_url)
    status, body, _headers = _raw_request(
        host,
        port,
        method="POST",
        target="/v1/bookings",
        headers=[
            "Content-Type: application/json",
            "Transfer-Encoding: chunked",
            "Idempotency-Key: chunked-bad-size",
        ],
        body=b"zz\r\nnope\r\n0\r\n\r\n",
    )
    assert 400 <= status < 500
    assert json.loads(body)["error"] == "invalid_body"
    assert upstream.requests == []  # type: ignore[attr-defined]
    assert controller.snapshot().remaining == 1
    types = [event["type"] for event in controller.events()]
    assert "fault_consumed" not in types
    assert "upstream_completed" not in types


def test_incomplete_chunk_is_4xx_without_upstream_or_fault(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    controller = chaos["controller"]
    _arm_drop_before(http, admin_url)
    host, port = _split_url(proxy_url)
    status, body, _headers = _raw_request(
        host,
        port,
        method="POST",
        target="/v1/bookings",
        headers=[
            "Content-Type: application/json",
            "Transfer-Encoding: chunked",
            "Idempotency-Key: chunked-eof",
        ],
        body=b"10\r\npartial",
        shutdown_write=True,
    )
    assert 400 <= status < 500
    assert json.loads(body)["error"] == "invalid_body"
    assert upstream.requests == []  # type: ignore[attr-defined]
    assert controller.snapshot().remaining == 1
    types = [event["type"] for event in controller.events()]
    assert "fault_consumed" not in types
    assert "upstream_completed" not in types


def test_content_length_and_transfer_encoding_rejected(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    controller = chaos["controller"]
    _arm_drop_before(http, admin_url)
    host, port = _split_url(proxy_url)
    payload = b'{"slot_id":"slot-1","patient_ref":"synth-ada"}'
    status, body, _headers = _raw_request(
        host,
        port,
        method="POST",
        target="/v1/bookings",
        headers=[
            "Content-Type: application/json",
            f"Content-Length: {len(payload)}",
            "Transfer-Encoding: chunked",
            "Idempotency-Key: chunked-ambiguous",
        ],
        body=payload,
    )
    assert 400 <= status < 500
    assert json.loads(body)["error"] == "invalid_body"
    assert upstream.requests == []  # type: ignore[attr-defined]
    assert controller.snapshot().remaining == 1
    types = [event["type"] for event in controller.events()]
    assert "fault_consumed" not in types


def test_content_length_body_still_forwards(
    chaos: dict[str, object],
) -> None:
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    host, port = _split_url(proxy_url)
    payload = b'{"slot_id":"slot-1","patient_ref":"synth-ada"}'
    status, _body, _headers = _raw_request(
        host,
        port,
        method="POST",
        target="/v1/bookings",
        headers=[
            "Content-Type: application/json",
            f"Content-Length: {len(payload)}",
            "Idempotency-Key: content-length-0001",
        ],
        body=payload,
    )
    assert status == 201
    assert upstream.requests[0]["body"] == payload  # type: ignore[attr-defined]


def test_chunked_drop_before_upstream_still_consumes_fault(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    _arm_drop_before(http, admin_url)
    host, port = _split_url(proxy_url)
    payload = b'{"slot_id":"slot-1","patient_ref":"synth-ada"}'
    sock = socket.create_connection((host, port), timeout=5)
    try:
        sock.sendall(
            (
                "POST /v1/bookings HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Content-Type: application/json\r\n"
                "Transfer-Encoding: chunked\r\n"
                "Idempotency-Key: chunked-drop-0001\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            + encode_chunked(payload, [len(payload)])
        )
        sock.settimeout(2)
        leftover = sock.recv(4096)
        assert leftover == b""
    except (TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        sock.close()
    assert upstream.requests == []  # type: ignore[attr-defined]
    events = http.get(f"{admin_url}/v1/admin/events").json()["events"]
    types = [event["type"] for event in events]
    assert "fault_consumed" in types
    assert "dropped_before_upstream" in types
    assert "upstream_completed" not in types


def test_chunked_delay_forwards_decoded_body(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    armed = http.put(
        f"{admin_url}/v1/admin/faults",
        json={
            "mode": "delay",
            "remaining": 1,
            "delay_ms": 20,
            "method": "POST",
            "path": "/v1/bookings",
        },
    )
    assert armed.status_code == 200
    host, port = _split_url(proxy_url)
    payload = b'{"slot_id":"slot-1","patient_ref":"synth-ada"}'
    status, _body, _headers = _raw_request(
        host,
        port,
        method="POST",
        target="/v1/bookings",
        headers=[
            "Content-Type: application/json",
            "Transfer-Encoding: chunked",
            "Idempotency-Key: chunked-delay-0001",
        ],
        body=encode_chunked(payload, [len(payload)]),
    )
    assert status == 201
    assert upstream.requests[0]["body"] == payload  # type: ignore[attr-defined]
    types = [event["type"] for event in http.get(f"{admin_url}/v1/admin/events").json()["events"]]
    assert "fault_consumed" in types
    assert "response_delayed" in types
    assert "upstream_completed" in types


def test_chunked_admin_fault_put_configures_fault(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    admin_url = str(chaos["admin_url"])
    host, port = _split_url(admin_url)
    payload = (
        b'{"mode":"drop_before_upstream","remaining":1,'
        b'"method":"POST","path":"/v1/bookings"}'
    )
    status, body, _headers = _raw_request(
        host,
        port,
        method="PUT",
        target="/v1/admin/faults",
        headers=["Content-Type: application/json", "Transfer-Encoding: chunked"],
        body=encode_chunked(payload, [20, len(payload) - 20]),
    )
    assert status == 200
    assert json.loads(body)["mode"] == "drop_before_upstream"
    current = http.get(f"{admin_url}/v1/admin/faults")
    assert current.json()["remaining"] == 1


def test_malformed_admin_chunked_put_does_not_configure_fault(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    admin_url = str(chaos["admin_url"])
    host, port = _split_url(admin_url)
    status, body, _headers = _raw_request(
        host,
        port,
        method="PUT",
        target="/v1/admin/faults",
        headers=["Content-Type: application/json", "Transfer-Encoding: chunked"],
        body=b"gg\r\n{}\r\n0\r\n\r\n",
    )
    assert 400 <= status < 500
    assert json.loads(body)["error"] == "invalid_body"
    current = http.get(f"{admin_url}/v1/admin/faults")
    assert current.json()["mode"] == "none"
    assert current.json()["remaining"] == 0


def test_unsupported_transfer_coding_is_rejected(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    controller = chaos["controller"]
    _arm_drop_before(http, admin_url)
    host, port = _split_url(proxy_url)
    status, body, _headers = _raw_request(
        host,
        port,
        method="POST",
        target="/v1/bookings",
        headers=[
            "Content-Type: application/json",
            "Transfer-Encoding: gzip, chunked",
            "Idempotency-Key: chunked-gzip",
        ],
        body=b"0\r\n\r\n",
    )
    assert 400 <= status < 500
    assert json.loads(body)["error"] == "invalid_body"
    assert upstream.requests == []  # type: ignore[attr-defined]
    assert controller.snapshot().remaining == 1

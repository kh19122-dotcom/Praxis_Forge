from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlsplit

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def request_path(handler: BaseHTTPRequestHandler) -> str:
    return urlsplit(handler.path).path or "/"


def request_target(handler: BaseHTTPRequestHandler) -> str:
    split = urlsplit(handler.path)
    target = split.path or "/"
    if split.query:
        return f"{target}?{split.query}"
    return target


def read_body(handler: BaseHTTPRequestHandler) -> bytes:
    raw_length = handler.headers.get("Content-Length")
    if not raw_length:
        return b""
    length = int(raw_length)
    if length <= 0:
        return b""
    return handler.rfile.read(length)


def header_map(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in handler.headers.items():
        if key.lower() in HOP_BY_HOP or key.lower() == "accept-encoding":
            continue
        headers[key] = value
    return headers


def idempotency_key(handler: BaseHTTPRequestHandler) -> str | None:
    value = handler.headers.get("Idempotency-Key")
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def send_json(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: dict[str, Any],
) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)
    handler.close_connection = True


def send_raw(
    handler: BaseHTTPRequestHandler,
    status: int,
    headers: list[tuple[str, str]],
    body: bytes,
) -> None:
    handler.send_response(status)
    for key, value in headers:
        if key.lower() in HOP_BY_HOP or key.lower() in {"date", "server"}:
            continue
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    if body and handler.command != "HEAD":
        handler.wfile.write(body)
    handler.close_connection = True

from __future__ import annotations

import json
import re
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

_CHUNK_SIZE_RE = re.compile(r"^[0-9A-Fa-f]+$")
_MAX_CHUNK_LINE = 64 * 1024


class BodyFramingError(Exception):
    """Client request body framing is malformed, ambiguous, or unsupported."""


def request_path(handler: BaseHTTPRequestHandler) -> str:
    return urlsplit(handler.path).path or "/"


def request_target(handler: BaseHTTPRequestHandler) -> str:
    split = urlsplit(handler.path)
    target = split.path or "/"
    if split.query:
        return f"{target}?{split.query}"
    return target


def read_body(handler: BaseHTTPRequestHandler) -> bytes:
    transfer_values = handler.headers.get_all("Transfer-Encoding") or []
    length_values = handler.headers.get_all("Content-Length") or []
    if transfer_values and length_values:
        raise BodyFramingError("Content-Length and Transfer-Encoding cannot both be present")
    if transfer_values:
        return _read_chunked_body(handler, transfer_values)
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


def _header_codings(values: list[str]) -> list[str]:
    codings: list[str] = []
    for value in values:
        for part in value.split(","):
            coding = part.strip().lower()
            if coding:
                codings.append(coding)
    return codings


def _read_crlf_line(handler: BaseHTTPRequestHandler) -> bytes:
    line = handler.rfile.readline(_MAX_CHUNK_LINE + 1)
    if not line:
        raise BodyFramingError("incomplete chunked body")
    if len(line) > _MAX_CHUNK_LINE or not line.endswith(b"\n"):
        raise BodyFramingError("malformed chunk size")
    if not line.endswith(b"\r\n"):
        raise BodyFramingError("invalid chunk delimiter")
    return line[:-2]


def _read_chunked_body(handler: BaseHTTPRequestHandler, transfer_values: list[str]) -> bytes:
    if _header_codings(transfer_values) != ["chunked"]:
        raise BodyFramingError("unsupported Transfer-Encoding")
    chunks: list[bytes] = []
    while True:
        size_line = _read_crlf_line(handler)
        try:
            size_token = size_line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise BodyFramingError("malformed chunk size") from exc
        size_hex = size_token.split(";", 1)[0].strip()
        if not _CHUNK_SIZE_RE.fullmatch(size_hex):
            raise BodyFramingError("malformed chunk size")
        size = int(size_hex, 16)
        if size == 0:
            _consume_trailers(handler)
            break
        data = handler.rfile.read(size)
        if len(data) < size:
            raise BodyFramingError("incomplete chunked body")
        delimiter = handler.rfile.read(2)
        if delimiter != b"\r\n":
            raise BodyFramingError("invalid chunk delimiter")
        chunks.append(data)
    return b"".join(chunks)


def _consume_trailers(handler: BaseHTTPRequestHandler) -> None:
    while True:
        line = handler.rfile.readline(_MAX_CHUNK_LINE + 1)
        if not line:
            raise BodyFramingError("incomplete chunked body")
        if line == b"\r\n":
            return
        if len(line) > _MAX_CHUNK_LINE or not line.endswith(b"\r\n"):
            raise BodyFramingError("malformed chunked trailer")

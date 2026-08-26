from __future__ import annotations

import re
from pathlib import Path

HOST_PORT_PUBLISH = re.compile(r"(?:\d+\.\d+\.\d+\.\d+:)?\d+:\d+\Z")


def _compose_file() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "docker-compose.yml",
        Path("/docker-compose.yml"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("docker-compose.yml not found")


ALLOWED_PUBLISH = {
    "127.0.0.1:8080:8080",
    "127.0.0.1:8081:8081",
    "127.0.0.1:8090:8090",
    "127.0.0.1:8091:8091",
    "127.0.0.1:8092:8092",
    "127.0.0.1:8093:8093",
}


def test_host_published_chaos_and_admin_ports_are_loopback_only() -> None:
    text = _compose_file().read_text(encoding="utf-8")
    assert "127.0.0.1:8090:8090" in text
    assert "127.0.0.1:8091:8091" in text
    assert "127.0.0.1:8092:8092" in text
    assert "127.0.0.1:8093:8093" in text
    assert "FORGE_BOOKING_CHAOS_URL: http://chaos-booking:8090" in text
    assert "FORGE_PVS_CHAOS_URL: http://chaos-pvs:8091" in text
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip().lstrip("-").strip().strip("\"'")
        if HOST_PORT_PUBLISH.fullmatch(stripped):
            assert stripped in ALLOWED_PUBLISH

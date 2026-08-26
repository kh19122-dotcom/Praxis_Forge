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


def test_compose_keeps_admin_ports_loopback_and_runner_on_internal_http() -> None:
    text = _compose_file().read_text(encoding="utf-8")
    assert "127.0.0.1:8080:8080" in text
    assert "127.0.0.1:8081:8081" in text
    assert "FORGE_BOOKING_URL: http://fake-booking:8080" in text
    assert "FORGE_PVS_URL: http://fake-pvs:8081" in text
    assert "scenario-runner:" in text
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip().lstrip("-").strip().strip("\"'")
        if HOST_PORT_PUBLISH.fullmatch(stripped):
            assert stripped in {"127.0.0.1:8080:8080", "127.0.0.1:8081:8081"}

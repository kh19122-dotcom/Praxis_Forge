from __future__ import annotations

import re
from pathlib import Path

HOST_PORT_PUBLISH = re.compile(r"(?:\d+\.\d+\.\d+\.\d+:)?\d+:8080\Z")


def _compose_file() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "docker-compose.yml",
        Path("/docker-compose.yml"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("docker-compose.yml not found")


def test_default_compose_port_is_loopback_only() -> None:
    text = _compose_file().read_text(encoding="utf-8")
    assert "127.0.0.1:8080:8080" in text
    assert "FORGE_STATE_PATH: /var/lib/forge/state.json" in text
    assert "fake-booking-state:/var/lib/forge" in text
    assert "docker.sock" not in text
    assert "/var/run/docker.sock" not in text
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip().lstrip("-").strip().strip("\"'")
        if HOST_PORT_PUBLISH.fullmatch(stripped):
            assert stripped == "127.0.0.1:8080:8080"

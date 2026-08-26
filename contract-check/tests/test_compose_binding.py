from __future__ import annotations

import re
from pathlib import Path

HOST_PORT_PUBLISH = re.compile(r"(?:\d+\.\d+\.\d+\.\d+:)?\d+:\d+\Z")

ALLOWED_PUBLISH = {
    "127.0.0.1:8080:8080",
    "127.0.0.1:8081:8081",
    "127.0.0.1:8090:8090",
    "127.0.0.1:8091:8091",
    "127.0.0.1:8092:8092",
    "127.0.0.1:8093:8093",
}


def _compose_file() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "docker-compose.yml",
        Path("/docker-compose.yml"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("docker-compose.yml not found")


def test_compose_runs_contract_check_over_internal_http() -> None:
    text = _compose_file().read_text(encoding="utf-8")
    assert "contract-check:" in text
    assert "FORGE_BOOKING_URL: http://fake-booking:8080" in text
    assert "FORGE_PVS_URL: http://fake-pvs:8081" in text
    assert "docker.sock" not in text
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    docker_text = dockerfile.read_text(encoding="utf-8")
    assert "docker.sock" not in docker_text
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip().lstrip("-").strip().strip("\"'")
        if HOST_PORT_PUBLISH.fullmatch(stripped):
            assert stripped in ALLOWED_PUBLISH

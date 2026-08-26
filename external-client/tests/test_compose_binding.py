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


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _compose_text() -> str:
    return (_root() / "docker-compose.yml").read_text(encoding="utf-8")


def _lab_text() -> str:
    return (_root() / "docker-compose.lab.yml").read_text(encoding="utf-8")


def _client_text() -> str:
    return (_root() / "external-client" / "docker-compose.yml").read_text(encoding="utf-8")


def _publishes(text: str) -> list[str]:
    found: list[str] = []
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip().lstrip("-").strip().strip("\"'")
        if HOST_PORT_PUBLISH.fullmatch(stripped):
            found.append(stripped)
    return found


def test_default_compose_stays_loopback_and_off_lab_network() -> None:
    text = _compose_text()
    assert "127.0.0.1:8080:8080" in text
    assert "127.0.0.1:8081:8081" in text
    assert "127.0.0.1:8090:8090" in text
    assert "127.0.0.1:8091:8091" in text
    assert "praxis-forge-lab" not in text
    assert "0.0.0.0:" not in text
    assert "docker.sock" not in text
    assert "/var/run/docker.sock" not in text
    for publish in _publishes(text):
        assert publish in ALLOWED_PUBLISH


def test_lab_overlay_attaches_only_chaos_to_named_bridge() -> None:
    text = _lab_text()
    assert "name: praxis-forge-lab" in text
    assert "driver: bridge" in text
    assert "chaos-booking:" in text
    assert "chaos-pvs:" in text
    assert "- chaos-booking" in text or "chaos-booking" in text
    assert "- forge-booking" in text
    assert "- forge-pvs" in text
    assert "fake-booking:" not in text
    assert "fake-pvs:" not in text
    assert "0.0.0.0:" not in text
    assert "docker.sock" not in text
    assert "/var/run/docker.sock" not in text
    assert _publishes(text) == []


def test_external_client_compose_joins_lab_network_only() -> None:
    text = _client_text()
    assert "name: praxis-forge-lab" in text
    assert "external: true" in text
    assert "FORGE_BOOKING_URL: http://chaos-booking:8090" in text
    assert "FORGE_PVS_URL: http://chaos-pvs:8091" in text
    assert "FORGE_BOOKING_CHAOS_ADMIN_URL: http://chaos-booking:8092" in text
    assert "FORGE_PVS_CHAOS_ADMIN_URL: http://chaos-pvs:8093" in text
    assert "http://fake-booking" not in text
    assert "http://fake-pvs" not in text
    assert "0.0.0.0:" not in text
    assert "docker.sock" not in text
    assert "/var/run/docker.sock" not in text
    assert _publishes(text) == []

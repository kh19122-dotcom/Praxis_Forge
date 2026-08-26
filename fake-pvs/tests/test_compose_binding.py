from __future__ import annotations

from pathlib import Path


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
    assert "127.0.0.1:8081:8081" in text
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip().strip("-").strip().strip("\"'")
        if stripped.endswith(":8081"):
            assert stripped == "127.0.0.1:8081:8081"

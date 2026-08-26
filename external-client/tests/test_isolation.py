from __future__ import annotations

from pathlib import Path


def test_source_has_no_simulator_chaos_runner_or_praxisos_imports() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = (
        "fake_booking",
        "fake_pvs",
        "chaos_proxy",
        "scenario_runner",
        "contract_check",
        "praxisos",
        "PraxisOS",
        "docker.sock",
    )
    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path} contains {token}"
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    for token in forbidden:
        assert token not in pyproject
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "docker.sock" not in dockerfile
    assert "docker.sock" not in compose
    assert "/var/run/docker.sock" not in compose

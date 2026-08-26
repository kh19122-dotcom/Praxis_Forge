from __future__ import annotations

from pathlib import Path


def test_source_has_no_simulator_or_praxisos_imports() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = ("fake_booking", "fake_pvs", "chaos_proxy", "praxisos", "PraxisOS", "docker.sock")
    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path} contains {token}"
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    for token in forbidden:
        assert token not in pyproject

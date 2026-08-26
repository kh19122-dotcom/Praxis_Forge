from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from chaos_proxy.controller import FaultController


def test_normal_forwarding(chaos: dict[str, object], http: httpx.Client) -> None:
    proxy_url = str(chaos["proxy_url"])
    response = http.get(f"{proxy_url}/healthz")
    assert response.status_code == 200
    assert response.json()["service"] == "upstream"


def test_admin_arm_and_reset(chaos: dict[str, object], http: httpx.Client) -> None:
    admin_url = str(chaos["admin_url"])
    armed = http.put(
        f"{admin_url}/v1/admin/faults",
        json={"mode": "drop_before_upstream", "remaining": 1, "path": "/v1/bookings"},
    )
    assert armed.status_code == 200
    assert armed.json()["mode"] == "drop_before_upstream"
    current = http.get(f"{admin_url}/v1/admin/faults")
    assert current.json()["remaining"] == 1
    reset = http.post(f"{admin_url}/v1/admin/reset")
    assert reset.status_code == 200
    cleared = http.get(f"{admin_url}/v1/admin/faults")
    assert cleared.json()["mode"] == "none"
    assert cleared.json()["remaining"] == 0


def test_drop_before_upstream_is_transport_error_and_skips_upstream(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    http.put(
        f"{admin_url}/v1/admin/faults",
        json={
            "mode": "drop_before_upstream",
            "remaining": 1,
            "method": "POST",
            "path": "/v1/bookings",
        },
    )
    with pytest.raises(httpx.HTTPError):
        http.post(
            f"{proxy_url}/v1/bookings",
            headers={"Idempotency-Key": "demo-key-0001"},
            json={"slot_id": "slot-1", "patient_ref": "synth-ada"},
        )
    assert upstream.requests == []  # type: ignore[attr-defined]
    follow = http.post(
        f"{proxy_url}/v1/bookings",
        headers={"Idempotency-Key": "demo-key-0001"},
        json={"slot_id": "slot-1", "patient_ref": "synth-ada"},
    )
    assert follow.status_code == 201
    assert len(upstream.requests) == 1  # type: ignore[attr-defined]


def test_drop_after_upstream_commits_then_fails_client(
    chaos: dict[str, object], http: httpx.Client
) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    upstream = chaos["upstream"]
    http.put(
        f"{admin_url}/v1/admin/faults",
        json={
            "mode": "drop_after_upstream",
            "remaining": 1,
            "method": "POST",
            "path": "/v1/tasks",
        },
    )
    with pytest.raises(httpx.HTTPError):
        http.post(
            f"{proxy_url}/v1/tasks",
            headers={"Idempotency-Key": "demo-task-0001"},
            json={"patient_id": "synth-ada", "title": "synth-chart-review"},
        )
    assert len(upstream.requests) == 1  # type: ignore[attr-defined]
    events = http.get(f"{admin_url}/v1/admin/events").json()["events"]
    types = [event["type"] for event in events]
    assert "upstream_completed" in types
    assert "dropped_after_upstream" in types


def test_delay_exceeds_client_timeout(chaos: dict[str, object]) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    with httpx.Client(timeout=2.0) as admin:
        admin.put(
            f"{admin_url}/v1/admin/faults",
            json={
                "mode": "delay",
                "remaining": 1,
                "delay_ms": 400,
                "method": "POST",
                "path": "/v1/bookings",
            },
        )
    with httpx.Client(timeout=0.1) as client:
        with pytest.raises(httpx.TimeoutException):
            client.post(
                f"{proxy_url}/v1/bookings",
                headers={"Idempotency-Key": "demo-key-0001"},
                json={"slot_id": "slot-1"},
            )


def test_fault_is_bounded_to_matching_request(chaos: dict[str, object], http: httpx.Client) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    http.put(
        f"{admin_url}/v1/admin/faults",
        json={
            "mode": "drop_before_upstream",
            "remaining": 1,
            "method": "POST",
            "path": "/v1/bookings",
            "idempotency_key": "only-this-key",
        },
    )
    other = http.post(
        f"{proxy_url}/v1/bookings",
        headers={"Idempotency-Key": "other-key-0001"},
        json={"slot_id": "slot-1"},
    )
    assert other.status_code == 201
    with pytest.raises(httpx.HTTPError):
        http.post(
            f"{proxy_url}/v1/bookings",
            headers={"Idempotency-Key": "only-this-key"},
            json={"slot_id": "slot-1"},
        )


def test_invalid_fault_rejected(chaos: dict[str, object], http: httpx.Client) -> None:
    admin_url = str(chaos["admin_url"])
    response = http.put(f"{admin_url}/v1/admin/faults", json={"mode": "explode"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_fault"


def test_controller_reset_clears_events() -> None:
    controller = FaultController()
    controller.configure({"mode": "delay", "remaining": 2, "delay_ms": 10})
    controller.consume("POST", "/v1/bookings", None)
    assert controller.events()
    controller.reset()
    assert controller.snapshot().mode == "none"
    assert controller.events() == []


def test_source_has_no_simulator_imports() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = ("fake_booking", "fake_pvs", "praxisos", "PraxisOS")
    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path} contains {token}"
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    for token in forbidden:
        assert token not in pyproject

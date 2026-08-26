from __future__ import annotations

import threading
import time

import httpx


def test_in_flight_proxy_request_cannot_record_into_new_epoch(
    chaos: dict[str, object],
) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    with httpx.Client(timeout=2.0) as admin:
        armed = admin.put(
            f"{admin_url}/v1/admin/faults",
            json={
                "mode": "delay",
                "remaining": 1,
                "delay_ms": 250,
                "method": "POST",
                "path": "/v1/bookings",
            },
        )
        assert armed.status_code == 200

    late: dict[str, object] = {}

    def _create() -> None:
        with httpx.Client(timeout=5.0) as client:
            response = client.post(
                f"{proxy_url}/v1/bookings",
                headers={"Idempotency-Key": "reset-race-proxy"},
                json={"slot_id": "slot-1", "patient_ref": "synth-ada"},
            )
            late["status"] = response.status_code

    worker = threading.Thread(target=_create)
    worker.start()
    deadline = time.time() + 2
    while time.time() < deadline:
        with httpx.Client(timeout=2.0) as admin:
            events = admin.get(f"{admin_url}/v1/admin/events").json()["events"]
        types = [event["type"] for event in events]
        if "request_received" in types or "upstream_completed" in types:
            break
        time.sleep(0.01)

    with httpx.Client(timeout=2.0) as admin:
        reset = admin.post(f"{admin_url}/v1/admin/reset")
        assert reset.status_code == 200
        assert reset.json()["status"] == "reset"
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert late["status"] == 201
    with httpx.Client(timeout=2.0) as admin:
        events = admin.get(f"{admin_url}/v1/admin/events").json()["events"]
    assert events == []
    with httpx.Client(timeout=2.0) as admin:
        follow = admin.get(f"{admin_url}/v1/admin/faults")
    assert follow.json()["mode"] == "none"
    assert follow.json()["remaining"] == 0

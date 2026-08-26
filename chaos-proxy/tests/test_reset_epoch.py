from __future__ import annotations

import threading
import time

import httpx


def test_in_flight_upstream_request_cannot_cross_reset_barrier(
    chaos: dict[str, object],
    http: httpx.Client,
) -> None:
    admin_url = str(chaos["admin_url"])
    proxy_url = str(chaos["proxy_url"])
    controller = chaos["controller"]
    upstream = chaos["upstream"]
    entered = threading.Event()
    hold = threading.Event()
    upstream.entered = entered  # type: ignore[attr-defined]
    upstream.hold = hold  # type: ignore[attr-defined]
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
    assert entered.wait(timeout=2)
    assert not hold.is_set()

    armed = http.put(
        f"{admin_url}/v1/admin/faults",
        json={
            "mode": "drop_before_upstream",
            "remaining": 1,
            "method": "POST",
            "path": "/v1/tasks",
        },
    )
    assert armed.status_code == 200
    reset_done = threading.Event()

    def _reset() -> None:
        response = http.post(f"{admin_url}/v1/admin/reset")
        late["reset_status"] = response.status_code
        late["reset_body"] = response.json()
        reset_done.set()

    reset_worker = threading.Thread(target=_reset)
    reset_worker.start()
    time.sleep(0.1)
    assert not reset_done.is_set()
    hold.set()
    worker.join(timeout=5)
    reset_worker.join(timeout=5)
    assert not worker.is_alive()
    assert not reset_worker.is_alive()
    assert late["status"] == 201
    assert late["reset_status"] == 200
    assert late["reset_body"]["status"] == "reset"
    events = http.get(f"{admin_url}/v1/admin/events").json()["events"]
    assert events == []
    fault = http.get(f"{admin_url}/v1/admin/faults").json()
    assert fault["mode"] == "none"
    assert fault["remaining"] == 0
    follow = http.post(
        f"{proxy_url}/v1/tasks",
        headers={"Idempotency-Key": "post-reset-proxy"},
        json={"patient_id": "synth-ada", "title": "synth-task"},
    )
    assert follow.status_code == 201
    types = [event["type"] for event in controller.events()]
    assert "dropped_before_upstream" not in types
    assert "fault_consumed" not in types

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from fake_booking.app import app, store
from fake_booking.settings import Settings


def _first_slot(client: TestClient) -> dict:
    response = client.get("/v1/slots")
    assert response.status_code == 200
    return response.json()["slots"][0]


def test_delayed_create_cannot_mutate_after_reset() -> None:
    store.settings = Settings(seed="obj-001", state_path=None)
    store.reset()
    with TestClient(app) as client:
        slot = _first_slot(client)
        armed = client.put(
            "/v1/admin/faults",
            json={"mode": "delay", "delay_ms": 250, "remaining": 1},
        )
        assert armed.status_code == 200
        late: dict[str, object] = {}

        def _create() -> None:
            response = client.post(
                "/v1/bookings",
                headers={"Idempotency-Key": "reset-race-booking"},
                json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
            )
            late["status"] = response.status_code
            late["body"] = response.json()

        worker = threading.Thread(target=_create)
        worker.start()
        deadline = time.time() + 2
        while time.time() < deadline:
            events = client.get("/v1/admin/events").json()["events"]
            if any(event["type"] == "booking_requested" for event in events):
                break
            time.sleep(0.01)
        reset = client.post("/v1/admin/reset")
        assert reset.status_code == 200
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert late["status"] == 409
        assert late["body"]["error"] == "epoch_stale"
        events = client.get("/v1/admin/events").json()["events"]
        assert events == []
        remaining = {item["id"] for item in client.get("/v1/slots").json()["slots"]}
        assert slot["id"] in remaining
        created = client.post(
            "/v1/bookings",
            headers={"Idempotency-Key": "post-reset-booking"},
            json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
        )
        assert created.status_code == 201
        types = [event["type"] for event in client.get("/v1/admin/events").json()["events"]]
        assert "booking_requested" in types
        assert "booking_committed" in types
        traces = {event["trace_id"] for event in client.get("/v1/admin/events").json()["events"]}
        assert traces == {f"tr_{store.epoch():06d}_000001"}


def test_pre_reset_trace_id_is_never_reused() -> None:
    store.settings = Settings(seed="obj-001", state_path=None)
    store.reset()
    with TestClient(app) as client:
        slot = _first_slot(client)
        first = client.post(
            "/v1/bookings",
            headers={"Idempotency-Key": "pre-reset-trace-booking"},
            json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
        )
        assert first.status_code == 201
        before = {event["trace_id"] for event in client.get("/v1/admin/events").json()["events"]}
        assert before
        assert all(trace.startswith("tr_") and trace.count("_") == 2 for trace in before)
        reset = client.post("/v1/admin/reset")
        assert reset.status_code == 200
        created = client.post(
            "/v1/bookings",
            headers={"Idempotency-Key": "post-reset-trace-booking"},
            json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
        )
        assert created.status_code == 201
        after = {event["trace_id"] for event in client.get("/v1/admin/events").json()["events"]}
        assert after
        assert before.isdisjoint(after)

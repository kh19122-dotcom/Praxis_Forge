from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from fake_pvs.app import app, store
from fake_pvs.settings import Settings


def test_delayed_create_cannot_mutate_after_reset() -> None:
    store.settings = Settings(seed="obj-002", state_path=None)
    store.reset()
    with TestClient(app) as client:
        armed = client.put(
            "/v1/admin/faults",
            json={"mode": "delay", "delay_ms": 250, "remaining": 1},
        )
        assert armed.status_code == 200
        late: dict[str, object] = {}

        def _create() -> None:
            response = client.post(
                "/v1/tasks",
                headers={"Idempotency-Key": "reset-race-pvs"},
                json={
                    "patient_id": "synth-ada",
                    "title": "synth-task",
                    "priority": "normal",
                },
            )
            late["status"] = response.status_code
            late["body"] = response.json()

        worker = threading.Thread(target=_create)
        worker.start()
        deadline = time.time() + 2
        while time.time() < deadline:
            events = client.get("/v1/admin/events").json()["events"]
            if any(event["type"] == "task_requested" for event in events):
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
        created = client.post(
            "/v1/tasks",
            headers={"Idempotency-Key": "post-reset-pvs"},
            json={
                "patient_id": "synth-ada",
                "title": "synth-task",
                "priority": "normal",
            },
        )
        assert created.status_code == 201
        types = [event["type"] for event in client.get("/v1/admin/events").json()["events"]]
        assert "task_requested" in types
        assert "task_committed" in types
        traces = {event["trace_id"] for event in client.get("/v1/admin/events").json()["events"]}
        assert traces == {"tr_000001"}

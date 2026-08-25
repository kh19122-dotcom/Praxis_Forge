from __future__ import annotations

from fastapi.testclient import TestClient

from fake_booking.catalog import generate_slots
from fake_booking.ids import slot_id
from fake_booking.settings import Settings
from fake_booking.store import Store


def _first_slot(client: TestClient) -> dict:
    response = client.get("/v1/slots")
    assert response.status_code == 200
    slots = response.json()["slots"]
    assert slots
    return slots[0]


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "fake-booking", "seed": "obj-001"}


def test_openapi_is_inspectable(client: TestClient) -> None:
    generated = client.get("/openapi.json")
    assert generated.status_code == 200
    spec = generated.json()
    assert spec["info"]["title"] == "Praxis Forge Fake Booking"
    assert "/v1/bookings" in spec["paths"]
    yaml_spec = client.get("/openapi.yaml")
    assert yaml_spec.status_code == 200
    assert "Praxis Forge Fake Booking" in yaml_spec.text


def test_fixed_seed_produces_repeatable_slots() -> None:
    first = generate_slots(Settings(seed="obj-001"))
    second = generate_slots(Settings(seed="obj-001"))
    other = generate_slots(Settings(seed="obj-002"))
    assert list(first) == list(second)
    assert list(first) != list(other)
    expected_id = slot_id("obj-001", "res-alpha", "2030-01-06T09:00:00Z")
    assert first[expected_id]["start"] == "2030-01-06T09:00:00Z"
    assert first[expected_id]["resource_id"] == "res-alpha"


def test_happy_path_create_and_read(client: TestClient) -> None:
    slot = _first_slot(client)
    created = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "happy-path-key-01"},
        json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
    )
    assert created.status_code == 201
    booking = created.json()
    assert booking["status"] == "confirmed"
    assert booking["slot_id"] == slot["id"]
    assert booking["patient_ref"] == "synth-ada"
    assert booking["id"].startswith("bkg_")

    fetched = client.get(f"/v1/bookings/{booking['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == booking["id"]

    remaining = client.get("/v1/slots")
    ids = {item["id"] for item in remaining.json()["slots"]}
    assert slot["id"] not in ids


def test_conflict_is_distinguishable_from_infrastructure_failure(client: TestClient) -> None:
    slot = _first_slot(client)
    first = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "conflict-key-aaaa"},
        json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
    )
    assert first.status_code == 201
    second = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "conflict-key-bbbb"},
        json={"slot_id": slot["id"], "patient_ref": "synth-ben"},
    )
    assert second.status_code == 409
    body = second.json()
    assert body["error"] == "slot_conflict"
    assert body["details"]["existing_booking_id"] == first.json()["id"]
    assert body["details"]["committed"] is False
    assert body["trace_id"].startswith("tr_")

    failure = client.put("/v1/admin/faults", json={"mode": "fail_before_commit", "remaining": 1})
    assert failure.status_code == 200
    other_slot = client.get("/v1/slots").json()["slots"][0]
    infra = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "conflict-key-cccc"},
        json={"slot_id": other_slot["id"], "patient_ref": "synth-cal"},
    )
    assert infra.status_code == 503
    assert infra.json()["error"] == "fail_before_commit"
    assert infra.json()["error"] != "slot_conflict"


def test_idempotent_retry_does_not_create_second_booking(client: TestClient) -> None:
    slot = _first_slot(client)
    payload = {"slot_id": slot["id"], "patient_ref": "synth-ada"}
    headers = {"Idempotency-Key": "same-key-0001"}
    first = client.post("/v1/bookings", headers=headers, json=payload)
    second = client.post("/v1/bookings", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    events = client.get("/v1/admin/events").json()["events"]
    assert len([event for event in events if event["type"] == "booking_committed"]) == 1
    assert len([event for event in events if event["type"] == "booking_replayed"]) == 1


def test_idempotency_key_reuse_with_different_body_is_conflict(client: TestClient) -> None:
    slots = client.get("/v1/slots").json()["slots"]
    first = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "reuse-key-0001"},
        json={"slot_id": slots[0]["id"], "patient_ref": "synth-ada"},
    )
    assert first.status_code == 201
    reused = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "reuse-key-0001"},
        json={"slot_id": slots[1]["id"], "patient_ref": "synth-ben"},
    )
    assert reused.status_code == 409
    assert reused.json()["error"] == "idempotency_conflict"
    remaining = {item["id"] for item in client.get("/v1/slots").json()["slots"]}
    assert slots[1]["id"] in remaining
    assert client.get(f"/v1/bookings/{first.json()['id']}").status_code == 200


def test_fail_before_commit_leaves_slot_available(client: TestClient) -> None:
    slot = _first_slot(client)
    configured = client.put("/v1/admin/faults", json={"mode": "fail_before_commit", "remaining": 1})
    assert configured.json()["mode"] == "fail_before_commit"
    failed = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "fail-before-0001"},
        json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
    )
    assert failed.status_code == 503
    body = failed.json()
    assert body["error"] == "fail_before_commit"
    assert body["details"]["committed"] is False

    events = client.get("/v1/admin/events", params={"trace_id": body["trace_id"]}).json()["events"]
    types = [event["type"] for event in events]
    assert "commit_skipped" in types
    assert "booking_committed" not in types

    available = client.get("/v1/slots").json()["slots"]
    assert any(item["id"] == slot["id"] for item in available)

    retry = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "fail-before-0002"},
        json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
    )
    assert retry.status_code == 201


def test_ambiguous_remote_effect_is_recoverable_from_forge_evidence(client: TestClient) -> None:
    slot = _first_slot(client)
    client.put("/v1/admin/faults", json={"mode": "ambiguous", "delay_ms": 5, "remaining": 1})
    ambiguous = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "ambiguous-key-01"},
        json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
    )
    assert ambiguous.status_code == 504
    body = ambiguous.json()
    assert body["error"] == "ambiguous_outcome"
    assert body["details"]["committed"] is None
    trace_id = body["trace_id"]

    events = client.get("/v1/admin/events", params={"trace_id": trace_id}).json()["events"]
    types = [event["type"] for event in events]
    assert "booking_committed" in types
    assert "response_suppressed" in types
    committed = next(event for event in events if event["type"] == "booking_committed")
    booking_id = committed["details"]["booking_id"]

    evidence = client.get(f"/v1/bookings/{booking_id}")
    assert evidence.status_code == 200
    assert evidence.json()["slot_id"] == slot["id"]
    remaining = client.get("/v1/slots").json()["slots"]
    assert slot["id"] not in {item["id"] for item in remaining}

    replay = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "ambiguous-key-01"},
        json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == booking_id


def test_delayed_response_still_commits(client: TestClient) -> None:
    slot = _first_slot(client)
    client.put("/v1/admin/faults", json={"mode": "delay", "delay_ms": 20, "remaining": 1})
    created = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "delay-key-0001"},
        json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
    )
    assert created.status_code == 201
    events = client.get("/v1/admin/events").json()["events"]
    assert any(event["type"] == "response_delayed" for event in events)
    assert any(event["type"] == "booking_committed" for event in events)


def test_reset_restores_catalog(client: TestClient) -> None:
    slot = _first_slot(client)
    client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "reset-key-0001"},
        json={"slot_id": slot["id"], "patient_ref": "synth-ada"},
    )
    reset = client.post("/v1/admin/reset")
    assert reset.status_code == 200
    assert reset.json()["seed"] == "obj-001"
    restored = client.get("/v1/slots").json()["slots"]
    assert any(item["id"] == slot["id"] for item in restored)
    assert client.get("/v1/admin/events").json()["events"] == []


def test_rejects_non_synthetic_patient_ref(client: TestClient) -> None:
    slot = _first_slot(client)
    response = client.post(
        "/v1/bookings",
        headers={"Idempotency-Key": "real-looking-name"},
        json={"slot_id": slot["id"], "patient_ref": "jane-doe"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_store_reset_is_deterministic() -> None:
    a = Store(Settings(seed="obj-001"))
    b = Store(Settings(seed="obj-001"))
    assert list(a.slots) == list(b.slots)
    assert len(a.slots) == 2 * 5 * 5

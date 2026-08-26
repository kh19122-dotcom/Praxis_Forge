from __future__ import annotations

from fastapi.testclient import TestClient

from fake_pvs.corpus import generate_encounters, generate_patients
from fake_pvs.ids import encounter_id
from fake_pvs.settings import Settings
from fake_pvs.store import Store


def _first_patient(client: TestClient) -> dict:
    response = client.get("/v1/patients")
    assert response.status_code == 200
    patients = response.json()["patients"]
    assert patients
    return patients[0]


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "fake-pvs", "seed": "obj-002"}


def test_openapi_is_inspectable(client: TestClient) -> None:
    generated = client.get("/openapi.json")
    assert generated.status_code == 200
    spec = generated.json()
    assert spec["info"]["title"] == "Praxis Forge Fake PVS"
    assert "/v1/patients" in spec["paths"]
    assert "/v1/tasks" in spec["paths"]
    yaml_spec = client.get("/openapi.yaml")
    assert yaml_spec.status_code == 200
    assert "Praxis Forge Fake PVS" in yaml_spec.text


def test_fixed_seed_produces_repeatable_patients_and_encounters() -> None:
    first_patients = generate_patients(Settings(seed="obj-002"))
    second_patients = generate_patients(Settings(seed="obj-002"))
    first_encounters = generate_encounters(Settings(seed="obj-002"))
    second_encounters = generate_encounters(Settings(seed="obj-002"))
    other_encounters = generate_encounters(Settings(seed="obj-003"))
    assert list(first_patients) == list(second_patients)
    assert list(first_encounters) == list(second_encounters)
    assert list(first_encounters) != list(other_encounters)
    expected_id = encounter_id("obj-002", "synth-ada", "2030-01-06T09:00:00Z", "0")
    assert first_encounters[expected_id]["patient_id"] == "synth-ada"
    assert first_encounters[expected_id]["occurred_at"] == "2030-01-06T09:00:00Z"
    assert first_encounters[expected_id]["kind"] == "intake"


def test_patient_records_use_synthetic_identifier_convention(client: TestClient) -> None:
    patients = client.get("/v1/patients").json()["patients"]
    assert patients
    for patient in patients:
        assert patient["id"].startswith("synth-")
        assert patient["cohort"].startswith("cohort-")
        assert patient["site"].startswith("site-")
    encounters = client.get("/v1/patients/synth-ada/encounters").json()["encounters"]
    assert encounters
    for encounter in encounters:
        assert encounter["patient_id"].startswith("synth-")
        assert encounter["summary"].startswith("synth-")


def test_search_patients_by_simulator_fields(client: TestClient) -> None:
    alpha = client.get("/v1/patients", params={"cohort": "cohort-alpha"})
    assert alpha.status_code == 200
    assert {item["id"] for item in alpha.json()["patients"]} == {"synth-ada", "synth-ben"}

    south = client.get("/v1/patients", params={"site": "site-south"})
    assert south.status_code == 200
    assert {item["id"] for item in south.json()["patients"]} == {"synth-deb", "synth-eli"}

    one = client.get("/v1/patients", params={"id": "synth-cal"})
    assert one.status_code == 200
    assert [item["id"] for item in one.json()["patients"]] == ["synth-cal"]


def test_read_patient_and_encounters(client: TestClient) -> None:
    fetched = client.get("/v1/patients/synth-ada")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == "synth-ada"
    listed = client.get("/v1/patients/synth-ada/encounters")
    assert listed.status_code == 200
    body = listed.json()
    assert body["patient_id"] == "synth-ada"
    assert len(body["encounters"]) == 3
    encounter = client.get(f"/v1/encounters/{body['encounters'][0]['id']}")
    assert encounter.status_code == 200
    assert encounter.json()["patient_id"] == "synth-ada"


def test_missing_patient_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/patients/synth-missing")
    assert response.status_code == 404
    assert response.json()["error"] == "patient_not_found"
    listed = client.get("/v1/patients/synth-missing/encounters")
    assert listed.status_code == 404
    assert listed.json()["error"] == "patient_not_found"


def test_rejects_non_synthetic_patient_identifier(client: TestClient) -> None:
    by_path = client.get("/v1/patients/jane-doe")
    assert by_path.status_code == 422
    assert by_path.json()["error"] == "validation_error"
    by_query = client.get("/v1/patients", params={"id": "jane-doe"})
    assert by_query.status_code == 422
    assert by_query.json()["error"] == "validation_error"


def test_happy_path_create_and_read_task(client: TestClient) -> None:
    patient = _first_patient(client)
    created = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "happy-path-key-01"},
        json={"patient_id": patient["id"], "title": "synth-chart-review", "priority": "normal"},
    )
    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "open"
    assert task["patient_id"] == patient["id"]
    assert task["title"] == "synth-chart-review"
    assert task["id"].startswith("tsk_")

    fetched = client.get(f"/v1/tasks/{task['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == task["id"]


def test_conflict_is_distinguishable_from_infrastructure_failure(client: TestClient) -> None:
    first = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "conflict-key-aaaa"},
        json={"patient_id": "synth-ada", "title": "synth-note-one", "priority": "low"},
    )
    assert first.status_code == 201
    reused = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "conflict-key-aaaa"},
        json={"patient_id": "synth-ben", "title": "synth-note-two", "priority": "high"},
    )
    assert reused.status_code == 409
    body = reused.json()
    assert body["error"] == "idempotency_conflict"
    assert body["details"]["task_id"] == first.json()["id"]
    assert body["details"]["committed"] is False
    assert body["trace_id"].startswith("tr_")

    failure = client.put("/v1/admin/faults", json={"mode": "fail_before_commit", "remaining": 1})
    assert failure.status_code == 200
    infra = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "conflict-key-cccc"},
        json={"patient_id": "synth-cal", "title": "synth-note-three", "priority": "normal"},
    )
    assert infra.status_code == 503
    assert infra.json()["error"] == "fail_before_commit"
    assert infra.json()["error"] != "idempotency_conflict"


def test_idempotent_retry_does_not_create_second_task(client: TestClient) -> None:
    payload = {"patient_id": "synth-ada", "title": "synth-follow-up", "priority": "normal"}
    headers = {"Idempotency-Key": "same-key-0001"}
    first = client.post("/v1/tasks", headers=headers, json=payload)
    second = client.post("/v1/tasks", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    events = client.get("/v1/admin/events").json()["events"]
    assert len([event for event in events if event["type"] == "task_committed"]) == 1
    assert len([event for event in events if event["type"] == "task_replayed"]) == 1


def test_idempotency_key_reuse_with_different_body_is_conflict(client: TestClient) -> None:
    first = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "reuse-key-0001"},
        json={"patient_id": "synth-ada", "title": "synth-task-a", "priority": "low"},
    )
    assert first.status_code == 201
    reused = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "reuse-key-0001"},
        json={"patient_id": "synth-ben", "title": "synth-task-b", "priority": "high"},
    )
    assert reused.status_code == 409
    assert reused.json()["error"] == "idempotency_conflict"
    assert client.get(f"/v1/tasks/{first.json()['id']}").status_code == 200
    assert client.get("/v1/tasks").status_code == 405


def test_unknown_patient_task_create_is_not_found(client: TestClient) -> None:
    response = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "missing-patient-01"},
        json={"patient_id": "synth-missing", "title": "synth-task", "priority": "normal"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "patient_not_found"


def test_rejects_non_synthetic_task_fields(client: TestClient) -> None:
    response = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "real-looking-name"},
        json={"patient_id": "jane-doe", "title": "Call patient", "priority": "normal"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_fail_before_commit_does_not_create_task(client: TestClient) -> None:
    configured = client.put("/v1/admin/faults", json={"mode": "fail_before_commit", "remaining": 1})
    assert configured.json()["mode"] == "fail_before_commit"
    failed = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "fail-before-0001"},
        json={"patient_id": "synth-ada", "title": "synth-task", "priority": "normal"},
    )
    assert failed.status_code == 503
    body = failed.json()
    assert body["error"] == "fail_before_commit"
    assert body["details"]["committed"] is False

    events = client.get("/v1/admin/events", params={"trace_id": body["trace_id"]}).json()["events"]
    types = [event["type"] for event in events]
    assert "commit_skipped" in types
    assert "task_committed" not in types

    retry = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "fail-before-0002"},
        json={"patient_id": "synth-ada", "title": "synth-task", "priority": "normal"},
    )
    assert retry.status_code == 201
    assert retry.json()["id"].startswith("tsk_")


def test_ambiguous_remote_effect_is_recoverable_from_forge_evidence(client: TestClient) -> None:
    client.put("/v1/admin/faults", json={"mode": "ambiguous", "delay_ms": 5, "remaining": 1})
    ambiguous = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "ambiguous-key-01"},
        json={"patient_id": "synth-ada", "title": "synth-task", "priority": "normal"},
    )
    assert ambiguous.status_code == 504
    body = ambiguous.json()
    assert body["error"] == "ambiguous_outcome"
    assert body["details"]["committed"] is None
    trace_id = body["trace_id"]

    events = client.get("/v1/admin/events", params={"trace_id": trace_id}).json()["events"]
    types = [event["type"] for event in events]
    assert "task_committed" in types
    assert "response_suppressed" in types
    committed = next(event for event in events if event["type"] == "task_committed")
    task_id = committed["details"]["task_id"]

    evidence = client.get(f"/v1/tasks/{task_id}")
    assert evidence.status_code == 200
    assert evidence.json()["patient_id"] == "synth-ada"
    assert evidence.json()["title"] == "synth-task"

    replay = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "ambiguous-key-01"},
        json={"patient_id": "synth-ada", "title": "synth-task", "priority": "normal"},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == task_id


def test_delayed_response_still_commits(client: TestClient) -> None:
    client.put("/v1/admin/faults", json={"mode": "delay", "delay_ms": 20, "remaining": 1})
    created = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "delay-key-0001"},
        json={"patient_id": "synth-ada", "title": "synth-task", "priority": "normal"},
    )
    assert created.status_code == 201
    events = client.get("/v1/admin/events").json()["events"]
    assert any(event["type"] == "response_delayed" for event in events)
    assert any(event["type"] == "task_committed" for event in events)


def test_reset_restores_corpus(client: TestClient) -> None:
    created = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "reset-key-0001"},
        json={"patient_id": "synth-ada", "title": "synth-task", "priority": "normal"},
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    reset = client.post("/v1/admin/reset")
    assert reset.status_code == 200
    assert reset.json()["seed"] == "obj-002"
    restored = client.get("/v1/patients").json()["patients"]
    assert any(item["id"] == "synth-ada" for item in restored)
    assert client.get("/v1/admin/events").json()["events"] == []
    assert client.get(f"/v1/tasks/{task_id}").status_code == 404


def test_store_reset_is_deterministic() -> None:
    a = Store(Settings(seed="obj-002"))
    b = Store(Settings(seed="obj-002"))
    assert list(a.patients) == list(b.patients)
    assert list(a.encounters) == list(b.encounters)
    assert len(a.patients) == 6
    assert len(a.encounters) == 6 * 3

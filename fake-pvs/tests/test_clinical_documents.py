from __future__ import annotations

import threading
import unicodedata
from hashlib import sha256

from fastapi.testclient import TestClient

from fake_pvs.app import store
from fake_pvs.ids import document_request_hash

CONTENT_HASH = "a" * 64


def _payload(
    *,
    text: str = "hello",
    patient: str = "visit-ada",
    visit: str = "visit-one",
    document_ref: str = "note-alpha",
    content_hash: str = CONTENT_HASH,
) -> dict[str, str]:
    return {
        "synthetic_patient_id": patient,
        "synthetic_visit_id": visit,
        "document_ref": document_ref,
        "content_hash": content_hash,
        "rendered_text": text,
    }


def _post(client: TestClient, payload: dict, key: str):
    return client.post(
        "/v1/clinical-documents",
        headers={"Idempotency-Key": key},
        json=payload,
    )


def test_openapi_includes_document_routes(client: TestClient) -> None:
    generated = client.get("/openapi.json")
    assert generated.status_code == 200
    spec = generated.json()
    assert "/v1/clinical-documents" in spec["paths"]
    assert "/v1/clinical-documents/{document_id}" in spec["paths"]
    yaml_spec = client.get("/openapi.yaml")
    assert yaml_spec.status_code == 200
    assert "/v1/clinical-documents" in yaml_spec.text


def test_utf8_multibyte_and_normalization_forms_are_exact(client: TestClient) -> None:
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfc != nfd
    first = _post(client, _payload(text=nfc, document_ref="note-nfc"), "doc-nfc-0001")
    second = _post(client, _payload(text=nfd, document_ref="note-nfd"), "doc-nfd-0001")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    fetched_nfc = client.get(f"/v1/clinical-documents/{first.json()['id']}")
    fetched_nfd = client.get(f"/v1/clinical-documents/{second.json()['id']}")
    assert fetched_nfc.status_code == 200
    assert fetched_nfd.status_code == 200
    assert fetched_nfc.json()["rendered_text"] == nfc
    assert fetched_nfd.json()["rendered_text"] == nfd
    assert fetched_nfc.json()["rendered_text"] != fetched_nfd.json()["rendered_text"]
    assert fetched_nfc.json()["rendered_text_sha256"] == sha256(nfc.encode("utf-8")).hexdigest()
    assert fetched_nfd.json()["rendered_text_sha256"] == sha256(nfd.encode("utf-8")).hexdigest()
    assert fetched_nfc.json()["status"] == "committed"
    assert first.json()["id"].startswith("doc_")
    assert not first.json()["id"].startswith("tsk_")


def test_size_boundaries_accept_and_reject(client: TestClient) -> None:
    one = _post(client, _payload(text="x", document_ref="note-one"), "doc-size-0001")
    assert one.status_code == 201
    max_ok = _post(
        client,
        _payload(text="y" * 262144, document_ref="note-max"),
        "doc-size-0002",
    )
    assert max_ok.status_code == 201
    assert len(max_ok.json()["rendered_text"].encode("utf-8")) == 262144

    empty = _post(client, _payload(text="", document_ref="note-empty"), "doc-size-0003")
    assert empty.status_code == 422
    too_large = _post(
        client,
        _payload(text="z" * 262145, document_ref="note-over"),
        "doc-size-0004",
    )
    assert too_large.status_code == 422
    nul = _post(client, _payload(text="a\x00b", document_ref="note-nul"), "doc-size-0005")
    assert nul.status_code == 422
    events = client.get("/v1/admin/events").json()["events"]
    assert len([event for event in events if event["type"] == "document_committed"]) == 2
    requested_keys = {
        event["details"]["idempotency_key"]
        for event in events
        if event["type"] == "document_requested"
    }
    assert requested_keys == {"doc-size-0001", "doc-size-0002"}


def test_invalid_input_does_not_write(client: TestClient) -> None:
    before = client.get("/v1/admin/events").json()["events"]
    non_str = client.post(
        "/v1/clinical-documents",
        headers={"Idempotency-Key": "doc-invalid-01"},
        json=_payload() | {"rendered_text": 12},
    )
    assert non_str.status_code == 422
    extra = client.post(
        "/v1/clinical-documents",
        headers={"Idempotency-Key": "doc-invalid-02"},
        json=_payload() | {"unexpected": "x"},
    )
    assert extra.status_code == 422
    bad_ref = _post(client, _payload(patient="Synth-Ada"), "doc-invalid-03")
    assert bad_ref.status_code == 422
    synth_star = _post(client, _payload(patient="synth-ada"), "doc-invalid-04")
    assert synth_star.status_code == 201
    bad_hash = _post(client, _payload(content_hash="A" * 64), "doc-invalid-05")
    assert bad_hash.status_code == 422
    short_hash = _post(client, _payload(content_hash="abc"), "doc-invalid-06")
    assert short_hash.status_code == 422
    after = client.get("/v1/admin/events").json()["events"]
    committed = [event for event in after if event["type"] == "document_committed"]
    assert len(committed) == 1
    assert before == []


def test_same_key_replay_and_conflict(client: TestClient) -> None:
    payload = _payload(text="same-bytes")
    first = _post(client, payload, "doc-replay-0001")
    second = _post(client, payload, "doc-replay-0001")
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    events = client.get("/v1/admin/events").json()["events"]
    assert len([event for event in events if event["type"] == "document_committed"]) == 1
    assert len([event for event in events if event["type"] == "document_replayed"]) == 1

    conflict = _post(client, _payload(text="other-bytes"), "doc-replay-0001")
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["error"] == "idempotency_conflict"
    assert body["details"]["existing_id"] == first.json()["id"]
    assert body["details"]["committed"] is False
    assert client.get(f"/v1/clinical-documents/{first.json()['id']}").status_code == 200
    assert client.get("/v1/clinical-documents").status_code == 405


def test_logical_duplicate_and_changed_bytes_conflict(client: TestClient) -> None:
    first = _post(client, _payload(text="alpha"), "doc-logical-0001")
    assert first.status_code == 201
    duplicate = _post(client, _payload(text="alpha"), "doc-logical-0002")
    assert duplicate.status_code == 409
    body = duplicate.json()
    assert body["error"] == "logical_duplicate"
    assert body["details"]["existing_id"] == first.json()["id"]
    assert body["details"]["committed"] is False
    changed = _post(client, _payload(text="beta"), "doc-logical-0003")
    assert changed.status_code == 409
    assert changed.json()["error"] == "logical_duplicate"
    assert changed.json()["details"]["existing_id"] == first.json()["id"]
    events = client.get("/v1/admin/events").json()["events"]
    assert len([event for event in events if event["type"] == "document_committed"]) == 1


def test_unknown_id_and_unsupported_verbs(client: TestClient) -> None:
    missing = client.get("/v1/clinical-documents/doc_missing")
    assert missing.status_code == 404
    assert missing.json()["error"] == "document_not_found"
    assert client.put("/v1/clinical-documents/doc_missing", json=_payload()).status_code == 405
    assert client.patch("/v1/clinical-documents/doc_missing", json=_payload()).status_code == 405
    assert client.delete("/v1/clinical-documents/doc_missing").status_code == 405
    assert client.get("/v1/clinical-documents").status_code == 405


def test_task_regression_unchanged(client: TestClient) -> None:
    created = client.post(
        "/v1/tasks",
        headers={"Idempotency-Key": "task-regression-01"},
        json={"patient_id": "synth-ada", "title": "synth-chart-review", "priority": "normal"},
    )
    assert created.status_code == 201
    assert created.json()["id"].startswith("tsk_")
    fetched = client.get(f"/v1/tasks/{created.json()['id']}")
    assert fetched.status_code == 200
    document = _post(client, _payload(), "doc-task-ns-0001")
    assert document.status_code == 201
    assert document.json()["id"].startswith("doc_")
    assert document.json()["id"] != created.json()["id"]


def test_ambiguous_commit_is_not_false_no_commit(client: TestClient) -> None:
    client.put("/v1/admin/faults", json={"mode": "ambiguous", "delay_ms": 5, "remaining": 1})
    ambiguous = _post(client, _payload(text="held"), "doc-ambiguous-01")
    assert ambiguous.status_code == 504
    body = ambiguous.json()
    assert body["error"] == "ambiguous_outcome"
    assert body["details"]["committed"] is None
    events = client.get("/v1/admin/events", params={"trace_id": body["trace_id"]}).json()["events"]
    types = [event["type"] for event in events]
    assert "document_committed" in types
    assert "response_suppressed" in types
    committed = next(event for event in events if event["type"] == "document_committed")
    document_id = committed["details"]["document_id"]
    evidence = client.get(f"/v1/clinical-documents/{document_id}")
    assert evidence.status_code == 200
    assert evidence.json()["rendered_text"] == "held"
    replay = _post(client, _payload(text="held"), "doc-ambiguous-01")
    assert replay.status_code == 200
    assert replay.json()["id"] == document_id
    second_create = [
        event
        for event in client.get("/v1/admin/events").json()["events"]
        if event["type"] == "document_committed"
    ]
    assert len(second_create) == 1


def test_fail_before_commit_does_not_create_document(client: TestClient) -> None:
    client.put("/v1/admin/faults", json={"mode": "fail_before_commit", "remaining": 1})
    failed = _post(client, _payload(), "doc-fail-0001")
    assert failed.status_code == 503
    assert failed.json()["details"]["committed"] is False
    events = client.get("/v1/admin/events", params={"trace_id": failed.json()["trace_id"]}).json()[
        "events"
    ]
    types = [event["type"] for event in events]
    assert "commit_skipped" in types
    assert "document_committed" not in types
    retry = _post(client, _payload(), "doc-fail-0002")
    assert retry.status_code == 201
    assert retry.json()["id"].startswith("doc_")


def test_request_hash_is_length_prefixed(client: TestClient) -> None:
    text = "hash-me"
    created = _post(client, _payload(text=text), "doc-hash-0001")
    assert created.status_code == 201
    digest = sha256(text.encode("utf-8")).hexdigest()
    expected = document_request_hash(
        "visit-ada",
        "visit-one",
        "note-alpha",
        CONTENT_HASH,
        digest,
    )
    record = store.get_document(created.json()["id"])
    assert record is not None
    assert record["request_hash"] == expected
    joined = sha256(
        "|".join(["visit-ada", "visit-one", "note-alpha", CONTENT_HASH, digest]).encode()
    ).hexdigest()
    assert record["request_hash"] != joined


def test_concurrent_overlapping_creates_preserve_uniqueness(client: TestClient) -> None:
    payload = _payload(text="race", document_ref="note-race")
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def _create(key: str) -> None:
        response = _post(client, payload, key)
        with lock:
            results.append((response.status_code, response.json()))

    workers = [
        threading.Thread(target=_create, args=("doc-race-aaaa",)),
        threading.Thread(target=_create, args=("doc-race-bbbb",)),
        threading.Thread(target=_create, args=("doc-race-aaaa",)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    created_ids = {
        item[1]["id"]
        for item in results
        if item[0] in {200, 201}
    }
    assert len(created_ids) == 1
    document_id = next(iter(created_ids))
    assert client.get(f"/v1/clinical-documents/{document_id}").status_code == 200
    events = client.get("/v1/admin/events").json()["events"]
    assert len([event for event in events if event["type"] == "document_committed"]) == 1
    statuses = {item[0] for item in results}
    assert 201 in statuses
    assert 409 in statuses or 200 in statuses
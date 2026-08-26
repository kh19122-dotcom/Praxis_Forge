from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_pvs.models import FaultConfig
from fake_pvs.persist import RestoreError
from fake_pvs.settings import Settings
from fake_pvs.store import Store


def _corrupt(path: str, mutator) -> str:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    mutator(payload)
    text = json.dumps(payload)
    Path(path).write_text(text, encoding="utf-8")
    return text


def test_from_env_state_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FORGE_STATE_PATH", raising=False)
    assert Settings.from_env().state_path is None
    path = str(tmp_path / "pvs.json")
    monkeypatch.setenv("FORGE_STATE_PATH", path)
    assert Settings.from_env().state_path == path
    monkeypatch.setenv("FORGE_STATE_PATH", "   ")
    assert Settings.from_env().state_path is None


def test_ephemeral_store_does_not_write(tmp_path: Path) -> None:
    store = Store(Settings(seed="obj-002"))
    store.create_task("ephemeral-key-01", "synth-ada", "synth-chart-review", "normal")
    assert list(tmp_path.iterdir()) == []
    assert store.settings.state_path is None


def test_durable_task_survives_new_store_instance(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    first = Store(Settings(seed="obj-002", state_path=path))
    created = first.create_task("persist-key-0001", "synth-ada", "synth-chart-review", "normal")
    task_id = created["task"]["id"]
    first.set_fault(FaultConfig(mode="fail_before_commit", remaining=3))
    assert first.fault.mode == "fail_before_commit"

    second = Store(Settings(seed="obj-002", state_path=path))
    restored = second.get_task(task_id)
    assert restored is not None
    assert restored["id"] == task_id
    assert restored["title"] == "synth-chart-review"
    replay = second.create_task("persist-key-0001", "synth-ada", "synth-chart-review", "normal")
    assert replay["kind"] == "replay"
    assert replay["task"]["id"] == task_id
    conflict = second.create_task("persist-key-0001", "synth-ben", "synth-other", "high")
    assert conflict["kind"] == "idempotency_conflict"
    assert second.fault.mode == "none"
    assert second.fault.remaining == 0
    assert any(event["type"] == "task_committed" for event in second.events)


def test_admin_reset_clears_durable_pvs_state(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    first = Store(Settings(seed="obj-002", state_path=path))
    created = first.create_task("reset-key-0001", "synth-ada", "synth-task", "normal")
    task_id = created["task"]["id"]
    first.reset()
    assert first.get_task(task_id) is None
    assert first.events == []
    assert "synth-ada" in first.patients

    second = Store(Settings(seed="obj-002", state_path=path))
    assert second.get_task(task_id) is None
    assert second.events == []
    assert list(second.patients) == list(first.patients)


def test_seed_mismatch_fails_closed_and_preserves_file(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    first = Store(Settings(seed="obj-002", state_path=path))
    created = first.create_task("seed-key-0001", "synth-ada", "synth-task", "normal")
    original = Path(path).read_bytes()
    with pytest.raises(RestoreError, match="seed mismatch"):
        Store(Settings(seed="obj-009", state_path=path))
    assert Path(path).read_bytes() == original
    restored = Store(Settings(seed="obj-002", state_path=path))
    assert restored.get_task(created["task"]["id"]) is not None


def test_truncated_json_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "pvs.json"
    store = Store(Settings(seed="obj-002", state_path=str(path)))
    store.create_task("trunc-key-0001", "synth-ada", "synth-task", "normal")
    original = b'{"schema":"praxis-forge.fake-pvs-state.v1","seed":"obj-002"'
    path.write_bytes(original)
    with pytest.raises(RestoreError, match="truncated|not valid JSON"):
        Store(Settings(seed="obj-002", state_path=str(path)))
    assert path.read_bytes() == original


def test_non_object_json_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "pvs.json"
    original = b"[1, 2, 3]"
    path.write_bytes(original)
    with pytest.raises(RestoreError, match="JSON object"):
        Store(Settings(seed="obj-002", state_path=str(path)))
    assert path.read_bytes() == original


def test_dangling_idempotency_map_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    first = Store(Settings(seed="obj-002", state_path=path))
    first.create_task("map-key-0001", "synth-ada", "synth-task", "normal")
    original = _corrupt(
        path,
        lambda payload: payload["tasks_by_key"].update({"broken-key-0001": "tsk_missing"}),
    )
    with pytest.raises(RestoreError, match="dangling"):
        Store(Settings(seed="obj-002", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == original


def test_malformed_event_counter_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    first = Store(Settings(seed="obj-002", state_path=path))
    first.create_task("event-key-0001", "synth-ada", "synth-task", "normal")
    original = _corrupt(path, lambda payload: payload.update({"seq": 0, "trace": 0}))
    with pytest.raises(RestoreError, match="counters"):
        Store(Settings(seed="obj-002", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == original


def test_unsupported_schema_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    first = Store(Settings(seed="obj-002", state_path=path))
    first.create_task("schema-key-0001", "synth-ada", "synth-task", "normal")
    original = _corrupt(path, lambda payload: payload.update({"schema": "not-a-schema"}))
    with pytest.raises(RestoreError, match="unsupported state schema"):
        Store(Settings(seed="obj-002", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == original


def test_invalid_stored_task_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    first = Store(Settings(seed="obj-002", state_path=path))
    created = first.create_task("body-key-0001", "synth-ada", "synth-task", "normal")
    task_id = created["task"]["id"]

    def mutate(payload: dict) -> None:
        payload["tasks"][task_id]["title"] = "Call patient"

    original = _corrupt(path, mutate)
    with pytest.raises(RestoreError, match="invalid stored task"):
        Store(Settings(seed="obj-002", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == original


def test_absent_state_file_initializes_baseline(tmp_path: Path) -> None:
    path = str(tmp_path / "missing" / "pvs.json")
    store = Store(Settings(seed="obj-002", state_path=path))
    assert store.tasks == {}
    assert Path(path).is_file()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["tasks"] == {}
    assert payload["seed"] == "obj-002"

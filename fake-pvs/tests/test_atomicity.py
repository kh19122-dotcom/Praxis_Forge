from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_pvs.persist import PersistenceCrash
from fake_pvs.settings import Settings
from fake_pvs.store import EpochStale, Store


def test_commit_failpoint_does_not_restore_effect_without_evidence(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    first = Store(Settings(seed="obj-002", state_path=path))
    first.begin_request(
        "task_requested",
        idempotency_key="crash-key-0001",
        patient_id="synth-ada",
        title="synth-task",
        priority="normal",
    )
    first.finish_request(first.epoch())
    before = Path(path).read_text(encoding="utf-8")
    first.arm_failpoint("commit")
    with pytest.raises(PersistenceCrash):
        first.create_task("crash-key-0001", "synth-ada", "synth-task", "normal")
    assert Path(path).read_text(encoding="utf-8") == before

    restored = Store(Settings(seed="obj-002", state_path=path))
    assert restored.tasks == {}
    assert restored.tasks_by_key == {}
    types = [event["type"] for event in restored.events]
    assert "task_committed" not in types
    assert types == ["task_requested"]


def test_successful_commit_is_atomic_with_evidence(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    first = Store(Settings(seed="obj-002", state_path=path))
    created = first.create_task("atomic-key-0001", "synth-ada", "synth-task", "normal")
    task_id = created["task"]["id"]
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert task_id in payload["tasks"]
    assert payload["tasks_by_key"]["atomic-key-0001"] == task_id
    assert any(event["type"] == "task_committed" for event in payload["events"])


def test_stale_epoch_cannot_commit_after_reset(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    store = Store(Settings(seed="obj-002", state_path=path))
    epoch, trace_id = store.begin_request(
        "task_requested",
        idempotency_key="stale-key-0001",
        patient_id="synth-ada",
        title="synth-task",
        priority="normal",
    )
    store.finish_request(epoch)
    store.reset()
    with pytest.raises(EpochStale):
        store.create_task(
            "stale-key-0001",
            "synth-ada",
            "synth-task",
            "normal",
            epoch=epoch,
            trace_id=trace_id,
        )
    assert store.tasks == {}
    assert store.events == []

from __future__ import annotations

from pathlib import Path

from fake_pvs.models import FaultConfig
from fake_pvs.settings import Settings
from fake_pvs.store import Store


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
    first.record("tr_000001", "task_committed", task_id=task_id, committed=True)
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


def test_seed_mismatch_does_not_restore_pvs_state(tmp_path: Path) -> None:
    path = str(tmp_path / "pvs.json")
    first = Store(Settings(seed="obj-002", state_path=path))
    created = first.create_task("seed-key-0001", "synth-ada", "synth-task", "normal")
    other = Store(Settings(seed="obj-009", state_path=path))
    assert other.get_task(created["task"]["id"]) is None
    assert other.events == []

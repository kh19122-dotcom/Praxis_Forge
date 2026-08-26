from __future__ import annotations

from pathlib import Path

from fake_booking.models import FaultConfig
from fake_booking.settings import Settings
from fake_booking.store import Store


def _first_slot_id(store: Store) -> str:
    return next(iter(store.slots))


def test_from_env_state_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FORGE_STATE_PATH", raising=False)
    assert Settings.from_env().state_path is None
    path = str(tmp_path / "booking.json")
    monkeypatch.setenv("FORGE_STATE_PATH", path)
    assert Settings.from_env().state_path == path
    monkeypatch.setenv("FORGE_STATE_PATH", "   ")
    assert Settings.from_env().state_path is None


def test_ephemeral_store_does_not_write(tmp_path: Path) -> None:
    store = Store(Settings(seed="obj-001"))
    slot_id = _first_slot_id(store)
    store.create_booking("ephemeral-key-01", slot_id, "synth-ada")
    assert list(tmp_path.iterdir()) == []
    assert store.settings.state_path is None


def test_durable_booking_survives_new_store_instance(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    created = first.create_booking("persist-key-0001", slot_id, "synth-ada")
    booking_id = created["booking"]["id"]
    first.record("tr_000001", "booking_committed", booking_id=booking_id, committed=True)
    first.set_fault(FaultConfig(mode="fail_before_commit", remaining=3))
    assert first.fault.mode == "fail_before_commit"

    second = Store(Settings(seed="obj-001", state_path=path))
    restored = second.get_booking(booking_id)
    assert restored is not None
    assert restored["id"] == booking_id
    assert restored["patient_ref"] == "synth-ada"
    replay = second.create_booking("persist-key-0001", slot_id, "synth-ada")
    assert replay["kind"] == "replay"
    assert replay["booking"]["id"] == booking_id
    conflict = second.create_booking("persist-key-0002", slot_id, "synth-ben")
    assert conflict["kind"] == "slot_conflict"
    assert conflict["existing_booking_id"] == booking_id
    assert second.fault.mode == "none"
    assert second.fault.remaining == 0
    assert any(event["type"] == "booking_committed" for event in second.events)


def test_admin_reset_clears_durable_booking_state(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    created = first.create_booking("reset-key-0001", slot_id, "synth-ada")
    booking_id = created["booking"]["id"]
    first.reset()
    assert first.get_booking(booking_id) is None
    assert first.slots[slot_id]["booking_id"] is None
    assert first.events == []

    second = Store(Settings(seed="obj-001", state_path=path))
    assert second.get_booking(booking_id) is None
    assert second.slots[slot_id]["booking_id"] is None
    assert second.events == []
    assert list(second.slots) == list(first.slots)


def test_seed_mismatch_does_not_restore_booking_state(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    created = first.create_booking("seed-key-0001", slot_id, "synth-ada")
    other = Store(Settings(seed="obj-009", state_path=path))
    assert other.get_booking(created["booking"]["id"]) is None
    assert other.events == []

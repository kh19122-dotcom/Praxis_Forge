from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_booking.persist import PersistenceCrash
from fake_booking.settings import Settings
from fake_booking.store import EpochStale, Store


def _first_slot_id(store: Store) -> str:
    return next(iter(store.slots))


def test_commit_failpoint_does_not_restore_effect_without_evidence(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    first.begin_request(
        "booking_requested",
        idempotency_key="crash-key-0001",
        slot_id=slot_id,
        patient_ref="synth-ada",
    )
    first.finish_request(first.epoch())
    before = Path(path).read_text(encoding="utf-8")
    first.arm_failpoint("commit")
    with pytest.raises(PersistenceCrash):
        first.create_booking("crash-key-0001", slot_id, "synth-ada")
    assert Path(path).read_text(encoding="utf-8") == before

    restored = Store(Settings(seed="obj-001", state_path=path))
    assert restored.bookings == {}
    assert restored.bookings_by_key == {}
    assert all(slot["booking_id"] is None for slot in restored.slots.values())
    types = [event["type"] for event in restored.events]
    assert "booking_committed" not in types
    assert types == ["booking_requested"]


def test_successful_commit_is_atomic_with_evidence(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    created = first.create_booking("atomic-key-0001", slot_id, "synth-ada")
    booking_id = created["booking"]["id"]
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert booking_id in payload["bookings"]
    assert payload["bookings_by_key"]["atomic-key-0001"] == booking_id
    assert payload["slot_booking_ids"][slot_id] == booking_id
    assert any(event["type"] == "booking_committed" for event in payload["events"])


def test_stale_epoch_cannot_commit_after_reset(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    store = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(store)
    epoch, trace_id = store.begin_request(
        "booking_requested",
        idempotency_key="stale-key-0001",
        slot_id=slot_id,
        patient_ref="synth-ada",
    )
    store.finish_request(epoch)
    store.reset()
    with pytest.raises(EpochStale):
        store.create_booking(
            "stale-key-0001",
            slot_id,
            "synth-ada",
            epoch=epoch,
            trace_id=trace_id,
        )
    assert store.bookings == {}
    assert store.events == []

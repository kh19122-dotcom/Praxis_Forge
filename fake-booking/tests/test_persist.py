from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_booking.models import FaultConfig
from fake_booking.persist import RestoreError
from fake_booking.settings import Settings
from fake_booking.store import Store


def _first_slot_id(store: Store) -> str:
    return next(iter(store.slots))


def _corrupt(path: str, mutator) -> str:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    mutator(payload)
    text = json.dumps(payload)
    Path(path).write_text(text, encoding="utf-8")
    return text


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


def test_seed_mismatch_fails_closed_and_preserves_file(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    created = first.create_booking("seed-key-0001", slot_id, "synth-ada")
    original = Path(path).read_bytes()
    with pytest.raises(RestoreError, match="seed mismatch"):
        Store(Settings(seed="obj-009", state_path=path))
    assert Path(path).read_bytes() == original
    restored = Store(Settings(seed="obj-001", state_path=path))
    assert restored.get_booking(created["booking"]["id"]) is not None


def test_truncated_json_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "booking.json"
    store = Store(Settings(seed="obj-001", state_path=str(path)))
    store.create_booking("trunc-key-0001", _first_slot_id(store), "synth-ada")
    original = b'{"schema":"praxis-forge.fake-booking-state.v1","seed":"obj-001"'
    path.write_bytes(original)
    with pytest.raises(RestoreError, match="truncated|not valid JSON"):
        Store(Settings(seed="obj-001", state_path=str(path)))
    assert path.read_bytes() == original


def test_non_object_json_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "booking.json"
    original = b"[1, 2, 3]"
    path.write_bytes(original)
    with pytest.raises(RestoreError, match="JSON object"):
        Store(Settings(seed="obj-001", state_path=str(path)))
    assert path.read_bytes() == original


def test_dangling_idempotency_map_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.create_booking("map-key-0001", _first_slot_id(first), "synth-ada")
    original = _corrupt(
        path,
        lambda payload: payload["bookings_by_key"].update({"broken-key-0001": "bkg_missing"}),
    )
    with pytest.raises(RestoreError, match="dangling"):
        Store(Settings(seed="obj-001", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == original


def test_ghost_slot_mapping_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    first.create_booking("ghost-key-0001", slot_id, "synth-ada")
    other_slot = next(sid for sid in first.slots if sid != slot_id)
    original = _corrupt(
        path,
        lambda payload: payload["slot_booking_ids"].update({other_slot: "bkg_missing"}),
    )
    with pytest.raises(RestoreError, match="ghost"):
        Store(Settings(seed="obj-001", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == original


def test_malformed_event_counter_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.create_booking("event-key-0001", _first_slot_id(first), "synth-ada")
    original = _corrupt(path, lambda payload: payload.update({"seq": 0, "trace": 0}))
    with pytest.raises(RestoreError, match="counters"):
        Store(Settings(seed="obj-001", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == original


def test_unsupported_schema_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.create_booking("schema-key-0001", _first_slot_id(first), "synth-ada")
    original = _corrupt(path, lambda payload: payload.update({"schema": "not-a-schema"}))
    with pytest.raises(RestoreError, match="unsupported state schema"):
        Store(Settings(seed="obj-001", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == original


def test_invalid_stored_booking_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    created = first.create_booking("body-key-0001", _first_slot_id(first), "synth-ada")
    booking_id = created["booking"]["id"]

    def mutate(payload: dict) -> None:
        payload["bookings"][booking_id]["patient_ref"] = "jane-doe"

    original = _corrupt(path, mutate)
    with pytest.raises(RestoreError, match="invalid stored booking"):
        Store(Settings(seed="obj-001", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == original


def test_absent_state_file_initializes_baseline(tmp_path: Path) -> None:
    path = str(tmp_path / "missing" / "booking.json")
    store = Store(Settings(seed="obj-001", state_path=path))
    assert store.bookings == {}
    assert Path(path).is_file()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["bookings"] == {}
    assert payload["seed"] == "obj-001"

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_booking.models import FaultConfig
from fake_booking.persist import PersistenceCrash, RestoreError
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


def _committed_event(payload: dict) -> dict:
    return next(event for event in payload["events"] if event["type"] == "booking_committed")


def _assert_restore_fails(path: str, original: str, match: str) -> None:
    with pytest.raises(RestoreError, match=match):
        Store(Settings(seed="obj-001", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == original


def test_trace_epoch_mismatch_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.create_booking("epoch-key-0001", _first_slot_id(first), "synth-ada")

    def mutate(payload: dict) -> None:
        for event in payload["events"]:
            event["trace_id"] = "tr_000002_000001"

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "event trace epoch")


def test_zero_local_trace_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.create_booking("trace-zero-0001", _first_slot_id(first), "synth-ada")

    def mutate(payload: dict) -> None:
        epoch = payload["epoch"]
        for event in payload["events"]:
            event["trace_id"] = f"tr_{epoch:06d}_000000"

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "local trace")


def test_sequence_gap_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.create_booking("seq-gap-000001", _first_slot_id(first), "synth-ada")

    def mutate(payload: dict) -> None:
        payload["seq"] = 3
        for event in payload["events"]:
            if event["seq"] == 1:
                event["seq"] = 3

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "contiguous order")


def test_sequence_out_of_order_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    epoch, trace_id = first.begin_request(
        "booking_requested",
        idempotency_key="seq-order-0001",
        slot_id=slot_id,
        patient_ref="synth-ada",
    )
    first.finish_request(epoch)
    first.create_booking("seq-order-0001", slot_id, "synth-ada", epoch=epoch, trace_id=trace_id)
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload["events"] = list(reversed(payload["events"]))
    original = json.dumps(payload)
    Path(path).write_text(original, encoding="utf-8")
    _assert_restore_fails(path, original, "contiguous order")


def test_duplicate_sequence_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    epoch, trace_id = first.begin_request(
        "booking_requested",
        idempotency_key="seq-dup-000001",
        slot_id=slot_id,
        patient_ref="synth-ada",
    )
    first.finish_request(epoch)
    first.create_booking("seq-dup-000001", slot_id, "synth-ada", epoch=epoch, trace_id=trace_id)

    def mutate(payload: dict) -> None:
        payload["events"][1]["seq"] = payload["events"][0]["seq"]

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "contiguous order")


def test_committed_false_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.create_booking("commit-false-01", _first_slot_id(first), "synth-ada")

    def mutate(payload: dict) -> None:
        _committed_event(payload)["details"]["committed"] = False

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "not marked committed")


def test_wrong_committed_idempotency_key_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.create_booking("commit-key-0001", _first_slot_id(first), "synth-ada")

    def mutate(payload: dict) -> None:
        _committed_event(payload)["details"]["idempotency_key"] = "wrong-key-0001"

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "idempotency key")


def test_wrong_committed_slot_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    other_slot = next(sid for sid in first.slots if sid != slot_id)
    first.create_booking("commit-slot-0001", slot_id, "synth-ada")

    def mutate(payload: dict) -> None:
        _committed_event(payload)["details"]["slot_id"] = other_slot

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "slot")


def test_duplicate_committed_event_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.create_booking("commit-dup-0001", _first_slot_id(first), "synth-ada")

    def mutate(payload: dict) -> None:
        extra = dict(_committed_event(payload))
        extra["seq"] = payload["seq"] + 1
        extra["details"] = dict(extra["details"])
        payload["events"].append(extra)
        payload["seq"] = extra["seq"]

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "duplicate committed evidence")


def test_object_missing_committed_evidence_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.create_booking("missing-ev-0001", _first_slot_id(first), "synth-ada")

    def mutate(payload: dict) -> None:
        payload["events"] = [
            event for event in payload["events"] if event["type"] != "booking_committed"
        ]
        payload["seq"] = len(payload["events"])

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "without matching committed evidence")


def test_committed_evidence_missing_object_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    created = first.create_booking("ghost-ev-00001", _first_slot_id(first), "synth-ada")
    booking_id = created["booking"]["id"]

    def mutate(payload: dict) -> None:
        payload["bookings"].pop(booking_id)
        payload["bookings_by_key"] = {}
        payload["slot_booking_ids"] = {}

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "missing booking")


def test_restore_reset_does_not_reuse_issued_trace(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    first.create_booking("trace-keep-0001", slot_id, "synth-ada")
    issued = {event["trace_id"] for event in first.events}
    restored = Store(Settings(seed="obj-001", state_path=path))
    restored.reset()
    other_slot = next(iter(restored.slots))
    created = restored.create_booking("trace-new-00001", other_slot, "synth-ada")
    assert created["kind"] == "created"
    after = {event["trace_id"] for event in restored.events}
    assert after
    assert issued.isdisjoint(after)


def test_collapsed_two_operation_trace_history_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_ids = list(first.slots)
    first.create_booking("collapse-a-0001", slot_ids[0], "synth-ada")
    first.create_booking("collapse-b-0001", slot_ids[1], "synth-ben")
    traces = [event["trace_id"] for event in first.events]
    assert traces == ["tr_000001_000001", "tr_000001_000002"]

    def mutate(payload: dict) -> None:
        for event in payload["events"]:
            if event["trace_id"] == "tr_000001_000002":
                event["trace_id"] = "tr_000001_000001"
        payload["trace"] = 1

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "collapsed trace history")


def test_valid_multi_event_single_trace_restore(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    epoch, trace_id = first.begin_request(
        "booking_requested",
        idempotency_key="multi-event-0001",
        slot_id=slot_id,
        patient_ref="synth-ada",
    )
    first.record(trace_id, "fault_injected", epoch=epoch, mode="delay")
    first.record(trace_id, "response_delayed", epoch=epoch, delay_ms=5, mode="delay")
    created = first.create_booking(
        "multi-event-0001",
        slot_id,
        "synth-ada",
        epoch=epoch,
        trace_id=trace_id,
    )
    first.record(
        trace_id,
        "response_suppressed",
        epoch=epoch,
        reason="ambiguous_outcome",
        booking_id=created["booking"]["id"],
        committed=True,
    )
    first.finish_request(epoch)
    second = Store(Settings(seed="obj-001", state_path=path))
    restored = second.get_booking(created["booking"]["id"])
    assert restored is not None
    types = [event["type"] for event in second.events]
    assert types == [
        "booking_requested",
        "fault_injected",
        "response_delayed",
        "booking_committed",
        "response_suppressed",
    ]
    assert {event["trace_id"] for event in second.events} == {trace_id}


def test_valid_replay_and_conflict_traces_restore(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_ids = list(first.slots)
    created = first.create_booking("replay-key-0001", slot_ids[0], "synth-ada")
    epoch, replay_trace = first.begin_request(
        "booking_requested",
        idempotency_key="replay-key-0001",
        slot_id=slot_ids[0],
        patient_ref="synth-ada",
    )
    replay = first.create_booking(
        "replay-key-0001",
        slot_ids[0],
        "synth-ada",
        epoch=epoch,
        trace_id=replay_trace,
    )
    first.finish_request(epoch)
    assert replay["kind"] == "replay"
    conflict_epoch, conflict_trace = first.begin_request(
        "booking_requested",
        idempotency_key="replay-key-0001",
        slot_id=slot_ids[1],
        patient_ref="synth-ben",
    )
    conflict = first.create_booking(
        "replay-key-0001",
        slot_ids[1],
        "synth-ben",
        epoch=conflict_epoch,
        trace_id=conflict_trace,
    )
    first.finish_request(conflict_epoch)
    assert conflict["kind"] == "idempotency_conflict"
    second = Store(Settings(seed="obj-001", state_path=path))
    assert second.get_booking(created["booking"]["id"]) is not None
    types = [event["type"] for event in second.events]
    assert "booking_committed" in types
    assert "booking_replayed" in types
    assert "conflict" in types


def test_restore_restart_reset_allocation_never_reuses_trace(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_ids = list(first.slots)
    first.create_booking("alloc-keep-0001", slot_ids[0], "synth-ada")
    first.create_booking("alloc-keep-0002", slot_ids[1], "synth-ben")
    issued = {event["trace_id"] for event in first.events}
    restored = Store(Settings(seed="obj-001", state_path=path))
    restored.configure_fault(FaultConfig(mode="delay", remaining=1))
    allocated = next(
        event["trace_id"] for event in restored.events if event["type"] == "fault_configured"
    )
    issued.add(allocated)
    restarted = Store(Settings(seed="obj-001", state_path=path))
    assert any(
        event["type"] == "fault_configured" and event["trace_id"] == allocated
        for event in restarted.events
    )
    allocated_again_fault = restarted.configure_fault(
        FaultConfig(mode="fail_before_commit", remaining=1)
    )
    allocated_again = next(
        event["trace_id"]
        for event in restarted.events
        if event["type"] == "fault_configured"
        and event["details"]["mode"] == allocated_again_fault.mode
    )
    assert allocated_again not in issued
    issued.add(allocated_again)
    restarted.reset()
    after_reset_fault = restarted.configure_fault(FaultConfig(mode="delay", remaining=1))
    after_reset = next(
        event["trace_id"] for event in restarted.events if event["type"] == "fault_configured"
    )
    assert after_reset not in issued
    assert after_reset_fault.mode == "delay"
    assert len(issued | {allocated, allocated_again, after_reset}) == len(issued) + 1


def test_same_key_replay_collapse_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    created = first.create_booking("replay-collapse-01", slot_id, "synth-ada")
    epoch, replay_trace = first.begin_request(
        "booking_requested",
        idempotency_key="replay-collapse-01",
        slot_id=slot_id,
        patient_ref="synth-ada",
    )
    replay = first.create_booking(
        "replay-collapse-01",
        slot_id,
        "synth-ada",
        epoch=epoch,
        trace_id=replay_trace,
    )
    first.finish_request(epoch)
    assert replay["kind"] == "replay"
    traces = [event["trace_id"] for event in first.events]
    assert traces == [
        "tr_000001_000001",
        "tr_000001_000002",
        "tr_000001_000002",
    ]
    restored = Store(Settings(seed="obj-001", state_path=path))
    assert restored.get_booking(created["booking"]["id"]) is not None
    assert {event["trace_id"] for event in restored.events} == {
        "tr_000001_000001",
        "tr_000001_000002",
    }

    def mutate(payload: dict) -> None:
        for event in payload["events"]:
            if event["trace_id"] == "tr_000001_000002":
                event["trace_id"] = "tr_000001_000001"
        payload["trace"] = 1

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "collapsed trace history")


def test_fault_config_trace_collapse_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    created = first.create_booking("fault-collapse-01", slot_id, "synth-ada")
    first.configure_fault(FaultConfig(mode="fail_before_commit", remaining=1))
    traces = [event["trace_id"] for event in first.events]
    assert traces == ["tr_000001_000001", "tr_000001_000002"]
    restored = Store(Settings(seed="obj-001", state_path=path))
    assert restored.get_booking(created["booking"]["id"]) is not None
    assert [event["type"] for event in restored.events] == ["booking_committed", "fault_configured"]

    def mutate(payload: dict) -> None:
        for event in payload["events"]:
            if event["trace_id"] == "tr_000001_000002":
                event["trace_id"] = "tr_000001_000001"
        payload["trace"] = 1

    original = _corrupt(path, mutate)
    _assert_restore_fails(path, original, "collapsed trace history")


def test_fault_configure_persist_failpoint_leaves_old_or_complete_state(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    created = first.create_booking("fault-atomic-0001", slot_id, "synth-ada")
    first.configure_fault(
        FaultConfig(mode="delay", delay_ms=17, remaining=2, idempotency_key="synth-old-fault")
    )
    before = Path(path).read_text(encoding="utf-8")
    payload = json.loads(before)
    assert payload["trace"] == 2
    assert payload["fault"]["mode"] == "delay"
    assert payload["fault"]["remaining"] == 2
    first.arm_failpoint("persist")
    with pytest.raises(PersistenceCrash):
        first.configure_fault(FaultConfig(mode="fail_before_commit", remaining=1))
    assert Path(path).read_text(encoding="utf-8") == before

    restored = Store(Settings(seed="obj-001", state_path=path))
    assert restored.get_booking(created["booking"]["id"]) is not None
    assert restored.fault.mode == "delay"
    assert restored.fault.delay_ms == 17
    assert restored.fault.remaining == 2
    assert restored.fault.idempotency_key == "synth-old-fault"
    traces = [
        event["trace_id"]
        for event in restored.events
        if event["type"] == "fault_configured"
    ]
    assert traces == ["tr_000001_000002"]
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["trace"] == 2

    complete = Store(Settings(seed="obj-001", state_path=path))
    complete.configure_fault(FaultConfig(mode="fail_before_commit", remaining=1))
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["trace"] == 3
    assert payload["fault"]["mode"] == "fail_before_commit"
    assert payload["fault"]["remaining"] == 1
    restarted = Store(Settings(seed="obj-001", state_path=path))
    assert restarted.fault.mode == "fail_before_commit"
    assert restarted.fault.remaining == 1
    traces = [
        event["trace_id"]
        for event in restarted.events
        if event["type"] == "fault_configured"
    ]
    assert traces == ["tr_000001_000002", "tr_000001_000003"]


def test_fault_configure_survives_immediate_post_replace_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    first.create_booking("fault-post-replace-01", slot_id, "synth-ada")
    first.configure_fault(
        FaultConfig(mode="delay", delay_ms=17, remaining=2, idempotency_key="synth-keep-fault")
    )
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["trace"] == 2
    assert payload["fault"] == {
        "mode": "delay",
        "delay_ms": 17,
        "remaining": 2,
        "idempotency_key": "synth-keep-fault",
    }
    restarted = Store(Settings(seed="obj-001", state_path=path))
    assert restarted.fault.mode == "delay"
    assert restarted.fault.delay_ms == 17
    assert restarted.fault.remaining == 2
    assert restarted.fault.idempotency_key == "synth-keep-fault"
    traces = [
        event["trace_id"]
        for event in restarted.events
        if event["type"] == "fault_configured"
    ]
    assert traces == ["tr_000001_000002"]
    allocated = restarted.configure_fault(FaultConfig(mode="fail_before_commit", remaining=1))
    assert allocated.mode == "fail_before_commit"
    after = Store(Settings(seed="obj-001", state_path=path))
    assert after.fault.mode == "fail_before_commit"
    assert after.fault.remaining == 1
    traces = [
        event["trace_id"]
        for event in after.events
        if event["type"] == "fault_configured"
    ]
    assert traces == ["tr_000001_000002", "tr_000001_000003"]


def test_fault_configure_memory_failpoint_does_not_advance_durable_trace(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    first.create_booking("fault-memory-0001", slot_id, "synth-ada")
    before = Path(path).read_text(encoding="utf-8")
    first.arm_failpoint("fault_configure")
    with pytest.raises(PersistenceCrash):
        first.configure_fault(FaultConfig(mode="delay", remaining=1))
    assert Path(path).read_text(encoding="utf-8") == before
    restored = Store(Settings(seed="obj-001", state_path=path))
    assert restored.fault.mode == "none"
    assert json.loads(Path(path).read_text(encoding="utf-8"))["trace"] == 1
    assert "fault_configured" not in [event["type"] for event in restored.events]




def test_markerless_trace_highwater_rejected_rolled_equals_valid_history(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    created = first.create_booking("markerless-hw-0001", slot_id, "synth-ada")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    assert payload["trace"] == 1
    assert {event["trace_id"] for event in payload["events"]} == {"tr_000001_000001"}
    original = _corrupt(path, lambda data: data.update({"trace": 2}))
    _assert_restore_fails(path, original, "counters")
    rolled = _corrupt(path, lambda data: data.update({"trace": 1}))
    restored = Store(Settings(seed="obj-001", state_path=path))
    assert Path(path).read_text(encoding="utf-8") == rolled
    assert restored.get_booking(created["booking"]["id"]) is not None
    accepted = {event["trace_id"] for event in restored.events}
    assert accepted == {"tr_000001_000001"}
    assert not hasattr(Store, "next_trace_id")
    other_slot = next(sid for sid in restored.slots if sid != slot_id)
    follow = restored.create_booking("markerless-hw-0002", other_slot, "synth-ben")
    assert follow["kind"] == "created"
    follow_traces = {event["trace_id"] for event in restored.events} - accepted
    assert follow_traces == {"tr_000001_000002"}
    assert "tr_000001_000002" not in accepted


def test_reduced_trace_counter_below_fault_marker_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    first.create_booking("fault-counter-0001", slot_id, "synth-ada")
    first.configure_fault(FaultConfig(mode="fail_before_commit", remaining=1))
    restored = Store(Settings(seed="obj-001", state_path=path))
    assert any(event["type"] == "fault_configured" for event in restored.events)
    original = _corrupt(path, lambda payload: payload.update({"trace": 1}))
    _assert_restore_fails(path, original, "counters")


def test_absent_fault_field_restores_historical_default(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    created = first.create_booking("fault-absent-0001", slot_id, "synth-ada")
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    payload.pop("fault", None)
    original = json.dumps(payload)
    Path(path).write_text(original, encoding="utf-8")
    restored = Store(Settings(seed="obj-001", state_path=path))
    assert restored.get_booking(created["booking"]["id"]) is not None
    assert restored.fault.mode == "none"
    assert restored.fault.remaining == 0
    assert restored.fault.delay_ms == 50
    assert restored.fault.idempotency_key is None
    assert Path(path).read_text(encoding="utf-8") == original
    restored.configure_fault(FaultConfig(mode="none"))
    rewritten = json.loads(Path(path).read_text(encoding="utf-8"))
    assert rewritten["fault"]["mode"] == "none"
    assert rewritten["fault"]["remaining"] == 0


def test_explicit_null_fault_fails_closed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    slot_id = _first_slot_id(first)
    first.create_booking("fault-null-00001", slot_id, "synth-ada")
    original = _corrupt(path, lambda payload: payload.update({"fault": None}))
    _assert_restore_fails(path, original, "invalid stored fault")


def test_one_shot_fault_consumption_survives_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.configure_fault(FaultConfig(mode="delay", delay_ms=17, remaining=1))
    consumed = first.consume_fault("synth-one-shot")
    assert consumed.mode == "delay"
    assert first.fault.mode == "none"
    assert first.fault.remaining == 0
    restored = Store(Settings(seed="obj-001", state_path=path))
    assert restored.fault.mode == "none"
    assert restored.fault.remaining == 0


def test_partial_fault_consumption_survives_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.configure_fault(FaultConfig(mode="delay", delay_ms=17, remaining=2))
    consumed = first.consume_fault("synth-partial")
    assert consumed.remaining == 2
    assert first.fault.remaining == 1
    restored = Store(Settings(seed="obj-001", state_path=path))
    assert restored.fault.mode == "delay"
    assert restored.fault.remaining == 1


def test_idempotency_filtered_fault_is_not_consumed(tmp_path: Path) -> None:
    path = str(tmp_path / "booking.json")
    first = Store(Settings(seed="obj-001", state_path=path))
    first.configure_fault(
        FaultConfig(mode="delay", delay_ms=17, remaining=2, idempotency_key="synth-key-a")
    )
    skipped = first.consume_fault("synth-key-b")
    assert skipped.mode == "none"
    assert first.fault.remaining == 2
    restored = Store(Settings(seed="obj-001", state_path=path))
    assert restored.fault.mode == "delay"
    assert restored.fault.remaining == 2
    assert restored.fault.idempotency_key == "synth-key-a"


#!/usr/bin/env python3
"""Host-side crash-consistency gate for atomic remote truth.

Proves that a failpoint at the Store commit persistence boundary cannot restore
a booking/task without matching committed evidence. This is not a completed
Store-method sequence: the first process crashes inside create_* before the
atomic snapshot write, then a new Store instance restores from the same file.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fake-booking" / "src"))
sys.path.insert(0, str(ROOT / "fake-pvs" / "src"))

from fake_booking.persist import PersistenceCrash as BookingCrash  # noqa: E402
from fake_booking.settings import Settings as BookingSettings  # noqa: E402
from fake_booking.store import Store as BookingStore  # noqa: E402
from fake_pvs.persist import PersistenceCrash as PvsCrash  # noqa: E402
from fake_pvs.settings import Settings as PvsSettings  # noqa: E402
from fake_pvs.store import Store as PvsStore  # noqa: E402


class CheckFailure(RuntimeError):
    pass


def check(label: str) -> None:
    print(f"PASS {label}", flush=True)


def booking_crash_window(tmp: Path) -> None:
    path = str(tmp / "booking.json")
    first = BookingStore(BookingSettings(seed="obj-001", state_path=path))
    slot_id = next(iter(first.slots))
    epoch, _trace = first.begin_request(
        "booking_requested",
        idempotency_key="crash-gate-booking",
        slot_id=slot_id,
        patient_ref="synth-ada",
    )
    first.finish_request(epoch)
    before = Path(path).read_text(encoding="utf-8")
    first.arm_failpoint("commit")
    try:
        first.create_booking("crash-gate-booking", slot_id, "synth-ada")
    except BookingCrash:
        pass
    else:
        raise CheckFailure("booking commit failpoint did not fire")
    if Path(path).read_text(encoding="utf-8") != before:
        raise CheckFailure("booking crash overwrote durable state")
    restored = BookingStore(BookingSettings(seed="obj-001", state_path=path))
    if restored.bookings or restored.bookings_by_key:
        raise CheckFailure("booking crash restored a business effect")
    if any(slot["booking_id"] is not None for slot in restored.slots.values()):
        raise CheckFailure("booking crash restored slot consumption")
    types = [event["type"] for event in restored.events]
    if "booking_committed" in types:
        raise CheckFailure(f"booking crash restored committed evidence: {types}")
    if types != ["booking_requested"]:
        raise CheckFailure(f"unexpected booking events after crash: {types}")
    check("booking_commit_failpoint_leaves_no_effect")


def pvs_crash_window(tmp: Path) -> None:
    path = str(tmp / "pvs.json")
    first = PvsStore(PvsSettings(seed="obj-002", state_path=path))
    epoch, _trace = first.begin_request(
        "task_requested",
        idempotency_key="crash-gate-pvs",
        patient_id="synth-ada",
        title="synth-chart-review",
        priority="normal",
    )
    first.finish_request(epoch)
    before = Path(path).read_text(encoding="utf-8")
    first.arm_failpoint("commit")
    try:
        first.create_task(
            "crash-gate-pvs",
            "synth-ada",
            "synth-chart-review",
            "normal",
        )
    except PvsCrash:
        pass
    else:
        raise CheckFailure("pvs commit failpoint did not fire")
    if Path(path).read_text(encoding="utf-8") != before:
        raise CheckFailure("pvs crash overwrote durable state")
    restored = PvsStore(PvsSettings(seed="obj-002", state_path=path))
    if restored.tasks or restored.tasks_by_key:
        raise CheckFailure("pvs crash restored a business effect")
    types = [event["type"] for event in restored.events]
    if "task_committed" in types:
        raise CheckFailure(f"pvs crash restored committed evidence: {types}")
    if types != ["task_requested"]:
        raise CheckFailure(f"unexpected pvs events after crash: {types}")
    check("pvs_commit_failpoint_leaves_no_effect")


def completed_commit_restores_with_evidence(tmp: Path) -> None:
    booking_path = str(tmp / "booking-ok.json")
    booking = BookingStore(BookingSettings(seed="obj-001", state_path=booking_path))
    slot_id = next(iter(booking.slots))
    created = booking.create_booking("crash-gate-ok", slot_id, "synth-ada")
    restored_booking = BookingStore(BookingSettings(seed="obj-001", state_path=booking_path))
    fetched = restored_booking.get_booking(created["booking"]["id"])
    if fetched is None:
        raise CheckFailure("completed booking did not restore")
    payload = json.loads(Path(booking_path).read_text(encoding="utf-8"))
    if not any(event["type"] == "booking_committed" for event in payload["events"]):
        raise CheckFailure("completed booking missing committed evidence")

    pvs_path = str(tmp / "pvs-ok.json")
    pvs = PvsStore(PvsSettings(seed="obj-002", state_path=pvs_path))
    created_task = pvs.create_task(
        "crash-gate-ok",
        "synth-ada",
        "synth-chart-review",
        "normal",
    )
    restored_pvs = PvsStore(PvsSettings(seed="obj-002", state_path=pvs_path))
    fetched_task = restored_pvs.get_task(created_task["task"]["id"])
    if fetched_task is None:
        raise CheckFailure("completed task did not restore")
    payload = json.loads(Path(pvs_path).read_text(encoding="utf-8"))
    if not any(event["type"] == "task_committed" for event in payload["events"]):
        raise CheckFailure("completed task missing committed evidence")
    check("completed_commit_restores_effect_and_evidence")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="praxis-forge-crash-") as raw:
            tmp = Path(raw)
            booking_crash_window(tmp)
            pvs_crash_window(tmp)
            completed_commit_restores_with_evidence(tmp)
        print("crash-consistency: pass")
        return 0
    except CheckFailure as exc:
        print(f"crash-consistency: fail: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

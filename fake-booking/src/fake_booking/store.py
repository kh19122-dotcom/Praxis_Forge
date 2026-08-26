from __future__ import annotations

import re
from hashlib import sha256
from threading import Condition, Lock
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from fake_booking.catalog import generate_slots
from fake_booking.ids import booking_id
from fake_booking.models import Event, FaultConfig, FaultState
from fake_booking.persist import PersistenceCrash, RestoreError, read_state, write_state
from fake_booking.settings import Settings

STATE_SCHEMA = "praxis-forge.fake-booking-state.v1"
TRACE_RE = re.compile(r"^tr_[0-9]{6,}$")


class EpochStale(Exception):
    def __init__(self, trace_id: str = "tr_000000") -> None:
        self.trace_id = trace_id
        super().__init__(trace_id)


class StoredBooking(BaseModel):
    id: str
    slot_id: str
    resource_id: str
    start: str
    end: str
    patient_ref: str = Field(pattern=r"^synth-[a-z0-9-]+$")
    status: str
    idempotency_key: str = Field(min_length=8, max_length=128)
    request_hash: str = Field(min_length=64, max_length=64)


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = Lock()
        self._cond = Condition(self._lock)
        self._epoch = 0
        self._resetting = False
        self._in_flight: dict[int, int] = {}
        self.slots: dict[str, dict] = {}
        self.bookings: dict[str, dict] = {}
        self.bookings_by_key: dict[str, str] = {}
        self.events: list[dict] = []
        self.fault = FaultState(mode="none", delay_ms=50, remaining=0, idempotency_key=None)
        self._seq = 0
        self._trace = 0
        self._failpoints: list[str] = []
        self.reset(restore=True)

    def arm_failpoint(self, name: str) -> None:
        with self._lock:
            self._failpoints.append(name)

    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def epoch_is_current(self, epoch: int) -> bool:
        with self._lock:
            return epoch == self._epoch

    def reset(self, restore: bool = False) -> None:
        with self._cond:
            self._resetting = True
            stale = self._epoch
            self._epoch += 1
            while self._in_flight.get(stale, 0) > 0:
                self._cond.wait()
            self._reset_locked(restore=restore)
            self._resetting = False
            self._cond.notify_all()

    def _reset_locked(self, *, restore: bool) -> None:
        self.slots = generate_slots(self.settings)
        self.bookings = {}
        self.bookings_by_key = {}
        self.events = []
        self.fault = FaultState(mode="none", delay_ms=50, remaining=0, idempotency_key=None)
        self._seq = 0
        self._trace = 0
        if restore and self._restore_locked():
            return
        self._persist_locked()

    def begin_request(self, event_type: str, **details: object) -> tuple[int, str]:
        with self._cond:
            while self._resetting:
                self._cond.wait()
            epoch = self._epoch
            self._in_flight[epoch] = self._in_flight.get(epoch, 0) + 1
            try:
                self._trace += 1
                trace_id = f"tr_{self._trace:06d}"
                self._append_event_locked(trace_id, event_type, **details)
                self._persist_locked()
            except Exception:
                self._release_locked(epoch)
                raise
            return epoch, trace_id

    def finish_request(self, epoch: int) -> None:
        with self._cond:
            self._release_locked(epoch)

    def _release_locked(self, epoch: int) -> None:
        remaining = self._in_flight.get(epoch, 0)
        if remaining <= 1:
            self._in_flight.pop(epoch, None)
        else:
            self._in_flight[epoch] = remaining - 1
        self._cond.notify_all()

    def next_trace_id(self, *, epoch: int | None = None) -> str:
        with self._lock:
            self._require_epoch_locked(epoch)
            self._trace += 1
            self._persist_locked()
            return f"tr_{self._trace:06d}"

    def record(
        self,
        trace_id: str,
        event_type: str,
        *,
        epoch: int | None = None,
        **details: object,
    ) -> dict:
        with self._lock:
            self._require_epoch_locked(epoch, trace_id)
            event = self._append_event_locked(trace_id, event_type, **details)
            self._persist_locked()
            return event

    def set_fault(self, config: FaultConfig, *, epoch: int | None = None) -> FaultState:
        with self._lock:
            self._require_epoch_locked(epoch)
            remaining = 0 if config.mode == "none" else config.remaining
            self.fault = FaultState(
                mode=config.mode,
                delay_ms=config.delay_ms,
                remaining=remaining,
                idempotency_key=config.idempotency_key,
            )
            return self.fault.model_copy()

    def consume_fault(self, idempotency_key: str, *, epoch: int | None = None) -> FaultState:
        with self._lock:
            self._require_epoch_locked(epoch)
            current = self.fault.model_copy()
            if current.mode == "none" or current.remaining <= 0:
                return FaultState(mode="none", delay_ms=current.delay_ms, remaining=0)
            if current.idempotency_key and current.idempotency_key != idempotency_key:
                return FaultState(
                    mode="none",
                    delay_ms=current.delay_ms,
                    remaining=current.remaining,
                )
            remaining = current.remaining - 1
            self.fault = FaultState(
                mode=current.mode if remaining > 0 else "none",
                delay_ms=current.delay_ms,
                remaining=remaining,
                idempotency_key=current.idempotency_key if remaining > 0 else None,
            )
            return current

    def list_slots(self, resource_id: str | None, available_only: bool) -> list[dict]:
        with self._lock:
            items = list(self.slots.values())
        if resource_id:
            items = [slot for slot in items if slot["resource_id"] == resource_id]
        public = []
        for slot in items:
            available = slot["booking_id"] is None
            if available_only and not available:
                continue
            public.append({**slot, "available": available})
        public.sort(key=lambda slot: (slot["start"], slot["resource_id"]))
        return public

    def get_booking(self, booking_id_value: str) -> dict | None:
        with self._lock:
            booking = self.bookings.get(booking_id_value)
            return dict(booking) if booking else None

    def create_booking(
        self,
        idempotency_key: str,
        slot_id: str,
        patient_ref: str,
        *,
        epoch: int | None = None,
        trace_id: str | None = None,
    ) -> dict:
        request_hash = sha256(f"{slot_id}|{patient_ref}".encode()).hexdigest()
        with self._lock:
            self._require_epoch_locked(epoch, trace_id or "tr_000000")
            if trace_id is None:
                self._trace += 1
                trace_id = f"tr_{self._trace:06d}"

            existing_id = self.bookings_by_key.get(idempotency_key)
            if existing_id:
                existing = self.bookings[existing_id]
                if existing["request_hash"] != request_hash:
                    self._append_event_locked(
                        trace_id,
                        "conflict",
                        reason="idempotency_conflict",
                        booking_id=existing["id"],
                    )
                    self._persist_locked()
                    return {"kind": "idempotency_conflict", "booking": dict(existing)}
                self._append_event_locked(
                    trace_id,
                    "booking_replayed",
                    booking_id=existing["id"],
                    slot_id=existing["slot_id"],
                    idempotency_key=idempotency_key,
                    committed=True,
                )
                self._persist_locked()
                return {"kind": "replay", "booking": dict(existing)}

            slot = self.slots.get(slot_id)
            if slot is None:
                self._append_event_locked(trace_id, "commit_skipped", reason="slot_not_found")
                self._persist_locked()
                return {"kind": "slot_not_found"}
            if slot["booking_id"] is not None:
                self._append_event_locked(
                    trace_id,
                    "conflict",
                    reason="slot_conflict",
                    existing_booking_id=slot["booking_id"],
                )
                self._persist_locked()
                return {
                    "kind": "slot_conflict",
                    "existing_booking_id": slot["booking_id"],
                }

            new_id = booking_id(self.settings.seed, idempotency_key)
            booking = {
                "id": new_id,
                "slot_id": slot_id,
                "resource_id": slot["resource_id"],
                "start": slot["start"],
                "end": slot["end"],
                "patient_ref": patient_ref,
                "status": "confirmed",
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
            }
            slot["booking_id"] = new_id
            self.bookings[new_id] = booking
            self.bookings_by_key[idempotency_key] = new_id
            self._append_event_locked(
                trace_id,
                "booking_committed",
                booking_id=new_id,
                slot_id=slot_id,
                idempotency_key=idempotency_key,
                committed=True,
            )
            self._trip_locked("commit")
            self._persist_locked()
            return {"kind": "created", "booking": dict(booking)}

    def _append_event_locked(self, trace_id: str, event_type: str, **details: object) -> dict:
        self._seq += 1
        event = {
            "seq": self._seq,
            "trace_id": trace_id,
            "type": event_type,
            "details": details,
        }
        self.events.append(event)
        return event

    def _require_epoch_locked(self, epoch: int | None, trace_id: str = "tr_000000") -> None:
        if epoch is not None and epoch != self._epoch:
            raise EpochStale(trace_id)

    def _trip_locked(self, name: str) -> None:
        if name in self._failpoints:
            self._failpoints.remove(name)
            raise PersistenceCrash(name)

    def _persist_locked(self) -> None:
        self._trip_locked("persist")
        path = self.settings.state_path
        if not path:
            return
        write_state(path, self._snapshot_locked())

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "seed": self.settings.seed,
            "seq": self._seq,
            "trace": self._trace,
            "bookings": self.bookings,
            "bookings_by_key": self.bookings_by_key,
            "events": self.events,
            "slot_booking_ids": {
                slot_id: slot["booking_id"]
                for slot_id, slot in self.slots.items()
                if slot["booking_id"] is not None
            },
        }

    def _restore_locked(self) -> bool:
        path = self.settings.state_path
        if not path:
            return False
        payload = read_state(path)
        if payload is None:
            return False
        validated = _validate_booking_snapshot(payload, self.settings, self.slots)
        bookings, bookings_by_key, events, seq, trace, slot_booking_ids = validated
        self.bookings = bookings
        self.bookings_by_key = bookings_by_key
        self.events = events
        self._seq = seq
        self._trace = trace
        for slot in self.slots.values():
            slot["booking_id"] = None
        for booking in self.bookings.values():
            self.slots[booking["slot_id"]]["booking_id"] = booking["id"]
        rebuilt_slots = {
            booking["slot_id"]: booking["id"] for booking in self.bookings.values()
        }
        if rebuilt_slots != slot_booking_ids:
            raise RestoreError("slot_booking_ids does not match restored bookings")
        return True


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_booking_snapshot(
    payload: dict[str, Any],
    settings: Settings,
    slots: dict[str, dict],
) -> tuple[dict[str, dict], dict[str, str], list[dict], int, int, dict[str, str]]:
    if payload.get("schema") != STATE_SCHEMA:
        raise RestoreError("unsupported state schema")
    if payload.get("seed") != settings.seed:
        raise RestoreError("seed mismatch")
    bookings_raw = payload.get("bookings")
    bookings_by_key_raw = payload.get("bookings_by_key")
    events_raw = payload.get("events")
    slot_booking_ids_raw = payload.get("slot_booking_ids")
    seq = payload.get("seq")
    trace = payload.get("trace")
    if not isinstance(bookings_raw, dict) or not isinstance(bookings_by_key_raw, dict):
        raise RestoreError("bookings maps must be objects")
    if not isinstance(events_raw, list) or not isinstance(slot_booking_ids_raw, dict):
        raise RestoreError("events must be a list and slot_booking_ids must be an object")
    if not _is_int(seq) or not _is_int(trace) or seq < 0 or trace < 0:
        raise RestoreError("sequence counters are invalid")

    bookings: dict[str, dict] = {}
    for key, value in bookings_raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise RestoreError("invalid booking record")
        try:
            stored = StoredBooking.model_validate(value)
        except ValidationError as exc:
            raise RestoreError("invalid stored booking") from exc
        if stored.id != key:
            raise RestoreError("booking id does not match map key")
        if stored.status != "confirmed":
            raise RestoreError("invalid booking status")
        slot = slots.get(stored.slot_id)
        if slot is None:
            raise RestoreError("booking references unknown slot")
        if (
            stored.resource_id != slot["resource_id"]
            or stored.start != slot["start"]
            or stored.end != slot["end"]
        ):
            raise RestoreError("booking does not match catalog slot")
        expected_hash = sha256(f"{stored.slot_id}|{stored.patient_ref}".encode()).hexdigest()
        if stored.request_hash != expected_hash:
            raise RestoreError("booking request_hash does not match body")
        if stored.id != booking_id(settings.seed, stored.idempotency_key):
            raise RestoreError("booking id is not seed-derived")
        bookings[key] = stored.model_dump()

    bookings_by_key: dict[str, str] = {}
    for key, value in bookings_by_key_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RestoreError("invalid idempotency map entry")
        target = bookings.get(value)
        if target is None:
            raise RestoreError("dangling bookings_by_key target")
        if target["idempotency_key"] != key:
            raise RestoreError("idempotency map key does not match booking")
        bookings_by_key[key] = value
    rebuilt_keys = {booking["idempotency_key"]: booking["id"] for booking in bookings.values()}
    if rebuilt_keys != bookings_by_key:
        raise RestoreError("idempotency map is inconsistent with bookings")

    slot_booking_ids: dict[str, str] = {}
    for key, value in slot_booking_ids_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RestoreError("invalid slot_booking_ids entry")
        if key not in slots:
            raise RestoreError("slot_booking_ids references unknown slot")
        booking = bookings.get(value)
        if booking is None:
            raise RestoreError("ghost slot consumption")
        if booking["slot_id"] != key:
            raise RestoreError("inconsistent booking-slot mapping")
        if key in slot_booking_ids:
            raise RestoreError("duplicate slot mapping")
        slot_booking_ids[key] = value
    slot_ids = [booking["slot_id"] for booking in bookings.values()]
    if len(slot_ids) != len(set(slot_ids)):
        raise RestoreError("duplicate slot consumption")
    rebuilt_slots = {booking["slot_id"]: booking["id"] for booking in bookings.values()}
    if rebuilt_slots != slot_booking_ids:
        raise RestoreError("slot_booking_ids does not match bookings")

    events = _validate_events(events_raw, seq, trace)
    _validate_commit_evidence(bookings, events)
    return bookings, bookings_by_key, events, seq, trace, slot_booking_ids


def _validate_events(events_raw: list[object], seq: int, trace: int) -> list[dict]:
    events: list[dict] = []
    seen_seq: set[int] = set()
    max_seq = 0
    max_trace = 0
    for item in events_raw:
        if not isinstance(item, dict):
            raise RestoreError("event entry is not an object")
        try:
            event = Event.model_validate(item)
        except ValidationError as exc:
            raise RestoreError("malformed event") from exc
        if event.seq <= 0 or event.seq in seen_seq:
            raise RestoreError("event sequence is invalid")
        if not TRACE_RE.fullmatch(event.trace_id):
            raise RestoreError("event trace_id is invalid")
        if not event.type or not isinstance(event.type, str):
            raise RestoreError("event type is invalid")
        if not isinstance(event.details, dict):
            raise RestoreError("event details must be an object")
        seen_seq.add(event.seq)
        max_seq = max(max_seq, event.seq)
        max_trace = max(max_trace, int(event.trace_id.split("_", 1)[1]))
        events.append(event.model_dump())
    if max_seq > seq or max_trace > trace:
        raise RestoreError("counters do not dominate restored events")
    return events


def _validate_commit_evidence(bookings: dict[str, dict], events: list[dict]) -> None:
    committed: set[str] = set()
    for event in events:
        if event["type"] != "booking_committed":
            continue
        booking_id_value = event["details"].get("booking_id")
        if not isinstance(booking_id_value, str) or booking_id_value not in bookings:
            raise RestoreError("committed evidence references a missing booking")
        committed.add(booking_id_value)
    missing = set(bookings) - committed
    if missing:
        raise RestoreError("booking exists without matching committed evidence")

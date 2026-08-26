from __future__ import annotations

from hashlib import sha256
from threading import Lock
from typing import Any

from fake_booking.catalog import generate_slots
from fake_booking.ids import booking_id
from fake_booking.models import FaultConfig, FaultState
from fake_booking.persist import read_state, write_state
from fake_booking.settings import Settings

STATE_SCHEMA = "praxis-forge.fake-booking-state.v1"


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = Lock()
        self.slots: dict[str, dict] = {}
        self.bookings: dict[str, dict] = {}
        self.bookings_by_key: dict[str, str] = {}
        self.events: list[dict] = []
        self.fault = FaultState(mode="none", delay_ms=50, remaining=0, idempotency_key=None)
        self._seq = 0
        self._trace = 0
        self.reset(restore=True)

    def reset(self, restore: bool = False) -> None:
        with self._lock:
            self._reset_locked(restore=restore)

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

    def next_trace_id(self) -> str:
        with self._lock:
            self._trace += 1
            self._persist_locked()
            return f"tr_{self._trace:06d}"

    def record(self, trace_id: str, event_type: str, **details: object) -> dict:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "trace_id": trace_id,
                "type": event_type,
                "details": details,
            }
            self.events.append(event)
            self._persist_locked()
            return event

    def set_fault(self, config: FaultConfig) -> FaultState:
        with self._lock:
            remaining = 0 if config.mode == "none" else config.remaining
            self.fault = FaultState(
                mode=config.mode,
                delay_ms=config.delay_ms,
                remaining=remaining,
                idempotency_key=config.idempotency_key,
            )
            return self.fault.model_copy()

    def consume_fault(self, idempotency_key: str) -> FaultState:
        with self._lock:
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

    def create_booking(self, idempotency_key: str, slot_id: str, patient_ref: str) -> dict:
        request_hash = sha256(f"{slot_id}|{patient_ref}".encode()).hexdigest()
        with self._lock:
            existing_id = self.bookings_by_key.get(idempotency_key)
            if existing_id:
                existing = self.bookings[existing_id]
                if existing["request_hash"] != request_hash:
                    return {"kind": "idempotency_conflict", "booking": dict(existing)}
                return {"kind": "replay", "booking": dict(existing)}

            slot = self.slots.get(slot_id)
            if slot is None:
                return {"kind": "slot_not_found"}
            if slot["booking_id"] is not None:
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
            self._persist_locked()
            return {"kind": "created", "booking": dict(booking)}

    def _persist_locked(self) -> None:
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
        if payload.get("schema") != STATE_SCHEMA:
            return False
        if payload.get("seed") != self.settings.seed:
            return False
        bookings = payload.get("bookings")
        bookings_by_key = payload.get("bookings_by_key")
        events = payload.get("events")
        slot_booking_ids = payload.get("slot_booking_ids")
        seq = payload.get("seq")
        trace = payload.get("trace")
        if not isinstance(bookings, dict) or not isinstance(bookings_by_key, dict):
            return False
        if not isinstance(events, list) or not isinstance(slot_booking_ids, dict):
            return False
        if not isinstance(seq, int) or not isinstance(trace, int):
            return False
        restored_bookings = {
            str(key): dict(value) for key, value in bookings.items() if isinstance(value, dict)
        }
        restored_keys = {
            str(key): str(value)
            for key, value in bookings_by_key.items()
            if isinstance(value, str)
        }
        self.bookings = restored_bookings
        self.bookings_by_key = restored_keys
        self.events = [dict(event) for event in events if isinstance(event, dict)]
        self._seq = seq
        self._trace = trace
        for slot_id, booking_id_value in slot_booking_ids.items():
            slot = self.slots.get(str(slot_id))
            if slot is None:
                continue
            slot["booking_id"] = None if booking_id_value is None else str(booking_id_value)
        for booking in self.bookings.values():
            slot_id = booking.get("slot_id")
            booking_id_value = booking.get("id")
            if (
                isinstance(slot_id, str)
                and isinstance(booking_id_value, str)
                and slot_id in self.slots
            ):
                self.slots[slot_id]["booking_id"] = booking_id_value
        return True

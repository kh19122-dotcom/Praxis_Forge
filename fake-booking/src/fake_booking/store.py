from __future__ import annotations

from hashlib import sha256
from threading import Lock

from fake_booking.catalog import generate_slots
from fake_booking.ids import booking_id
from fake_booking.models import FaultConfig, FaultState
from fake_booking.settings import Settings


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
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.slots = generate_slots(self.settings)
            self.bookings = {}
            self.bookings_by_key = {}
            self.events = []
            self.fault = FaultState(mode="none", delay_ms=50, remaining=0, idempotency_key=None)
            self._seq = 0
            self._trace = 0

    def next_trace_id(self) -> str:
        with self._lock:
            self._trace += 1
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
            return {"kind": "created", "booking": dict(booking)}

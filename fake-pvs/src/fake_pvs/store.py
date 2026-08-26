from __future__ import annotations

from hashlib import sha256
from threading import Lock
from typing import Any

from fake_pvs.corpus import generate_encounters, generate_patients
from fake_pvs.ids import task_id
from fake_pvs.models import FaultConfig, FaultState
from fake_pvs.persist import read_state, write_state
from fake_pvs.settings import Settings

STATE_SCHEMA = "praxis-forge.fake-pvs-state.v1"


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = Lock()
        self.patients: dict[str, dict] = {}
        self.encounters: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.tasks_by_key: dict[str, str] = {}
        self.events: list[dict] = []
        self.fault = FaultState(mode="none", delay_ms=50, remaining=0, idempotency_key=None)
        self._seq = 0
        self._trace = 0
        self.reset(restore=True)

    def reset(self, restore: bool = False) -> None:
        with self._lock:
            self._reset_locked(restore=restore)

    def _reset_locked(self, *, restore: bool) -> None:
        self.patients = generate_patients(self.settings)
        self.encounters = generate_encounters(self.settings)
        self.tasks = {}
        self.tasks_by_key = {}
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

    def list_patients(
        self,
        cohort: str | None = None,
        site: str | None = None,
        patient_id: str | None = None,
    ) -> list[dict]:
        with self._lock:
            items = list(self.patients.values())
        if cohort:
            items = [patient for patient in items if patient["cohort"] == cohort]
        if site:
            items = [patient for patient in items if patient["site"] == site]
        if patient_id:
            items = [patient for patient in items if patient["id"] == patient_id]
        items.sort(key=lambda patient: patient["id"])
        return [dict(patient) for patient in items]

    def get_patient(self, patient_id: str) -> dict | None:
        with self._lock:
            patient = self.patients.get(patient_id)
            return dict(patient) if patient else None

    def list_encounters(self, patient_id: str) -> list[dict] | None:
        with self._lock:
            if patient_id not in self.patients:
                return None
            items = [
                dict(encounter)
                for encounter in self.encounters.values()
                if encounter["patient_id"] == patient_id
            ]
        items.sort(key=lambda encounter: (encounter["occurred_at"], encounter["id"]))
        return items

    def get_encounter(self, encounter_id_value: str) -> dict | None:
        with self._lock:
            encounter = self.encounters.get(encounter_id_value)
            return dict(encounter) if encounter else None

    def get_task(self, task_id_value: str) -> dict | None:
        with self._lock:
            task = self.tasks.get(task_id_value)
            return dict(task) if task else None

    def create_task(
        self,
        idempotency_key: str,
        patient_id: str,
        title: str,
        priority: str,
    ) -> dict:
        request_hash = sha256(f"{patient_id}|{title}|{priority}".encode()).hexdigest()
        with self._lock:
            existing_id = self.tasks_by_key.get(idempotency_key)
            if existing_id:
                existing = self.tasks[existing_id]
                if existing["request_hash"] != request_hash:
                    return {"kind": "idempotency_conflict", "task": dict(existing)}
                return {"kind": "replay", "task": dict(existing)}

            if patient_id not in self.patients:
                return {"kind": "patient_not_found"}

            new_id = task_id(self.settings.seed, idempotency_key)
            task = {
                "id": new_id,
                "patient_id": patient_id,
                "title": title,
                "priority": priority,
                "status": "open",
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
            }
            self.tasks[new_id] = task
            self.tasks_by_key[idempotency_key] = new_id
            self._persist_locked()
            return {"kind": "created", "task": dict(task)}

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
            "tasks": self.tasks,
            "tasks_by_key": self.tasks_by_key,
            "events": self.events,
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
        tasks = payload.get("tasks")
        tasks_by_key = payload.get("tasks_by_key")
        events = payload.get("events")
        seq = payload.get("seq")
        trace = payload.get("trace")
        if not isinstance(tasks, dict) or not isinstance(tasks_by_key, dict):
            return False
        if not isinstance(events, list):
            return False
        if not isinstance(seq, int) or not isinstance(trace, int):
            return False
        self.tasks = {
            str(key): dict(value)
            for key, value in tasks.items()
            if isinstance(value, dict)
        }
        self.tasks_by_key = {
            str(key): str(value) for key, value in tasks_by_key.items() if isinstance(value, str)
        }
        self.events = [dict(event) for event in events if isinstance(event, dict)]
        self._seq = seq
        self._trace = trace
        return True

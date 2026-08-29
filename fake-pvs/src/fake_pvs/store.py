from __future__ import annotations

import re
from hashlib import sha256
from threading import Condition, Lock
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from fake_pvs.corpus import generate_encounters, generate_patients
from fake_pvs.ids import task_id
from fake_pvs.models import SYNTH_ID_PATTERN, Event, FaultConfig, FaultState
from fake_pvs.persist import PersistenceCrash, RestoreError, read_state, write_state
from fake_pvs.settings import Settings

STATE_SCHEMA = "praxis-forge.fake-pvs-state.v1"
TRACE_RE = re.compile(r"^tr_([0-9]{6,})_([0-9]{6,})$")
TASK_PRIORITIES = frozenset({"low", "normal", "high"})


class EpochStale(Exception):
    def __init__(self, trace_id: str = "tr_000000") -> None:
        self.trace_id = trace_id
        super().__init__(trace_id)


class StoredTask(BaseModel):
    id: str
    patient_id: str = Field(pattern=SYNTH_ID_PATTERN)
    title: str = Field(pattern=SYNTH_ID_PATTERN)
    priority: str
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
        self._pending_resets = 0
        self._reset_generation = 0
        self._in_flight: dict[int, int] = {}
        self.patients: dict[str, dict] = {}
        self.encounters: dict[str, dict] = {}
        self.tasks: dict[str, dict] = {}
        self.tasks_by_key: dict[str, str] = {}
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

    def in_flight_total(self) -> int:
        with self._lock:
            return sum(self._in_flight.values())

    def is_resetting(self) -> bool:
        with self._lock:
            return self._resetting

    def pending_reset_count(self) -> int:
        with self._lock:
            return self._pending_resets

    def reset_generation(self) -> int:
        with self._lock:
            return self._reset_generation

    def admit(self) -> int:
        with self._cond:
            while self._pending_resets > 0:
                self._cond.wait()
            epoch = self._epoch
            self._in_flight[epoch] = self._in_flight.get(epoch, 0) + 1
            return epoch

    def release(self, epoch: int) -> None:
        with self._cond:
            self._release_locked(epoch)

    def reset(self, restore: bool = False) -> None:
        with self._cond:
            self._pending_resets += 1
            owns_reset = False
            try:
                while self._resetting:
                    self._cond.wait()
                owns_reset = True
                self._resetting = True
                self._reset_generation += 1
                stale = self._epoch
                self._epoch += 1
                while self._in_flight.get(stale, 0) > 0:
                    self._cond.wait()
                self._reset_locked(restore=restore)
            finally:
                self._pending_resets -= 1
                if owns_reset:
                    self._resetting = False
                self._cond.notify_all()

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

    def begin_request(
        self,
        event_type: str,
        *,
        epoch: int | None = None,
        **details: object,
    ) -> tuple[int, str]:
        with self._cond:
            owns_lifetime = epoch is None
            if owns_lifetime:
                while self._pending_resets > 0:
                    self._cond.wait()
                epoch = self._epoch
                self._in_flight[epoch] = self._in_flight.get(epoch, 0) + 1
            elif epoch != self._epoch:
                raise EpochStale()
            try:
                self._trace += 1
                trace_id = _format_trace_id(epoch, self._trace)
                self._append_event_locked(trace_id, event_type, **details)
                self._persist_locked()
            except Exception:
                if owns_lifetime:
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
            self._apply_fault_locked(config)
            return self.fault.model_copy()

    def configure_fault(self, config: FaultConfig, *, epoch: int | None = None) -> FaultState:
        with self._lock:
            self._require_epoch_locked(epoch)
            self._trip_locked("fault_configure")
            self._apply_fault_locked(config)
            self._trace += 1
            trace_id = _format_trace_id(self._epoch if epoch is None else epoch, self._trace)
            self._append_event_locked(
                trace_id,
                "fault_configured",
                mode=self.fault.mode,
                delay_ms=self.fault.delay_ms,
                remaining=self.fault.remaining,
                idempotency_key=self.fault.idempotency_key,
            )
            self._persist_locked()
            return self.fault.model_copy()

    def _apply_fault_locked(self, config: FaultConfig) -> None:
        remaining = 0 if config.mode == "none" else config.remaining
        self.fault = FaultState(
            mode=config.mode,
            delay_ms=config.delay_ms,
            remaining=remaining,
            idempotency_key=config.idempotency_key,
        )

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
            self._persist_locked()
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
        *,
        epoch: int | None = None,
        trace_id: str | None = None,
    ) -> dict:
        request_hash = sha256(f"{patient_id}|{title}|{priority}".encode()).hexdigest()
        with self._lock:
            self._require_epoch_locked(epoch, trace_id or "tr_000000")
            if trace_id is None:
                self._trace += 1
                trace_id = _format_trace_id(self._epoch if epoch is None else epoch, self._trace)

            existing_id = self.tasks_by_key.get(idempotency_key)
            if existing_id:
                existing = self.tasks[existing_id]
                if existing["request_hash"] != request_hash:
                    self._append_event_locked(
                        trace_id,
                        "conflict",
                        reason="idempotency_conflict",
                        task_id=existing["id"],
                    )
                    self._persist_locked()
                    return {"kind": "idempotency_conflict", "task": dict(existing)}
                self._append_event_locked(
                    trace_id,
                    "task_replayed",
                    task_id=existing["id"],
                    patient_id=existing["patient_id"],
                    idempotency_key=idempotency_key,
                    committed=True,
                )
                self._persist_locked()
                return {"kind": "replay", "task": dict(existing)}

            if patient_id not in self.patients:
                self._append_event_locked(trace_id, "commit_skipped", reason="patient_not_found")
                self._persist_locked()
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
            self._append_event_locked(
                trace_id,
                "task_committed",
                task_id=new_id,
                patient_id=patient_id,
                idempotency_key=idempotency_key,
                committed=True,
            )
            self._trip_locked("commit")
            self._persist_locked()
            return {"kind": "created", "task": dict(task)}

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
            "epoch": self._epoch,
            "tasks": self.tasks,
            "tasks_by_key": self.tasks_by_key,
            "events": self.events,
            "fault": self.fault.model_dump(),
        }

    def _restore_locked(self) -> bool:
        path = self.settings.state_path
        if not path:
            return False
        payload = read_state(path)
        if payload is None:
            return False
        tasks, tasks_by_key, events, seq, trace, epoch = _validate_pvs_snapshot(
            payload, self.settings, self.patients
        )
        self.tasks = tasks
        self.tasks_by_key = tasks_by_key
        self.events = events
        self._seq = seq
        self._trace = trace
        self._epoch = epoch
        if "fault" not in payload:
            self.fault = FaultState(mode="none", delay_ms=50, remaining=0, idempotency_key=None)
        else:
            self.fault = _validate_fault_state(payload["fault"])
        return True


def _format_trace_id(epoch: int, trace: int) -> str:
    return f"tr_{epoch:06d}_{trace:06d}"


def _parse_trace_id(trace_id: str) -> tuple[int, int] | None:
    match = TRACE_RE.fullmatch(trace_id)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_fault_state(raw: object) -> FaultState:
    if not isinstance(raw, dict):
        raise RestoreError("invalid stored fault")
    try:
        return FaultState.model_validate(raw)
    except ValidationError as exc:
        raise RestoreError("invalid stored fault") from exc


def _validate_pvs_snapshot(
    payload: dict[str, Any],
    settings: Settings,
    patients: dict[str, dict],
) -> tuple[dict[str, dict], dict[str, str], list[dict], int, int, int]:
    if payload.get("schema") != STATE_SCHEMA:
        raise RestoreError("unsupported state schema")
    if payload.get("seed") != settings.seed:
        raise RestoreError("seed mismatch")
    tasks_raw = payload.get("tasks")
    tasks_by_key_raw = payload.get("tasks_by_key")
    events_raw = payload.get("events")
    seq = payload.get("seq")
    trace = payload.get("trace")
    epoch = payload.get("epoch")
    if not isinstance(tasks_raw, dict) or not isinstance(tasks_by_key_raw, dict):
        raise RestoreError("task maps must be objects")
    if not isinstance(events_raw, list):
        raise RestoreError("events must be a list")
    if (
        not _is_int(seq)
        or not _is_int(trace)
        or not _is_int(epoch)
        or seq < 0
        or trace < 0
        or epoch < 0
    ):
        raise RestoreError("sequence counters are invalid")

    tasks: dict[str, dict] = {}
    for key, value in tasks_raw.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise RestoreError("invalid task record")
        try:
            stored = StoredTask.model_validate(value)
        except ValidationError as exc:
            raise RestoreError("invalid stored task") from exc
        if stored.id != key:
            raise RestoreError("task id does not match map key")
        if stored.status != "open":
            raise RestoreError("invalid task status")
        if stored.priority not in TASK_PRIORITIES:
            raise RestoreError("invalid task priority")
        if stored.patient_id not in patients:
            raise RestoreError("task references unknown patient")
        expected_hash = sha256(
            f"{stored.patient_id}|{stored.title}|{stored.priority}".encode()
        ).hexdigest()
        if stored.request_hash != expected_hash:
            raise RestoreError("task request_hash does not match body")
        if stored.id != task_id(settings.seed, stored.idempotency_key):
            raise RestoreError("task id is not seed-derived")
        tasks[key] = stored.model_dump()

    tasks_by_key: dict[str, str] = {}
    for key, value in tasks_by_key_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RestoreError("invalid idempotency map entry")
        target = tasks.get(value)
        if target is None:
            raise RestoreError("dangling tasks_by_key target")
        if target["idempotency_key"] != key:
            raise RestoreError("idempotency map key does not match task")
        tasks_by_key[key] = value
    rebuilt_keys = {task["idempotency_key"]: task["id"] for task in tasks.values()}
    if rebuilt_keys != tasks_by_key:
        raise RestoreError("idempotency map is inconsistent with tasks")

    events = _validate_events(events_raw, seq, trace, epoch)
    _validate_commit_evidence(tasks, events)
    _validate_trace_identities(events)
    return tasks, tasks_by_key, events, seq, trace, epoch


def _validate_events(events_raw: list[object], seq: int, trace: int, epoch: int) -> list[dict]:
    events: list[dict] = []
    max_local_trace = 0
    for index, item in enumerate(events_raw):
        if not isinstance(item, dict):
            raise RestoreError("event entry is not an object")
        try:
            event = Event.model_validate(item)
        except ValidationError as exc:
            raise RestoreError("malformed event") from exc
        expected_seq = index + 1
        if event.seq != expected_seq:
            raise RestoreError("event sequence is not the generated contiguous order")
        parsed = _parse_trace_id(event.trace_id)
        if parsed is None:
            raise RestoreError("event trace_id is invalid")
        event_epoch, local_trace = parsed
        if event_epoch != epoch:
            raise RestoreError("event trace epoch does not match snapshot epoch")
        if local_trace < 1:
            raise RestoreError("event local trace is not allocated")
        if not event.type or not isinstance(event.type, str):
            raise RestoreError("event type is invalid")
        if not isinstance(event.details, dict):
            raise RestoreError("event details must be an object")
        max_local_trace = max(max_local_trace, local_trace)
        events.append(event.model_dump())
    if seq != len(events) or max_local_trace > trace or (events and max_local_trace != trace):
        raise RestoreError("counters do not dominate restored events")
    return events


def _validate_commit_evidence(tasks: dict[str, dict], events: list[dict]) -> None:
    committed: set[str] = set()
    for event in events:
        if event["type"] != "task_committed":
            continue
        details = event["details"]
        task_id_value = details.get("task_id")
        if not isinstance(task_id_value, str) or task_id_value not in tasks:
            raise RestoreError("committed evidence references a missing task")
        task = tasks[task_id_value]
        if details.get("committed") is not True:
            raise RestoreError("committed evidence is not marked committed")
        if details.get("idempotency_key") != task["idempotency_key"]:
            raise RestoreError("committed evidence idempotency key does not match task")
        if details.get("patient_id") != task["patient_id"]:
            raise RestoreError("committed evidence patient does not match task")
        if task_id_value in committed:
            raise RestoreError("duplicate committed evidence")
        committed.add(task_id_value)
    missing = set(tasks) - committed
    if missing:
        raise RestoreError("task exists without matching committed evidence")


def _validate_trace_identities(events: list[dict]) -> None:
    grouped: dict[str, list[dict]] = {}
    for event in events:
        grouped.setdefault(event["trace_id"], []).append(event)
    for group in grouped.values():
        object_ids: set[str] = set()
        operation_keys: set[str] = set()
        requested = 0
        terminals = 0
        has_fault_configured = False
        has_other_events = False
        for event in group:
            details = event["details"]
            event_type = event["type"]
            if event_type == "task_requested":
                requested += 1
            if event_type in {
                "task_committed",
                "task_replayed",
                "conflict",
                "commit_skipped",
                "fault_configured",
            }:
                terminals += 1
            if event_type == "fault_configured":
                has_fault_configured = True
            else:
                has_other_events = True
            if event_type in {"task_committed", "task_replayed"}:
                task_id_value = details.get("task_id")
                if isinstance(task_id_value, str):
                    object_ids.add(task_id_value)
            if event_type in {"task_requested", "task_committed", "task_replayed"}:
                key = details.get("idempotency_key")
                if isinstance(key, str):
                    operation_keys.add(key)
        if (
            requested > 1
            or terminals > 1
            or (has_fault_configured and has_other_events)
            or len(object_ids) > 1
            or len(operation_keys) > 1
        ):
            raise RestoreError("collapsed trace history")

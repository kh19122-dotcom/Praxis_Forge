from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

FaultMode = Literal["none", "fail_before_commit", "delay", "ambiguous"]
PatientStatus = Literal["active"]
EncounterKind = Literal["intake", "follow_up", "review"]
EncounterStatus = Literal["completed"]
TaskPriority = Literal["low", "normal", "high"]
TaskStatus = Literal["open"]

SYNTH_ID_PATTERN = r"^synth-[a-z0-9-]+$"


class Patient(BaseModel):
    id: str
    cohort: str
    site: str
    status: PatientStatus


class PatientList(BaseModel):
    seed: str
    patients: list[Patient]


class Encounter(BaseModel):
    id: str
    patient_id: str
    occurred_at: str
    kind: EncounterKind
    summary: str
    status: EncounterStatus


class EncounterList(BaseModel):
    seed: str
    patient_id: str
    encounters: list[Encounter]


class TaskCreate(BaseModel):
    patient_id: str = Field(pattern=SYNTH_ID_PATTERN)
    title: str = Field(pattern=SYNTH_ID_PATTERN)
    priority: TaskPriority = "normal"


class Task(BaseModel):
    id: str
    patient_id: str
    title: str
    priority: TaskPriority
    status: TaskStatus
    idempotency_key: str


class ErrorBody(BaseModel):
    error: str
    message: str
    trace_id: str
    details: dict[str, Any] | None = None


class Health(BaseModel):
    status: Literal["ok"]
    service: Literal["fake-pvs"]
    seed: str


class FaultConfig(BaseModel):
    mode: FaultMode = "none"
    delay_ms: int = Field(default=50, ge=0, le=5000)
    remaining: int = Field(default=1, ge=0, le=100)
    idempotency_key: str | None = None


class FaultState(BaseModel):
    mode: FaultMode
    delay_ms: int
    remaining: int
    idempotency_key: str | None = None


class Event(BaseModel):
    seq: int
    trace_id: str
    type: str
    details: dict[str, Any] = Field(default_factory=dict)


class EventList(BaseModel):
    seed: str
    events: list[Event]


class ResetResult(BaseModel):
    status: Literal["reset"]
    seed: str

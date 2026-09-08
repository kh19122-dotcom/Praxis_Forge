from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

FaultMode = Literal["none", "fail_before_commit", "delay", "ambiguous"]
PatientStatus = Literal["active"]
EncounterKind = Literal["intake", "follow_up", "review"]
EncounterStatus = Literal["completed"]
TaskPriority = Literal["low", "normal", "high"]
TaskStatus = Literal["open"]
DocumentStatus = Literal["committed"]

SYNTH_ID_PATTERN = r"^synth-[a-z0-9-]+$"
IS_REF_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
CONTENT_HASH_PATTERN = r"^[0-9a-f]{64}$"
SHA256_HEX_PATTERN = r"^[0-9a-f]{64}$"
RENDERED_TEXT_MAX_UTF8_BYTES = 262144


def validate_rendered_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("rendered_text must be a string")
    if "\x00" in value:
        raise ValueError("rendered_text must not contain NUL")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("rendered_text must be well-formed UTF-8") from exc
    if not (1 <= len(encoded) <= RENDERED_TEXT_MAX_UTF8_BYTES):
        raise ValueError("rendered_text UTF-8 length must be 1..262144")
    return value

RenderedText = Annotated[str, AfterValidator(validate_rendered_text)]


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


class ClinicalDocumentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    synthetic_patient_id: str = Field(pattern=IS_REF_PATTERN)
    synthetic_visit_id: str = Field(pattern=IS_REF_PATTERN)
    document_ref: str = Field(pattern=IS_REF_PATTERN)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    rendered_text: RenderedText


class ClinicalDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    synthetic_patient_id: str = Field(pattern=IS_REF_PATTERN)
    synthetic_visit_id: str = Field(pattern=IS_REF_PATTERN)
    document_ref: str = Field(pattern=IS_REF_PATTERN)
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    rendered_text: RenderedText
    rendered_text_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    idempotency_key: str
    status: DocumentStatus


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
    model_config = ConfigDict(extra="forbid")

    mode: FaultMode
    delay_ms: int
    remaining: int
    idempotency_key: str | None = None


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

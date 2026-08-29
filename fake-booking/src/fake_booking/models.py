from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FaultMode = Literal["none", "fail_before_commit", "delay", "ambiguous"]


class Slot(BaseModel):
    id: str
    resource_id: str
    start: str
    end: str
    available: bool


class SlotList(BaseModel):
    seed: str
    slots: list[Slot]


class BookingCreate(BaseModel):
    slot_id: str = Field(min_length=1)
    patient_ref: str = Field(pattern=r"^synth-[a-z0-9-]+$")


class Booking(BaseModel):
    id: str
    slot_id: str
    resource_id: str
    start: str
    end: str
    patient_ref: str
    status: Literal["confirmed"]
    idempotency_key: str


class ErrorBody(BaseModel):
    error: str
    message: str
    trace_id: str
    details: dict[str, Any] | None = None


class Health(BaseModel):
    status: Literal["ok"]
    service: Literal["fake-booking"]
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

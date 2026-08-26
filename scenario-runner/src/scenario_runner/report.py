from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["pass", "fail"]


@dataclass
class StepResult:
    name: str
    status: Status
    service: str | None = None
    method: str | None = None
    path: str | None = None
    http_status: int | None = None
    trace_id: str | None = None
    ids: dict[str, str] = field(default_factory=dict)
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "service": self.service,
            "method": self.method,
            "path": self.path,
            "http_status": self.http_status,
            "trace_id": self.trace_id,
            "ids": self.ids,
            "detail": self.detail,
        }


@dataclass
class ScenarioResult:
    name: str
    status: Status
    steps: list[StepResult] = field(default_factory=list)
    ids: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
            "ids": self.ids,
            "error": self.error,
        }


@dataclass
class SuiteReport:
    status: Status
    booking_url: str
    pvs_url: str
    booking_seed: str | None = None
    pvs_seed: str | None = None
    scenarios: list[ScenarioResult] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "booking_url": self.booking_url,
            "pvs_url": self.pvs_url,
            "booking_seed": self.booking_seed,
            "pvs_seed": self.pvs_seed,
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
            "error": self.error,
        }

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["pass", "fail"]
SoakMode = Literal["soak", "replay"]

SOAK_EVIDENCE_SCHEMA = "praxis-forge.soak-evidence.v1"


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

    def to_dict(self, *, include_steps: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "ids": self.ids,
            "error": self.error,
        }
        if include_steps:
            payload["steps"] = [step.to_dict() for step in self.steps]
        return payload


@dataclass
class SuiteReport:
    status: Status
    booking_url: str
    pvs_url: str
    booking_seed: str | None = None
    pvs_seed: str | None = None
    suite: str | None = None
    booking_chaos_url: str | None = None
    pvs_chaos_url: str | None = None
    scenarios: list[ScenarioResult] = field(default_factory=list)
    error: str | None = None

    def to_dict(self, *, include_steps: bool = True) -> dict[str, Any]:
        return {
            "status": self.status,
            "suite": self.suite,
            "booking_url": self.booking_url,
            "pvs_url": self.pvs_url,
            "booking_chaos_url": self.booking_chaos_url,
            "pvs_chaos_url": self.pvs_chaos_url,
            "booking_seed": self.booking_seed,
            "pvs_seed": self.pvs_seed,
            "scenarios": [
                scenario.to_dict(include_steps=include_steps) for scenario in self.scenarios
            ],
            "error": self.error,
        }


@dataclass
class FirstFailure:
    iteration: int
    suite: str
    replay_selector: str
    scenario: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "suite": self.suite,
            "replay_selector": self.replay_selector,
            "scenario": self.scenario,
            "error": self.error,
        }


@dataclass
class IterationReport:
    iteration: int
    suite: str
    replay_selector: str
    status: Status
    scenarios: list[ScenarioResult] = field(default_factory=list)
    booking_seed: str | None = None
    pvs_seed: str | None = None
    error: str | None = None

    def to_dict(self, *, include_steps: bool = True) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "suite": self.suite,
            "replay_selector": self.replay_selector,
            "status": self.status,
            "booking_seed": self.booking_seed,
            "pvs_seed": self.pvs_seed,
            "scenarios": [
                scenario.to_dict(include_steps=include_steps) for scenario in self.scenarios
            ],
            "error": self.error,
        }


@dataclass
class SoakReport:
    status: Status
    mode: SoakMode
    suites: list[str]
    requested_iterations: int
    completed_iterations: int = 0
    booking_url: str = ""
    pvs_url: str = ""
    booking_chaos_url: str | None = None
    pvs_chaos_url: str | None = None
    booking_seed: str | None = None
    pvs_seed: str | None = None
    replay_selector: str | None = None
    evidence_file: str | None = None
    iterations: list[IterationReport] = field(default_factory=list)
    first_failure: FirstFailure | None = None
    error: str | None = None
    schema: str = SOAK_EVIDENCE_SCHEMA

    def to_dict(self, *, include_steps: bool = True) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "mode": self.mode,
            "suites": list(self.suites),
            "requested_iterations": self.requested_iterations,
            "completed_iterations": self.completed_iterations,
            "replay_selector": self.replay_selector,
            "booking_url": self.booking_url,
            "pvs_url": self.pvs_url,
            "booking_chaos_url": self.booking_chaos_url,
            "pvs_chaos_url": self.pvs_chaos_url,
            "booking_seed": self.booking_seed,
            "pvs_seed": self.pvs_seed,
            "evidence_file": self.evidence_file,
            "iterations": [
                item.to_dict(include_steps=include_steps) for item in self.iterations
            ],
            "first_failure": None if self.first_failure is None else self.first_failure.to_dict(),
            "error": self.error,
        }

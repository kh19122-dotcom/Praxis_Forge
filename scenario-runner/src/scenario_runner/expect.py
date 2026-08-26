from __future__ import annotations

from typing import Any

from scenario_runner.http import HttpCall
from scenario_runner.report import StepResult


class CheckFailed(Exception):
    def __init__(self, step: StepResult) -> None:
        self.step = step
        super().__init__(step.detail or step.name)


class ScenarioContext:
    def __init__(self) -> None:
        self.steps: list[StepResult] = []
        self.ids: dict[str, str] = {}

    def remember(self, key: str, value: str) -> None:
        self.ids[key] = value

    def check(
        self,
        name: str,
        ok: bool,
        *,
        service: str | None = None,
        call: HttpCall | None = None,
        ids: dict[str, str] | None = None,
        detail: str | None = None,
        trace_id: str | None = None,
    ) -> StepResult:
        merged_ids = dict(self.ids)
        if ids:
            merged_ids.update(ids)
        resolved_trace = trace_id
        if resolved_trace is None and call is not None:
            resolved_trace = call.trace_id
        step = StepResult(
            name=name,
            status="pass" if ok else "fail",
            service=service,
            method=None if call is None else call.method,
            path=None if call is None else call.path,
            http_status=None if call is None else call.status_code,
            trace_id=resolved_trace,
            ids=merged_ids,
            detail=None if ok else detail,
        )
        self.steps.append(step)
        if not ok:
            raise CheckFailed(step)
        return step

    def expect_http(
        self,
        name: str,
        call: HttpCall,
        *,
        service: str,
        status: int,
        detail: str | None = None,
    ) -> dict[str, Any]:
        if call.error:
            self.check(
                name,
                False,
                service=service,
                call=call,
                detail=detail or f"HTTP call failed: {call.error}",
            )
        if call.status_code != status:
            self.check(
                name,
                False,
                service=service,
                call=call,
                detail=detail or f"expected HTTP {status}, got {call.status_code}: {call.body!r}",
            )
        if not isinstance(call.body, dict):
            self.check(
                name,
                False,
                service=service,
                call=call,
                detail=detail or f"expected JSON object, got {call.body!r}",
            )
        self.check(name, True, service=service, call=call, detail=detail)
        return call.body

    def expect_transport_error(
        self,
        name: str,
        call: HttpCall,
        *,
        service: str,
        detail: str | None = None,
    ) -> None:
        ok = call.error is not None and call.status_code is None
        self.check(
            name,
            ok,
            service=service,
            call=call,
            detail=detail
            or (
                "expected client-visible transport error, "
                f"got status={call.status_code!r} body={call.body!r} error={call.error!r}"
            ),
        )

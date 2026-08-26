from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi import Path as ApiPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from fake_pvs.models import (
    SYNTH_ID_PATTERN,
    Encounter,
    EncounterList,
    ErrorBody,
    Event,
    EventList,
    FaultConfig,
    FaultState,
    Health,
    Patient,
    PatientList,
    ResetResult,
    Task,
    TaskCreate,
)
from fake_pvs.persist import RestoreError
from fake_pvs.settings import Settings
from fake_pvs.store import EpochStale, Store

OPENAPI_PATH = Path(__file__).with_name("openapi.yaml")
try:
    store = Store(Settings.from_env())
except RestoreError as exc:
    raise SystemExit(f"fake-pvs restore failed: {exc}") from exc

app = FastAPI(
    title="Praxis Forge Fake PVS",
    version="0.1.0",
    description="Deterministic synthetic PVS-like simulator.",
    openapi_url="/openapi.json",
    docs_url="/docs",
)


class DataPlaneAdmissionMiddleware:
    """Enroll POST /v1/tasks before downstream body consumption."""

    def __init__(self, app, *, store: Store, path: str) -> None:
        self.app = app
        self.store = store
        self.path = path

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != self.path
        ):
            await self.app(scope, receive, send)
            return
        epoch = self.store.admit()
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["admission_epoch"] = epoch
        else:
            state.admission_epoch = epoch
        try:
            await self.app(scope, receive, send)
        finally:
            self.store.release(epoch)


app.add_middleware(DataPlaneAdmissionMiddleware, store=store, path="/v1/tasks")


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorBody(
            error="http_error",
            message=str(detail),
            trace_id="tr_000000",
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=ErrorBody(
            error="validation_error",
            message="Request failed schema validation.",
            trace_id="tr_000000",
            details={"errors": exc.errors()},
        ).model_dump(),
    )


def _error(status: int, error: str, message: str, trace_id: str, **details: object) -> None:
    raise HTTPException(
        status_code=status,
        detail=ErrorBody(
            error=error,
            message=message,
            trace_id=trace_id,
            details=details or None,
        ).model_dump(),
    )


def _stale(trace_id: str) -> None:
    _error(
        409,
        "epoch_stale",
        "Request began before a completed reset and cannot mutate the new epoch.",
        trace_id,
        committed=False,
    )


def _public_patient(patient: dict) -> Patient:
    return Patient(
        id=patient["id"],
        cohort=patient["cohort"],
        site=patient["site"],
        status=patient["status"],
    )


def _public_encounter(encounter: dict) -> Encounter:
    return Encounter(
        id=encounter["id"],
        patient_id=encounter["patient_id"],
        occurred_at=encounter["occurred_at"],
        kind=encounter["kind"],
        summary=encounter["summary"],
        status=encounter["status"],
    )


def _public_task(task: dict) -> Task:
    return Task(
        id=task["id"],
        patient_id=task["patient_id"],
        title=task["title"],
        priority=task["priority"],
        status=task["status"],
        idempotency_key=task["idempotency_key"],
    )


@app.get("/healthz", response_model=Health, tags=["meta"])
def healthz() -> Health:
    return Health(status="ok", service="fake-pvs", seed=store.settings.seed)


@app.get("/openapi.yaml", include_in_schema=False)
def openapi_yaml() -> Response:
    return Response(OPENAPI_PATH.read_text(encoding="utf-8"), media_type="application/yaml")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/v1/patients", response_model=PatientList, tags=["patients"])
def list_patients(
    cohort: str | None = None,
    site: str | None = None,
    patient_id: str | None = Query(default=None, alias="id", pattern=SYNTH_ID_PATTERN),
) -> PatientList:
    patients = [
        _public_patient(item) for item in store.list_patients(cohort, site, patient_id)
    ]
    return PatientList(seed=store.settings.seed, patients=patients)


@app.get("/v1/patients/{patient_id}", response_model=Patient, tags=["patients"])
def get_patient(patient_id: str = ApiPath(pattern=SYNTH_ID_PATTERN)) -> Patient:
    patient = store.get_patient(patient_id)
    if patient is None:
        _error(404, "patient_not_found", "Unknown patient.", "tr_000000", patient_id=patient_id)
    return _public_patient(patient)


@app.get(
    "/v1/patients/{patient_id}/encounters",
    response_model=EncounterList,
    tags=["encounters"],
)
def list_patient_encounters(patient_id: str = ApiPath(pattern=SYNTH_ID_PATTERN)) -> EncounterList:
    encounters = store.list_encounters(patient_id)
    if encounters is None:
        _error(404, "patient_not_found", "Unknown patient.", "tr_000000", patient_id=patient_id)
    return EncounterList(
        seed=store.settings.seed,
        patient_id=patient_id,
        encounters=[_public_encounter(item) for item in encounters],
    )


@app.get("/v1/encounters/{encounter_id}", response_model=Encounter, tags=["encounters"])
def get_encounter(encounter_id: str) -> Encounter:
    encounter = store.get_encounter(encounter_id)
    if encounter is None:
        _error(
            404,
            "encounter_not_found",
            "Unknown encounter.",
            "tr_000000",
            encounter_id=encounter_id,
        )
    return _public_encounter(encounter)


@app.post("/v1/tasks", response_model=Task, status_code=201, tags=["tasks"])
async def create_task(
    request: Request,
    body: TaskCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
) -> Task:
    try:
        epoch, trace_id = store.begin_request(
            "task_requested",
            epoch=request.state.admission_epoch,
            idempotency_key=idempotency_key,
            patient_id=body.patient_id,
            title=body.title,
            priority=body.priority,
        )
        fault = store.consume_fault(idempotency_key, epoch=epoch)
        if fault.mode == "fail_before_commit":
            store.record(trace_id, "fault_injected", epoch=epoch, mode=fault.mode)
            store.record(trace_id, "commit_skipped", epoch=epoch, reason="fail_before_commit")
            _error(
                503,
                "fail_before_commit",
                "Remote commit was not attempted.",
                trace_id,
                committed=False,
            )
        if fault.mode in {"delay", "ambiguous"} and fault.delay_ms:
            await asyncio.sleep(fault.delay_ms / 1000)
            store.record(
                trace_id,
                "response_delayed",
                epoch=epoch,
                delay_ms=fault.delay_ms,
                mode=fault.mode,
            )
        result = store.create_task(
            idempotency_key,
            body.patient_id,
            body.title,
            body.priority,
            epoch=epoch,
            trace_id=trace_id,
        )
    except EpochStale as stale:
        _stale(stale.trace_id)

    kind = result["kind"]
    if kind == "patient_not_found":
        _error(
            404,
            "patient_not_found",
            "Unknown patient.",
            trace_id,
            patient_id=body.patient_id,
        )
    if kind == "idempotency_conflict":
        _error(
            409,
            "idempotency_conflict",
            "Idempotency key was reused with a different request body.",
            trace_id,
            task_id=result["task"]["id"],
            committed=False,
        )

    task = result["task"]
    if not store.epoch_is_current(epoch):
        _stale(trace_id)
    if fault.mode == "ambiguous":
        try:
            store.record(
                trace_id,
                "response_suppressed",
                epoch=epoch,
                reason="ambiguous_outcome",
                task_id=task["id"],
                committed=True,
            )
        except EpochStale as stale:
            _stale(stale.trace_id)
        _error(
            504,
            "ambiguous_outcome",
            "Remote effect may have committed; client must inspect Forge evidence.",
            trace_id,
            committed=None,
        )
    if kind == "replay":
        response.status_code = 200
    return _public_task(task)


@app.get("/v1/tasks/{task_id}", response_model=Task, tags=["tasks"])
def get_task(task_id: str) -> Task:
    task = store.get_task(task_id)
    if task is None:
        _error(404, "task_not_found", "Unknown task.", "tr_000000", task_id=task_id)
    return _public_task(task)


@app.get("/v1/admin/events", response_model=EventList, tags=["admin"])
def list_events(trace_id: str | None = None) -> EventList:
    events = store.events
    if trace_id:
        events = [event for event in events if event["trace_id"] == trace_id]
    return EventList(
        seed=store.settings.seed,
        events=[Event.model_validate(event) for event in events],
    )


@app.get("/v1/admin/faults", response_model=FaultState, tags=["admin"])
def get_fault() -> FaultState:
    return store.fault


@app.put("/v1/admin/faults", response_model=FaultState, tags=["admin"])
def put_fault(config: FaultConfig) -> FaultState:
    epoch = store.epoch()
    try:
        fault = store.set_fault(config, epoch=epoch)
        store.record(
            store.next_trace_id(epoch=epoch),
            "fault_configured",
            epoch=epoch,
            mode=fault.mode,
            delay_ms=fault.delay_ms,
            remaining=fault.remaining,
            idempotency_key=fault.idempotency_key,
        )
    except EpochStale as stale:
        _stale(stale.trace_id)
    return fault


@app.post("/v1/admin/reset", response_model=ResetResult, tags=["admin"])
def reset() -> ResetResult:
    store.reset()
    return ResetResult(status="reset", seed=store.settings.seed)

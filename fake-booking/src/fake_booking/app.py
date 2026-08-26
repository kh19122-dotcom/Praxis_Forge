from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from fake_booking.models import (
    Booking,
    BookingCreate,
    ErrorBody,
    Event,
    EventList,
    FaultConfig,
    FaultState,
    Health,
    ResetResult,
    Slot,
    SlotList,
)
from fake_booking.persist import RestoreError
from fake_booking.settings import Settings
from fake_booking.store import EpochStale, Store

OPENAPI_PATH = Path(__file__).with_name("openapi.yaml")
try:
    store = Store(Settings.from_env())
except RestoreError as exc:
    raise SystemExit(f"fake-booking restore failed: {exc}") from exc

app = FastAPI(
    title="Praxis Forge Fake Booking",
    version="0.1.0",
    description="Deterministic synthetic booking-provider simulator.",
    openapi_url="/openapi.json",
    docs_url="/docs",
)


class RequestAdmissionMiddleware:
    """Enroll mutating requests before downstream body consumption."""

    def __init__(self, app, *, store: Store, routes: frozenset[tuple[str, str]]) -> None:
        self.app = app
        self.store = store
        self.routes = routes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method")
        path = scope.get("path")
        if (method, path) not in self.routes:
            await self.app(scope, receive, send)
            return
        epoch = await asyncio.to_thread(self.store.admit)
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["admission_epoch"] = epoch
        else:
            state.admission_epoch = epoch
        try:
            await self.app(scope, receive, send)
        finally:
            self.store.release(epoch)


app.add_middleware(
    RequestAdmissionMiddleware,
    store=store,
    routes=frozenset(
        {
            ("POST", "/v1/bookings"),
            ("PUT", "/v1/admin/faults"),
        }
    ),
)


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


def _public_booking(booking: dict) -> Booking:
    return Booking(
        id=booking["id"],
        slot_id=booking["slot_id"],
        resource_id=booking["resource_id"],
        start=booking["start"],
        end=booking["end"],
        patient_ref=booking["patient_ref"],
        status=booking["status"],
        idempotency_key=booking["idempotency_key"],
    )


@app.get("/healthz", response_model=Health, tags=["meta"])
def healthz() -> Health:
    return Health(status="ok", service="fake-booking", seed=store.settings.seed)


@app.get("/openapi.yaml", include_in_schema=False)
def openapi_yaml() -> Response:
    return Response(OPENAPI_PATH.read_text(encoding="utf-8"), media_type="application/yaml")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/v1/slots", response_model=SlotList, tags=["booking"])
def list_slots(
    resource_id: str | None = None,
    available: bool = Query(default=True),
) -> SlotList:
    slots = [
        Slot(
            id=item["id"],
            resource_id=item["resource_id"],
            start=item["start"],
            end=item["end"],
            available=item["available"],
        )
        for item in store.list_slots(resource_id, available)
    ]
    return SlotList(seed=store.settings.seed, slots=slots)


@app.post("/v1/bookings", response_model=Booking, status_code=201, tags=["booking"])
async def create_booking(
    request: Request,
    body: BookingCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
) -> Booking:
    try:
        epoch, trace_id = store.begin_request(
            "booking_requested",
            epoch=request.state.admission_epoch,
            idempotency_key=idempotency_key,
            slot_id=body.slot_id,
            patient_ref=body.patient_ref,
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
        result = store.create_booking(
            idempotency_key,
            body.slot_id,
            body.patient_ref,
            epoch=epoch,
            trace_id=trace_id,
        )
    except EpochStale as stale:
        _stale(stale.trace_id)

    kind = result["kind"]
    if kind == "slot_not_found":
        _error(404, "slot_not_found", "Unknown slot.", trace_id, slot_id=body.slot_id)
    if kind == "slot_conflict":
        _error(
            409,
            "slot_conflict",
            "Slot is already booked.",
            trace_id,
            existing_booking_id=result["existing_booking_id"],
            committed=False,
        )
    if kind == "idempotency_conflict":
        _error(
            409,
            "idempotency_conflict",
            "Idempotency key was reused with a different request body.",
            trace_id,
            booking_id=result["booking"]["id"],
            committed=False,
        )

    booking = result["booking"]
    if not store.epoch_is_current(epoch):
        _stale(trace_id)
    if fault.mode == "ambiguous":
        try:
            store.record(
                trace_id,
                "response_suppressed",
                epoch=epoch,
                reason="ambiguous_outcome",
                booking_id=booking["id"],
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
    return _public_booking(booking)


@app.get("/v1/bookings/{booking_id}", response_model=Booking, tags=["booking"])
def get_booking(booking_id: str) -> Booking:
    booking = store.get_booking(booking_id)
    if booking is None:
        _error(404, "booking_not_found", "Unknown booking.", "tr_000000", booking_id=booking_id)
    return _public_booking(booking)


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
def put_fault(request: Request, config: FaultConfig) -> FaultState:
    epoch = request.state.admission_epoch
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

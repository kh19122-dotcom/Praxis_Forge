from __future__ import annotations

from collections.abc import Callable

from scenario_runner.expect import ScenarioContext
from scenario_runner.http import ForgeSession, ServiceClient
from scenario_runner.scenarios import (
    SYNTH_PATIENT,
    SYNTH_TASK_TITLE,
    _create_booking,
    _create_task,
    _event_types,
    _first_available_slot,
    reset_services,
)


def _require_chaos(
    ctx: ScenarioContext, session: ForgeSession
) -> tuple[ServiceClient, ServiceClient, ServiceClient, ServiceClient]:
    ctx.check(
        "chaos_clients_configured",
        session.booking_chaos is not None
        and session.pvs_chaos is not None
        and session.booking_chaos_admin is not None
        and session.pvs_chaos_admin is not None,
        detail="transport-chaos scenarios require chaos proxy and admin clients",
    )
    assert session.booking_chaos is not None
    assert session.pvs_chaos is not None
    assert session.booking_chaos_admin is not None
    assert session.pvs_chaos_admin is not None
    return (
        session.booking_chaos,
        session.pvs_chaos,
        session.booking_chaos_admin,
        session.pvs_chaos_admin,
    )


def _reset_chaos(ctx: ScenarioContext, admin: ServiceClient, *, service: str) -> None:
    reset = admin.request("POST", "/v1/admin/reset")
    body = ctx.expect_http(f"reset_{service}_chaos", reset, service=service, status=200)
    ctx.check(
        f"{service}_chaos_reset",
        body.get("status") == "reset",
        service=service,
        call=reset,
        detail=f"unexpected chaos reset: {body!r}",
    )


def _arm_fault(
    ctx: ScenarioContext,
    admin: ServiceClient,
    *,
    service: str,
    mode: str,
    method: str,
    path: str,
    remaining: int = 1,
    delay_ms: int = 50,
) -> None:
    fault = admin.request(
        "PUT",
        "/v1/admin/faults",
        json={
            "mode": mode,
            "remaining": remaining,
            "delay_ms": delay_ms,
            "method": method,
            "path": path,
        },
    )
    body = ctx.expect_http(f"arm_{service}_{mode}", fault, service=service, status=200)
    ctx.check(
        f"{service}_fault_armed_{mode}",
        body.get("mode") == mode and body.get("remaining") == remaining,
        service=service,
        call=fault,
        detail=f"unexpected fault state: {body!r}",
    )


def _chaos_event_types(ctx: ScenarioContext, admin: ServiceClient, *, service: str) -> list[str]:
    listed = admin.request("GET", "/v1/admin/events")
    body = ctx.expect_http(f"list_{service}_chaos_events", listed, service=service, status=200)
    events = body.get("events")
    ctx.check(
        f"{service}_chaos_events_list",
        isinstance(events, list),
        service=service,
        call=listed,
        detail=f"unexpected chaos events payload: {body!r}",
    )
    return [str(event.get("type")) for event in events if isinstance(event, dict)]


def _latest_committed_id(
    ctx: ScenarioContext,
    client: ServiceClient,
    *,
    service: str,
    event_type: str,
    detail_key: str,
) -> str:
    listed = client.request("GET", "/v1/admin/events")
    body = ctx.expect_http(
        f"list_{service}_events_after_transport",
        listed,
        service=service,
        status=200,
    )
    events = body.get("events")
    matched = []
    if isinstance(events, list):
        matched = [event for event in events if event.get("type") == event_type]
    ctx.check(
        f"{service}_committed_event_present",
        bool(matched),
        service=service,
        call=listed,
        detail=f"no {event_type} event after transport fault",
    )
    value = (matched[-1].get("details") or {}).get(detail_key)
    ctx.check(
        f"{service}_id_recovered_from_events",
        isinstance(value, str) and value,
        service=service,
        call=listed,
        detail=f"committed event missing {detail_key}: {matched[-1]!r}",
    )
    return str(value)


def booking_transport_drop_before_upstream(ctx: ScenarioContext, session: ForgeSession) -> None:
    booking_chaos, _pvs_chaos, booking_admin, pvs_admin = _require_chaos(ctx, session)
    reset_services(ctx, session.booking, session.pvs)
    _reset_chaos(ctx, booking_admin, service="booking")
    _reset_chaos(ctx, pvs_admin, service="pvs")
    slot = _first_available_slot(ctx, session.booking)
    _arm_fault(
        ctx,
        booking_admin,
        service="booking",
        mode="drop_before_upstream",
        method="POST",
        path="/v1/bookings",
    )
    failed = _create_booking(
        booking_chaos,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-booking-drop-before",
    )
    ctx.expect_transport_error(
        "booking_pre_upstream_transport_error",
        failed,
        service="booking",
    )
    types = _chaos_event_types(ctx, booking_admin, service="booking")
    ctx.check(
        "booking_pre_upstream_chaos_events",
        "dropped_before_upstream" in types and "upstream_completed" not in types,
        service="booking",
        detail=f"chaos event types: {types}",
    )
    events = session.booking.request("GET", "/v1/admin/events")
    events_body = ctx.expect_http(
        "booking_events_after_pre_drop", events, service="booking", status=200
    )
    ctx.check(
        "booking_pre_drop_left_no_commit",
        "booking_committed" not in _event_types(events_body.get("events") or []),
        service="booking",
        call=events,
        detail=f"unexpected booking events: {events_body!r}",
    )
    still_open = session.booking.request("GET", "/v1/slots")
    still_open_body = ctx.expect_http(
        "slots_after_pre_drop", still_open, service="booking", status=200
    )
    available_ids = {
        item.get("id") for item in still_open_body.get("slots", []) if isinstance(item, dict)
    }
    ctx.check(
        "pre_drop_left_slot_available",
        slot["id"] in available_ids,
        service="booking",
        call=still_open,
        detail=f"slot {slot['id']} missing after pre-upstream drop",
    )
    retry = _create_booking(
        booking_chaos,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-booking-drop-before",
    )
    retry_body = ctx.expect_http(
        "booking_retry_after_pre_drop", retry, service="booking", status=201
    )
    booking_id = retry_body.get("id")
    ctx.check(
        "booking_retry_committed_after_pre_drop",
        isinstance(booking_id, str) and retry_body.get("slot_id") == slot["id"],
        service="booking",
        call=retry,
    )
    ctx.remember("booking_id", str(booking_id))


def booking_transport_drop_after_upstream(ctx: ScenarioContext, session: ForgeSession) -> None:
    booking_chaos, _pvs_chaos, booking_admin, pvs_admin = _require_chaos(ctx, session)
    reset_services(ctx, session.booking, session.pvs)
    _reset_chaos(ctx, booking_admin, service="booking")
    _reset_chaos(ctx, pvs_admin, service="pvs")
    slot = _first_available_slot(ctx, session.booking)
    _arm_fault(
        ctx,
        booking_admin,
        service="booking",
        mode="drop_after_upstream",
        method="POST",
        path="/v1/bookings",
    )
    failed = _create_booking(
        booking_chaos,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-booking-drop-after",
    )
    ctx.expect_transport_error(
        "booking_post_upstream_transport_error",
        failed,
        service="booking",
    )
    types = _chaos_event_types(ctx, booking_admin, service="booking")
    ctx.check(
        "booking_post_upstream_chaos_events",
        "upstream_completed" in types and "dropped_after_upstream" in types,
        service="booking",
        detail=f"chaos event types: {types}",
    )
    booking_id = _latest_committed_id(
        ctx,
        session.booking,
        service="booking",
        event_type="booking_committed",
        detail_key="booking_id",
    )
    ctx.remember("booking_id", booking_id)
    evidence = session.booking.request("GET", f"/v1/bookings/{booking_id}")
    evidence_body = ctx.expect_http(
        "booking_evidence_after_transport_drop",
        evidence,
        service="booking",
        status=200,
    )
    ctx.check(
        "booking_evidence_matches_request",
        evidence_body.get("id") == booking_id and evidence_body.get("slot_id") == slot["id"],
        service="booking",
        call=evidence,
    )
    remaining = session.booking.request("GET", "/v1/slots")
    remaining_body = ctx.expect_http(
        "slots_after_post_drop", remaining, service="booking", status=200
    )
    remaining_ids = {
        item.get("id")
        for item in remaining_body.get("slots", [])
        if isinstance(item, dict)
    }
    ctx.check(
        "post_drop_consumed_slot",
        slot["id"] not in remaining_ids,
        service="booking",
        call=remaining,
    )
    replay = _create_booking(
        booking_chaos,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-booking-drop-after",
    )
    replay_body = ctx.expect_http(
        "booking_replay_after_transport_drop", replay, service="booking", status=200
    )
    ctx.check(
        "booking_replay_returns_same_id",
        replay_body.get("id") == booking_id,
        service="booking",
        call=replay,
        detail=f"replayed {replay_body!r}",
    )


def pvs_transport_drop_after_upstream(
    ctx: ScenarioContext, session: ForgeSession
) -> None:
    _booking_chaos, pvs_chaos, booking_admin, pvs_admin = _require_chaos(ctx, session)
    reset_services(ctx, session.booking, session.pvs)
    _reset_chaos(ctx, booking_admin, service="booking")
    _reset_chaos(ctx, pvs_admin, service="pvs")
    _arm_fault(
        ctx,
        pvs_admin,
        service="pvs",
        mode="drop_after_upstream",
        method="POST",
        path="/v1/tasks",
    )
    failed = _create_task(
        pvs_chaos,
        patient_id=SYNTH_PATIENT,
        title=SYNTH_TASK_TITLE,
        idempotency_key="scenario-pvs-drop-after",
    )
    ctx.expect_transport_error("pvs_post_upstream_transport_error", failed, service="pvs")
    types = _chaos_event_types(ctx, pvs_admin, service="pvs")
    ctx.check(
        "pvs_post_upstream_chaos_events",
        "upstream_completed" in types and "dropped_after_upstream" in types,
        service="pvs",
        detail=f"chaos event types: {types}",
    )
    task_id = _latest_committed_id(
        ctx,
        session.pvs,
        service="pvs",
        event_type="task_committed",
        detail_key="task_id",
    )
    ctx.remember("task_id", task_id)
    evidence = session.pvs.request("GET", f"/v1/tasks/{task_id}")
    evidence_body = ctx.expect_http(
        "pvs_evidence_after_transport_drop", evidence, service="pvs", status=200
    )
    ctx.check(
        "pvs_evidence_matches_request",
        evidence_body.get("id") == task_id and evidence_body.get("title") == SYNTH_TASK_TITLE,
        service="pvs",
        call=evidence,
    )
    replay = _create_task(
        pvs_chaos,
        patient_id=SYNTH_PATIENT,
        title=SYNTH_TASK_TITLE,
        idempotency_key="scenario-pvs-drop-after",
    )
    replay_body = ctx.expect_http(
        "pvs_replay_after_transport_drop", replay, service="pvs", status=200
    )
    ctx.check(
        "pvs_replay_returns_same_id",
        replay_body.get("id") == task_id,
        service="pvs",
        call=replay,
        detail=f"replayed {replay_body!r}",
    )


TRANSPORT_SCENARIOS: dict[str, Callable[[ScenarioContext, ForgeSession], None]] = {
    "booking-transport-drop-before-upstream": booking_transport_drop_before_upstream,
    "booking-transport-drop-after-upstream": booking_transport_drop_after_upstream,
    "pvs-transport-drop-after-upstream": pvs_transport_drop_after_upstream,
}

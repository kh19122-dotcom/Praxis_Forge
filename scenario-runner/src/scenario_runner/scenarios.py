from __future__ import annotations

from collections.abc import Callable

from scenario_runner.expect import ScenarioContext
from scenario_runner.http import ServiceClient

EXPECTED_BOOKING_SEED = "obj-001"
EXPECTED_PVS_SEED = "obj-002"
SYNTH_PATIENT = "synth-ada"
SYNTH_TASK_TITLE = "synth-chart-review"


def reset_services(ctx: ScenarioContext, booking: ServiceClient, pvs: ServiceClient) -> None:
    booking_reset = booking.request("POST", "/v1/admin/reset")
    body = ctx.expect_http("reset_booking", booking_reset, service="booking", status=200)
    ctx.check(
        "booking_seed_after_reset",
        body.get("status") == "reset" and body.get("seed") == EXPECTED_BOOKING_SEED,
        service="booking",
        call=booking_reset,
        detail=f"expected seed {EXPECTED_BOOKING_SEED}, got {body!r}",
    )
    pvs_reset = pvs.request("POST", "/v1/admin/reset")
    pvs_body = ctx.expect_http("reset_pvs", pvs_reset, service="pvs", status=200)
    ctx.check(
        "pvs_seed_after_reset",
        pvs_body.get("status") == "reset" and pvs_body.get("seed") == EXPECTED_PVS_SEED,
        service="pvs",
        call=pvs_reset,
        detail=f"expected seed {EXPECTED_PVS_SEED}, got {pvs_body!r}",
    )


def _event_types(events: list[dict]) -> list[str]:
    return [str(event.get("type")) for event in events]


def _events_for_trace(events_body: dict, trace_id: str) -> list[dict]:
    events = events_body.get("events")
    if not isinstance(events, list):
        return []
    return [event for event in events if event.get("trace_id") == trace_id]


def _remember_event_trace(
    ctx: ScenarioContext,
    client: ServiceClient,
    *,
    service: str,
    event_type: str,
    detail_key: str,
    detail_value: str,
    remember_as: str,
) -> str:
    listed = client.request("GET", "/v1/admin/events")
    body = ctx.expect_http(
        f"list_{service}_events_for_{remember_as}",
        listed,
        service=service,
        status=200,
    )
    events = body.get("events")
    matched = []
    if isinstance(events, list):
        matched = [
            event
            for event in events
            if event.get("type") == event_type
            and (event.get("details") or {}).get(detail_key) == detail_value
        ]
    ctx.check(
        f"{remember_as}_found",
        bool(matched) and isinstance(matched[-1].get("trace_id"), str),
        service=service,
        call=listed,
        detail=f"no {event_type} event for {detail_key}={detail_value}",
    )
    trace_id = str(matched[-1]["trace_id"])
    ctx.remember(remember_as, trace_id)
    ctx.check(
        f"{remember_as}_recorded",
        True,
        service=service,
        call=listed,
        trace_id=trace_id,
        detail=f"{remember_as}={trace_id}",
    )
    return trace_id


def _first_available_slot(ctx: ScenarioContext, booking: ServiceClient) -> dict:
    listed = booking.request("GET", "/v1/slots")
    body = ctx.expect_http("list_booking_slots", listed, service="booking", status=200)
    slots = body.get("slots")
    ctx.check(
        "booking_has_available_slot",
        isinstance(slots, list) and len(slots) > 0,
        service="booking",
        call=listed,
        detail="expected at least one available slot",
    )
    slot = slots[0]
    ctx.check(
        "slot_payload",
        isinstance(slot, dict) and isinstance(slot.get("id"), str) and slot.get("id"),
        service="booking",
        call=listed,
        detail=f"invalid slot payload: {slot!r}",
    )
    ctx.remember("slot_id", slot["id"])
    return slot


def _create_booking(
    booking: ServiceClient,
    *,
    slot_id: str,
    patient_ref: str,
    idempotency_key: str,
):
    return booking.request(
        "POST",
        "/v1/bookings",
        headers={"Idempotency-Key": idempotency_key},
        json={"slot_id": slot_id, "patient_ref": patient_ref},
    )


def _create_task(
    pvs: ServiceClient,
    *,
    patient_id: str,
    title: str,
    idempotency_key: str,
    priority: str = "normal",
):
    return pvs.request(
        "POST",
        "/v1/tasks",
        headers={"Idempotency-Key": idempotency_key},
        json={"patient_id": patient_id, "title": title, "priority": priority},
    )


def combined_happy_path(ctx: ScenarioContext, booking: ServiceClient, pvs: ServiceClient) -> None:
    reset_services(ctx, booking, pvs)

    booking_health = booking.request("GET", "/healthz")
    booking_body = ctx.expect_http("booking_healthz", booking_health, service="booking", status=200)
    ctx.check(
        "booking_health_identity",
        booking_body.get("service") == "fake-booking"
        and booking_body.get("seed") == EXPECTED_BOOKING_SEED,
        service="booking",
        call=booking_health,
        detail=f"unexpected booking health: {booking_body!r}",
    )

    pvs_health = pvs.request("GET", "/healthz")
    pvs_body = ctx.expect_http("pvs_healthz", pvs_health, service="pvs", status=200)
    ctx.check(
        "pvs_health_identity",
        pvs_body.get("service") == "fake-pvs" and pvs_body.get("seed") == EXPECTED_PVS_SEED,
        service="pvs",
        call=pvs_health,
        detail=f"unexpected pvs health: {pvs_body!r}",
    )

    slot = _first_available_slot(ctx, booking)
    created = _create_booking(
        booking,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-happy-booking",
    )
    booking_created = ctx.expect_http("create_booking", created, service="booking", status=201)
    booking_id = booking_created.get("id")
    ctx.check(
        "booking_created_payload",
        isinstance(booking_id, str)
        and booking_created.get("status") == "confirmed"
        and booking_created.get("patient_ref") == SYNTH_PATIENT
        and booking_created.get("slot_id") == slot["id"],
        service="booking",
        call=created,
        detail=f"unexpected booking: {booking_created!r}",
    )
    ctx.remember("booking_id", booking_id)
    _remember_event_trace(
        ctx,
        booking,
        service="booking",
        event_type="booking_committed",
        detail_key="booking_id",
        detail_value=booking_id,
        remember_as="booking_trace_id",
    )

    fetched = booking.request("GET", f"/v1/bookings/{booking_id}")
    fetched_body = ctx.expect_http("read_booking", fetched, service="booking", status=200)
    ctx.check(
        "booking_read_matches_create",
        fetched_body.get("id") == booking_id,
        service="booking",
        call=fetched,
    )

    remaining = booking.request("GET", "/v1/slots")
    remaining_body = ctx.expect_http(
        "list_slots_after_booking", remaining, service="booking", status=200
    )
    remaining_ids = {
        item.get("id") for item in remaining_body.get("slots", []) if isinstance(item, dict)
    }
    ctx.check(
        "booked_slot_no_longer_available",
        slot["id"] not in remaining_ids,
        service="booking",
        call=remaining,
        detail=f"slot {slot['id']} still listed as available",
    )

    patients = pvs.request("GET", "/v1/patients")
    patients_body = ctx.expect_http("list_patients", patients, service="pvs", status=200)
    patient_ids = {
        item.get("id") for item in patients_body.get("patients", []) if isinstance(item, dict)
    }
    ctx.check(
        "synthetic_patient_present",
        SYNTH_PATIENT in patient_ids,
        service="pvs",
        call=patients,
        detail=f"missing {SYNTH_PATIENT} in {sorted(patient_ids)}",
    )

    patient = pvs.request("GET", f"/v1/patients/{SYNTH_PATIENT}")
    ctx.expect_http("read_patient", patient, service="pvs", status=200)
    encounters = pvs.request("GET", f"/v1/patients/{SYNTH_PATIENT}/encounters")
    encounters_body = ctx.expect_http("list_encounters", encounters, service="pvs", status=200)
    ctx.check(
        "patient_has_encounters",
        isinstance(encounters_body.get("encounters"), list)
        and len(encounters_body["encounters"]) > 0,
        service="pvs",
        call=encounters,
    )

    created_task = _create_task(
        pvs,
        patient_id=SYNTH_PATIENT,
        title=SYNTH_TASK_TITLE,
        idempotency_key="scenario-happy-task",
    )
    task_body = ctx.expect_http("create_task", created_task, service="pvs", status=201)
    task_id = task_body.get("id")
    ctx.check(
        "task_created_payload",
        isinstance(task_id, str)
        and task_body.get("patient_id") == SYNTH_PATIENT
        and task_body.get("title") == SYNTH_TASK_TITLE
        and task_body.get("status") == "open",
        service="pvs",
        call=created_task,
        detail=f"unexpected task: {task_body!r}",
    )
    ctx.remember("task_id", task_id)
    _remember_event_trace(
        ctx,
        pvs,
        service="pvs",
        event_type="task_committed",
        detail_key="task_id",
        detail_value=task_id,
        remember_as="task_trace_id",
    )

    fetched_task = pvs.request("GET", f"/v1/tasks/{task_id}")
    fetched_task_body = ctx.expect_http("read_task", fetched_task, service="pvs", status=200)
    ctx.check(
        "task_read_matches_create",
        fetched_task_body.get("id") == task_id,
        service="pvs",
        call=fetched_task,
    )


def booking_fail_before_commit(
    ctx: ScenarioContext, booking: ServiceClient, pvs: ServiceClient
) -> None:
    reset_services(ctx, booking, pvs)
    slot = _first_available_slot(ctx, booking)
    fault = booking.request(
        "PUT",
        "/v1/admin/faults",
        json={"mode": "fail_before_commit", "remaining": 1},
    )
    fault_body = ctx.expect_http(
        "inject_booking_fail_before_commit",
        fault,
        service="booking",
        status=200,
    )
    ctx.check(
        "booking_fault_armed",
        fault_body.get("mode") == "fail_before_commit",
        service="booking",
        call=fault,
    )

    failed = _create_booking(
        booking,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-booking-fail",
    )
    failed_body = ctx.expect_http(
        "booking_fail_before_commit_response",
        failed,
        service="booking",
        status=503,
    )
    ctx.check(
        "booking_fail_has_no_remote_effect_flag",
        failed_body.get("error") == "fail_before_commit"
        and (failed_body.get("details") or {}).get("committed") is False,
        service="booking",
        call=failed,
        detail=f"unexpected failure payload: {failed_body!r}",
    )
    ctx.check(
        "booking_fail_has_trace",
        isinstance(failed.trace_id, str) and failed.trace_id.startswith("tr_"),
        service="booking",
        call=failed,
    )
    ctx.remember("booking_fail_trace_id", failed.trace_id or "")

    events = booking.request("GET", "/v1/admin/events", params={"trace_id": failed.trace_id or ""})
    events_body = ctx.expect_http("booking_fail_events", events, service="booking", status=200)
    types = _event_types(_events_for_trace(events_body, failed.trace_id or ""))
    ctx.check(
        "booking_fail_events_show_commit_skipped",
        "commit_skipped" in types and "booking_committed" not in types,
        service="booking",
        call=events,
        detail=f"event types: {types}",
    )

    still_open = booking.request("GET", "/v1/slots")
    still_open_body = ctx.expect_http(
        "slots_after_booking_fail", still_open, service="booking", status=200
    )
    available_ids = {
        item.get("id") for item in still_open_body.get("slots", []) if isinstance(item, dict)
    }
    ctx.check(
        "failed_booking_left_slot_available",
        slot["id"] in available_ids,
        service="booking",
        call=still_open,
        detail=f"slot {slot['id']} missing after fail-before-commit",
    )

    retry = _create_booking(
        booking,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-booking-fail",
    )
    retry_body = ctx.expect_http("booking_retry_after_fail", retry, service="booking", status=201)
    booking_id = retry_body.get("id")
    ctx.check(
        "booking_retry_committed",
        isinstance(booking_id, str) and retry_body.get("slot_id") == slot["id"],
        service="booking",
        call=retry,
    )
    ctx.remember("booking_id", str(booking_id))


def booking_ambiguous_recovery(
    ctx: ScenarioContext, booking: ServiceClient, pvs: ServiceClient
) -> None:
    reset_services(ctx, booking, pvs)
    slot = _first_available_slot(ctx, booking)
    fault = booking.request(
        "PUT",
        "/v1/admin/faults",
        json={"mode": "ambiguous", "delay_ms": 5, "remaining": 1},
    )
    ctx.expect_http("inject_booking_ambiguous", fault, service="booking", status=200)

    ambiguous = _create_booking(
        booking,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-booking-ambiguous",
    )
    body = ctx.expect_http(
        "booking_ambiguous_client_outcome",
        ambiguous,
        service="booking",
        status=504,
    )
    ctx.check(
        "booking_ambiguous_committed_unknown",
        body.get("error") == "ambiguous_outcome"
        and (body.get("details") or {}).get("committed") is None,
        service="booking",
        call=ambiguous,
        detail=f"unexpected ambiguous payload: {body!r}",
    )
    ctx.check(
        "booking_ambiguous_has_trace",
        isinstance(ambiguous.trace_id, str) and ambiguous.trace_id.startswith("tr_"),
        service="booking",
        call=ambiguous,
    )
    ctx.remember("booking_ambiguous_trace_id", ambiguous.trace_id or "")

    events = booking.request(
        "GET", "/v1/admin/events", params={"trace_id": ambiguous.trace_id or ""}
    )
    events_body = ctx.expect_http("booking_ambiguous_events", events, service="booking", status=200)
    matched = _events_for_trace(events_body, ambiguous.trace_id or "")
    types = _event_types(matched)
    ctx.check(
        "booking_ambiguous_events_show_commit_and_suppressed_response",
        "booking_committed" in types and "response_suppressed" in types,
        service="booking",
        call=events,
        detail=f"event types: {types}",
    )
    committed = next(event for event in matched if event.get("type") == "booking_committed")
    booking_id = (committed.get("details") or {}).get("booking_id")
    ctx.check(
        "booking_id_recovered_from_events",
        isinstance(booking_id, str) and booking_id,
        service="booking",
        call=events,
        detail=f"committed event: {committed!r}",
    )
    ctx.remember("booking_id", booking_id)

    evidence = booking.request("GET", f"/v1/bookings/{booking_id}")
    evidence_body = ctx.expect_http(
        "booking_evidence_read", evidence, service="booking", status=200
    )
    ctx.check(
        "booking_evidence_matches_request",
        evidence_body.get("id") == booking_id and evidence_body.get("slot_id") == slot["id"],
        service="booking",
        call=evidence,
    )

    remaining = booking.request("GET", "/v1/slots")
    remaining_body = ctx.expect_http(
        "slots_after_ambiguous_booking", remaining, service="booking", status=200
    )
    remaining_ids = {
        item.get("id") for item in remaining_body.get("slots", []) if isinstance(item, dict)
    }
    ctx.check(
        "ambiguous_booking_consumed_slot",
        slot["id"] not in remaining_ids,
        service="booking",
        call=remaining,
    )

    replay = _create_booking(
        booking,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-booking-ambiguous",
    )
    replay_body = ctx.expect_http("booking_ambiguous_replay", replay, service="booking", status=200)
    ctx.check(
        "booking_replay_returns_same_id",
        replay_body.get("id") == booking_id,
        service="booking",
        call=replay,
        detail=f"replayed {replay_body!r}",
    )


def pvs_fail_before_commit(
    ctx: ScenarioContext, booking: ServiceClient, pvs: ServiceClient
) -> None:
    reset_services(ctx, booking, pvs)
    fault = pvs.request(
        "PUT",
        "/v1/admin/faults",
        json={"mode": "fail_before_commit", "remaining": 1},
    )
    ctx.expect_http("inject_pvs_fail_before_commit", fault, service="pvs", status=200)

    failed = _create_task(
        pvs,
        patient_id=SYNTH_PATIENT,
        title=SYNTH_TASK_TITLE,
        idempotency_key="scenario-pvs-fail",
    )
    failed_body = ctx.expect_http(
        "pvs_fail_before_commit_response", failed, service="pvs", status=503
    )
    ctx.check(
        "pvs_fail_has_no_remote_effect_flag",
        failed_body.get("error") == "fail_before_commit"
        and (failed_body.get("details") or {}).get("committed") is False,
        service="pvs",
        call=failed,
        detail=f"unexpected failure payload: {failed_body!r}",
    )
    ctx.remember("pvs_fail_trace_id", failed.trace_id or "")

    events = pvs.request("GET", "/v1/admin/events", params={"trace_id": failed.trace_id or ""})
    events_body = ctx.expect_http("pvs_fail_events", events, service="pvs", status=200)
    types = _event_types(_events_for_trace(events_body, failed.trace_id or ""))
    ctx.check(
        "pvs_fail_events_show_commit_skipped",
        "commit_skipped" in types and "task_committed" not in types,
        service="pvs",
        call=events,
        detail=f"event types: {types}",
    )

    retry = _create_task(
        pvs,
        patient_id=SYNTH_PATIENT,
        title=SYNTH_TASK_TITLE,
        idempotency_key="scenario-pvs-fail",
    )
    retry_body = ctx.expect_http("pvs_retry_after_fail", retry, service="pvs", status=201)
    task_id = retry_body.get("id")
    ctx.check(
        "pvs_retry_committed",
        isinstance(task_id, str) and retry_body.get("patient_id") == SYNTH_PATIENT,
        service="pvs",
        call=retry,
    )
    ctx.remember("task_id", str(task_id))
    evidence = pvs.request("GET", f"/v1/tasks/{task_id}")
    ctx.expect_http("pvs_retry_task_readable", evidence, service="pvs", status=200)


def pvs_ambiguous_recovery(
    ctx: ScenarioContext, booking: ServiceClient, pvs: ServiceClient
) -> None:
    reset_services(ctx, booking, pvs)
    fault = pvs.request(
        "PUT",
        "/v1/admin/faults",
        json={"mode": "ambiguous", "delay_ms": 5, "remaining": 1},
    )
    ctx.expect_http("inject_pvs_ambiguous", fault, service="pvs", status=200)

    ambiguous = _create_task(
        pvs,
        patient_id=SYNTH_PATIENT,
        title=SYNTH_TASK_TITLE,
        idempotency_key="scenario-pvs-ambiguous",
    )
    body = ctx.expect_http("pvs_ambiguous_client_outcome", ambiguous, service="pvs", status=504)
    ctx.check(
        "pvs_ambiguous_committed_unknown",
        body.get("error") == "ambiguous_outcome"
        and (body.get("details") or {}).get("committed") is None,
        service="pvs",
        call=ambiguous,
        detail=f"unexpected ambiguous payload: {body!r}",
    )
    ctx.remember("pvs_ambiguous_trace_id", ambiguous.trace_id or "")

    events = pvs.request("GET", "/v1/admin/events", params={"trace_id": ambiguous.trace_id or ""})
    events_body = ctx.expect_http("pvs_ambiguous_events", events, service="pvs", status=200)
    matched = _events_for_trace(events_body, ambiguous.trace_id or "")
    types = _event_types(matched)
    ctx.check(
        "pvs_ambiguous_events_show_commit_and_suppressed_response",
        "task_committed" in types and "response_suppressed" in types,
        service="pvs",
        call=events,
        detail=f"event types: {types}",
    )
    committed = next(event for event in matched if event.get("type") == "task_committed")
    task_id = (committed.get("details") or {}).get("task_id")
    ctx.check(
        "task_id_recovered_from_events",
        isinstance(task_id, str) and task_id,
        service="pvs",
        call=events,
        detail=f"committed event: {committed!r}",
    )
    ctx.remember("task_id", task_id)

    evidence = pvs.request("GET", f"/v1/tasks/{task_id}")
    evidence_body = ctx.expect_http("pvs_evidence_read", evidence, service="pvs", status=200)
    ctx.check(
        "pvs_evidence_matches_request",
        evidence_body.get("id") == task_id and evidence_body.get("title") == SYNTH_TASK_TITLE,
        service="pvs",
        call=evidence,
    )

    replay = _create_task(
        pvs,
        patient_id=SYNTH_PATIENT,
        title=SYNTH_TASK_TITLE,
        idempotency_key="scenario-pvs-ambiguous",
    )
    replay_body = ctx.expect_http("pvs_ambiguous_replay", replay, service="pvs", status=200)
    ctx.check(
        "pvs_replay_returns_same_id",
        replay_body.get("id") == task_id,
        service="pvs",
        call=replay,
        detail=f"replayed {replay_body!r}",
    )


def conflict_idempotency(ctx: ScenarioContext, booking: ServiceClient, pvs: ServiceClient) -> None:
    reset_services(ctx, booking, pvs)
    slot = _first_available_slot(ctx, booking)
    first = _create_booking(
        booking,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-conflict-a",
    )
    first_body = ctx.expect_http("conflict_first_booking", first, service="booking", status=201)
    booking_id = first_body.get("id")
    ctx.check("conflict_booking_id", isinstance(booking_id, str), service="booking", call=first)
    ctx.remember("booking_id", str(booking_id))

    conflict = _create_booking(
        booking,
        slot_id=slot["id"],
        patient_ref="synth-ben",
        idempotency_key="scenario-conflict-b",
    )
    conflict_body = ctx.expect_http("slot_conflict", conflict, service="booking", status=409)
    ctx.check(
        "slot_conflict_payload",
        conflict_body.get("error") == "slot_conflict"
        and (conflict_body.get("details") or {}).get("existing_booking_id") == booking_id
        and (conflict_body.get("details") or {}).get("committed") is False,
        service="booking",
        call=conflict,
        detail=f"unexpected conflict payload: {conflict_body!r}",
    )
    if conflict.trace_id:
        ctx.remember("slot_conflict_trace_id", conflict.trace_id)

    replay = _create_booking(
        booking,
        slot_id=slot["id"],
        patient_ref=SYNTH_PATIENT,
        idempotency_key="scenario-conflict-a",
    )
    replay_body = ctx.expect_http(
        "idempotent_booking_replay", replay, service="booking", status=200
    )
    ctx.check(
        "idempotent_booking_same_id",
        replay_body.get("id") == booking_id,
        service="booking",
        call=replay,
    )

    reused = _create_booking(
        booking,
        slot_id=slot["id"],
        patient_ref="synth-ben",
        idempotency_key="scenario-conflict-a",
    )
    reused_body = ctx.expect_http(
        "booking_idempotency_conflict", reused, service="booking", status=409
    )
    ctx.check(
        "booking_idempotency_conflict_payload",
        reused_body.get("error") == "idempotency_conflict"
        and (reused_body.get("details") or {}).get("committed") is False,
        service="booking",
        call=reused,
        detail=f"unexpected idempotency conflict: {reused_body!r}",
    )

    created_task = _create_task(
        pvs,
        patient_id=SYNTH_PATIENT,
        title=SYNTH_TASK_TITLE,
        idempotency_key="scenario-conflict-task",
    )
    task_body = ctx.expect_http("conflict_first_task", created_task, service="pvs", status=201)
    task_id = task_body.get("id")
    ctx.check("conflict_task_id", isinstance(task_id, str), service="pvs", call=created_task)
    ctx.remember("task_id", str(task_id))

    task_replay = _create_task(
        pvs,
        patient_id=SYNTH_PATIENT,
        title=SYNTH_TASK_TITLE,
        idempotency_key="scenario-conflict-task",
    )
    task_replay_body = ctx.expect_http(
        "idempotent_task_replay", task_replay, service="pvs", status=200
    )
    ctx.check(
        "idempotent_task_same_id",
        task_replay_body.get("id") == task_id,
        service="pvs",
        call=task_replay,
    )

    task_reused = _create_task(
        pvs,
        patient_id="synth-ben",
        title="synth-note-two",
        idempotency_key="scenario-conflict-task",
        priority="high",
    )
    task_reused_body = ctx.expect_http(
        "pvs_idempotency_conflict", task_reused, service="pvs", status=409
    )
    ctx.check(
        "pvs_idempotency_conflict_payload",
        task_reused_body.get("error") == "idempotency_conflict"
        and (task_reused_body.get("details") or {}).get("task_id") == task_id
        and (task_reused_body.get("details") or {}).get("committed") is False,
        service="pvs",
        call=task_reused,
        detail=f"unexpected task conflict: {task_reused_body!r}",
    )


SCENARIOS: dict[str, Callable[[ScenarioContext, ServiceClient, ServiceClient], None]] = {
    "combined-happy-path": combined_happy_path,
    "booking-fail-before-commit": booking_fail_before_commit,
    "booking-ambiguous-recovery": booking_ambiguous_recovery,
    "pvs-fail-before-commit": pvs_fail_before_commit,
    "pvs-ambiguous-recovery": pvs_ambiguous_recovery,
    "conflict-idempotency": conflict_idempotency,
}

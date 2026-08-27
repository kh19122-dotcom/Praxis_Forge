#!/usr/bin/env python3
"""Host-side restart-recovery gate against real Compose containers.

Uses Docker Compose from the host. Does not run inside scenario-runner,
simulator, proxy, or contract-check containers and does not mount the
Docker socket into those containers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yml"
PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "praxis-forge-restart-recovery")
OVERRIDE = os.environ.get("FORGE_COMPOSE_OVERRIDE", "").strip()
SERVICES = ("fake-booking", "fake-pvs", "chaos-booking", "chaos-pvs")

BOOKING = os.environ.get("FORGE_BOOKING_URL", "http://127.0.0.1:8080").rstrip("/")
PVS = os.environ.get("FORGE_PVS_URL", "http://127.0.0.1:8081").rstrip("/")
BOOKING_CHAOS = os.environ.get("FORGE_BOOKING_CHAOS_URL", "http://127.0.0.1:8090").rstrip("/")
PVS_CHAOS = os.environ.get("FORGE_PVS_CHAOS_URL", "http://127.0.0.1:8091").rstrip("/")
BOOKING_CHAOS_ADMIN = os.environ.get(
    "FORGE_BOOKING_CHAOS_ADMIN_URL",
    "http://127.0.0.1:8092",
).rstrip("/")
PVS_CHAOS_ADMIN = os.environ.get(
    "FORGE_PVS_CHAOS_ADMIN_URL",
    "http://127.0.0.1:8093",
).rstrip("/")

SYNTH_PATIENT = "synth-ada"
SYNTH_TITLE = "synth-chart-review"


class CheckFailure(RuntimeError):
    pass


def compose(*args: str) -> None:
    command = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE)]
    if OVERRIDE:
        command.extend(["-f", OVERRIDE])
    command.extend(args)
    result = subprocess.run(command, cwd=ROOT, check=False, text=True)
    if result.returncode != 0:
        raise CheckFailure(f"command failed ({result.returncode}): {' '.join(command)}")


def request(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int | None, Any, str | None]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json"}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            parsed: Any
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except json.JSONDecodeError:
                parsed = raw.decode("utf-8", errors="replace")
            return response.status, parsed, None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            parsed = raw.decode("utf-8", errors="replace")
        return exc.code, parsed, None
    except urllib.error.URLError as exc:
        reason = exc.reason if exc.reason is not None else exc
        return None, None, f"{type(reason).__name__}: {reason}"
    except (TimeoutError, ConnectionError, OSError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def wait_health(url: str, *, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last = "not attempted"
    while time.time() < deadline:
        status, body, error = request("GET", url, timeout=3.0)
        if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
            return
        last = error or f"status={status} body={body!r}"
        time.sleep(0.5)
    raise CheckFailure(f"timeout waiting for {url}: {last}")


def wait_stack() -> None:
    wait_health(f"{BOOKING}/healthz")
    wait_health(f"{PVS}/healthz")
    wait_health(f"{BOOKING_CHAOS}/healthz")
    wait_health(f"{PVS_CHAOS}/healthz")
    wait_health(f"{BOOKING_CHAOS_ADMIN}/healthz")
    wait_health(f"{PVS_CHAOS_ADMIN}/healthz")


def expect_status(
    label: str,
    status: int | None,
    body: Any,
    error: str | None,
    expected: int,
) -> Any:
    if status != expected:
        raise CheckFailure(
            f"{label}: expected HTTP {expected}, got {status} error={error!r} body={body!r}"
        )
    return body


def expect_transport_error(label: str, status: int | None, error: str | None) -> None:
    if status is not None or not error:
        raise CheckFailure(
            f"{label}: expected transport error, got status={status} error={error!r}"
        )


def first_slot() -> dict[str, Any]:
    status, body, error = request("GET", f"{BOOKING}/v1/slots")
    payload = expect_status("list_slots", status, body, error, 200)
    slots = payload.get("slots") if isinstance(payload, dict) else None
    if not isinstance(slots, list) or not slots or not isinstance(slots[0], dict):
        raise CheckFailure(f"no slots available: {payload!r}")
    return slots[0]


def create_booking(
    base: str,
    *,
    slot_id: str,
    key: str,
    patient_ref: str = SYNTH_PATIENT,
) -> tuple[int | None, Any, str | None]:
    return request(
        "POST",
        f"{base}/v1/bookings",
        payload={"slot_id": slot_id, "patient_ref": patient_ref},
        headers={"Idempotency-Key": key},
    )


def create_task(
    base: str,
    *,
    key: str,
    patient_id: str = SYNTH_PATIENT,
    title: str = SYNTH_TITLE,
) -> tuple[int | None, Any, str | None]:
    return request(
        "POST",
        f"{base}/v1/tasks",
        payload={"patient_id": patient_id, "title": title, "priority": "normal"},
        headers={"Idempotency-Key": key},
    )


def reset_simulators() -> None:
    for name, base in (("booking", BOOKING), ("pvs", PVS)):
        status, body, error = request("POST", f"{base}/v1/admin/reset")
        payload = expect_status(f"reset_{name}", status, body, error, 200)
        if not isinstance(payload, dict) or payload.get("status") != "reset":
            raise CheckFailure(f"reset_{name}: unexpected body {payload!r}")
    for name, base in (
        ("booking_chaos", BOOKING_CHAOS_ADMIN),
        ("pvs_chaos", PVS_CHAOS_ADMIN),
    ):
        status, body, error = request("POST", f"{base}/v1/admin/reset")
        payload = expect_status(f"reset_{name}", status, body, error, 200)
        if not isinstance(payload, dict) or payload.get("status") != "reset":
            raise CheckFailure(f"reset_{name}: unexpected body {payload!r}")


def restart_service(service: str) -> None:
    compose("restart", service)
    wait_stack()


def arm_chaos(admin: str, *, mode: str, method: str, path: str) -> None:
    status, body, error = request(
        "PUT",
        f"{admin}/v1/admin/faults",
        payload={
            "mode": mode,
            "remaining": 1,
            "method": method,
            "path": path,
            "delay_ms": 50,
        },
    )
    payload = expect_status(f"arm_{mode}_{path}", status, body, error, 200)
    if not isinstance(payload, dict) or payload.get("mode") != mode:
        raise CheckFailure(f"arm_{mode}: unexpected body {payload!r}")


def event_types(base: str) -> list[str]:
    status, body, error = request("GET", f"{base}/v1/admin/events")
    payload = expect_status(f"events_{base}", status, body, error, 200)
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise CheckFailure(f"events_{base}: unexpected body {payload!r}")
    return [str(event.get("type")) for event in events if isinstance(event, dict)]


def latest_committed_id(base: str, event_type: str, detail_key: str) -> str:
    status, body, error = request("GET", f"{base}/v1/admin/events")
    payload = expect_status(f"committed_events_{base}", status, body, error, 200)
    events = payload.get("events") if isinstance(payload, dict) else None
    matched = []
    if isinstance(events, list):
        matched = [
            event
            for event in events
            if isinstance(event, dict) and event.get("type") == event_type
        ]
    if not matched:
        raise CheckFailure(f"no {event_type} event at {base}: {payload!r}")
    value = (matched[-1].get("details") or {}).get(detail_key)
    if not isinstance(value, str) or not value:
        raise CheckFailure(f"{event_type} missing {detail_key}: {matched[-1]!r}")
    return value


def slot_available(slot_id: str) -> bool:
    status, body, error = request("GET", f"{BOOKING}/v1/slots")
    payload = expect_status("slots_available", status, body, error, 200)
    slots = payload.get("slots") if isinstance(payload, dict) else []
    return any(isinstance(item, dict) and item.get("id") == slot_id for item in slots)


def check(label: str) -> None:
    print(f"PASS {label}", flush=True)


def run_checks() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    if "docker.sock" in compose_text or "/var/run/docker.sock" in compose_text:
        raise CheckFailure("docker.sock appears in docker-compose.yml")
    if "FORGE_STATE_PATH: /var/lib/forge/state.json" not in compose_text:
        raise CheckFailure("durable FORGE_STATE_PATH missing from compose")
    check("compose_has_no_docker_socket")

    reset_simulators()
    slot = first_slot()
    status, body, error = create_booking(
        BOOKING, slot_id=slot["id"], key="restart-booking-normal"
    )
    created = expect_status("create_booking", status, body, error, 201)
    booking_id = created["id"]
    restart_service("fake-booking")
    status, body, error = request("GET", f"{BOOKING}/v1/bookings/{booking_id}")
    fetched = expect_status("booking_after_restart", status, body, error, 200)
    if fetched.get("id") != booking_id or fetched.get("slot_id") != slot["id"]:
        raise CheckFailure(f"booking changed after restart: {fetched!r}")
    check("booking_survives_restart")

    status, body, error = create_booking(
        BOOKING, slot_id=slot["id"], key="restart-booking-normal"
    )
    replay = expect_status("booking_idempotent_after_restart", status, body, error, 200)
    if replay.get("id") != booking_id:
        raise CheckFailure(f"booking replay mismatch: {replay!r}")
    check("booking_idempotent_replay_after_restart")

    status, body, error = create_booking(
        BOOKING,
        slot_id=slot["id"],
        key="restart-booking-conflict",
        patient_ref="synth-ben",
    )
    conflict = expect_status(
        "booking_slot_conflict_after_restart", status, body, error, 409
    )
    if not isinstance(conflict, dict) or conflict.get("error") != "slot_conflict":
        raise CheckFailure(f"expected slot_conflict: {conflict!r}")
    if slot_available(slot["id"]):
        raise CheckFailure(f"slot {slot['id']} still available after restart")
    check("booking_slot_consumption_survives_restart")

    types = event_types(BOOKING)
    if "booking_committed" not in types:
        raise CheckFailure(f"booking evidence missing after restart: {types}")
    check("booking_events_survive_restart")

    reset_simulators()
    status, body, error = create_task(PVS, key="restart-pvs-normal")
    created_task = expect_status("create_task", status, body, error, 201)
    task_id = created_task["id"]
    restart_service("fake-pvs")
    status, body, error = request("GET", f"{PVS}/v1/tasks/{task_id}")
    fetched_task = expect_status("task_after_restart", status, body, error, 200)
    if fetched_task.get("id") != task_id or fetched_task.get("title") != SYNTH_TITLE:
        raise CheckFailure(f"task changed after restart: {fetched_task!r}")
    check("pvs_task_survives_restart")

    status, body, error = create_task(PVS, key="restart-pvs-normal")
    replay_task = expect_status("pvs_idempotent_after_restart", status, body, error, 200)
    if replay_task.get("id") != task_id:
        raise CheckFailure(f"task replay mismatch: {replay_task!r}")
    check("pvs_idempotent_replay_after_restart")

    types = event_types(PVS)
    if "task_committed" not in types:
        raise CheckFailure(f"pvs evidence missing after restart: {types}")
    check("pvs_events_survive_restart")

    reset_simulators()
    slot = first_slot()
    arm_chaos(
        BOOKING_CHAOS_ADMIN,
        mode="drop_after_upstream",
        method="POST",
        path="/v1/bookings",
    )
    status, body, error = create_booking(
        BOOKING_CHAOS,
        slot_id=slot["id"],
        key="restart-booking-drop-after",
    )
    expect_transport_error("booking_drop_after_upstream", status, error)
    restart_service("fake-booking")
    recovered_booking_id = latest_committed_id(BOOKING, "booking_committed", "booking_id")
    status, body, error = request("GET", f"{BOOKING}/v1/bookings/{recovered_booking_id}")
    evidence = expect_status(
        "booking_evidence_after_drop_restart", status, body, error, 200
    )
    if evidence.get("id") != recovered_booking_id or evidence.get("slot_id") != slot["id"]:
        raise CheckFailure(f"booking evidence mismatch: {evidence!r}")
    status, body, error = create_booking(
        BOOKING,
        slot_id=slot["id"],
        key="restart-booking-drop-after",
    )
    replay = expect_status("booking_retry_after_drop_restart", status, body, error, 200)
    if replay.get("id") != recovered_booking_id:
        raise CheckFailure(f"booking retry mismatch: {replay!r}")
    check("booking_drop_after_upstream_survives_restart")

    reset_simulators()
    arm_chaos(PVS_CHAOS_ADMIN, mode="drop_after_upstream", method="POST", path="/v1/tasks")
    status, body, error = create_task(PVS_CHAOS, key="restart-pvs-drop-after")
    expect_transport_error("pvs_drop_after_upstream", status, error)
    restart_service("fake-pvs")
    recovered_task_id = latest_committed_id(PVS, "task_committed", "task_id")
    status, body, error = request("GET", f"{PVS}/v1/tasks/{recovered_task_id}")
    evidence = expect_status("pvs_evidence_after_drop_restart", status, body, error, 200)
    if evidence.get("id") != recovered_task_id or evidence.get("title") != SYNTH_TITLE:
        raise CheckFailure(f"pvs evidence mismatch: {evidence!r}")
    status, body, error = create_task(PVS, key="restart-pvs-drop-after")
    replay_task = expect_status("pvs_retry_after_drop_restart", status, body, error, 200)
    if replay_task.get("id") != recovered_task_id:
        raise CheckFailure(f"pvs retry mismatch: {replay_task!r}")
    check("pvs_drop_after_upstream_survives_restart")

    reset_simulators()
    slot = first_slot()
    arm_chaos(
        BOOKING_CHAOS_ADMIN,
        mode="drop_before_upstream",
        method="POST",
        path="/v1/bookings",
    )
    status, body, error = create_booking(
        BOOKING_CHAOS,
        slot_id=slot["id"],
        key="restart-booking-drop-before",
    )
    expect_transport_error("booking_drop_before_upstream", status, error)
    restart_service("fake-booking")
    if "booking_committed" in event_types(BOOKING):
        raise CheckFailure("drop_before_upstream left a committed booking")
    if not slot_available(slot["id"]):
        raise CheckFailure("drop_before_upstream consumed the slot")
    status, body, error = create_booking(
        BOOKING,
        slot_id=slot["id"],
        key="restart-booking-drop-before",
    )
    retry = expect_status("booking_create_after_drop_before", status, body, error, 201)
    if retry.get("slot_id") != slot["id"]:
        raise CheckFailure(f"unexpected retry booking: {retry!r}")
    check("drop_before_upstream_leaves_no_durable_effect")

    reset_simulators()
    slot = first_slot()
    status, body, error = create_booking(
        BOOKING, slot_id=slot["id"], key="restart-reset-booking"
    )
    created = expect_status("create_booking_for_reset", status, body, error, 201)
    booking_id = created["id"]
    status, body, error = create_task(PVS, key="restart-reset-pvs")
    created_task = expect_status("create_task_for_reset", status, body, error, 201)
    task_id = created_task["id"]
    restart_service("fake-booking")
    restart_service("fake-pvs")
    status, body, error = request("GET", f"{BOOKING}/v1/bookings/{booking_id}")
    expect_status("booking_present_before_admin_reset", status, body, error, 200)
    status, body, error = request("GET", f"{PVS}/v1/tasks/{task_id}")
    expect_status("task_present_before_admin_reset", status, body, error, 200)
    reset_simulators()
    status, body, error = request("GET", f"{BOOKING}/v1/bookings/{booking_id}")
    if status != 404:
        raise CheckFailure(f"admin reset left booking {booking_id}: {status} {body!r}")
    status, body, error = request("GET", f"{PVS}/v1/tasks/{task_id}")
    if status != 404:
        raise CheckFailure(f"admin reset left task {task_id}: {status} {body!r}")
    if not slot_available(slot["id"]):
        raise CheckFailure("admin reset did not restore slot catalog")
    restart_service("fake-booking")
    restart_service("fake-pvs")
    status, body, error = request("GET", f"{BOOKING}/v1/bookings/{booking_id}")
    if status != 404:
        raise CheckFailure(
            f"reset state did not survive restart: booking {status} {body!r}"
        )
    status, body, error = request("GET", f"{PVS}/v1/tasks/{task_id}")
    if status != 404:
        raise CheckFailure(f"reset state did not survive restart: task {status} {body!r}")
    check("admin_reset_clears_durable_state")

    reset_simulators()
    status, body, error = request(
        "PUT",
        f"{BOOKING}/v1/admin/faults",
        payload={"mode": "fail_before_commit", "remaining": 3},
    )
    fault = expect_status("arm_booking_fault", status, body, error, 200)
    if not isinstance(fault, dict) or fault.get("mode") != "fail_before_commit":
        raise CheckFailure(f"failed to arm booking fault: {fault!r}")
    status, body, error = request(
        "PUT",
        f"{PVS}/v1/admin/faults",
        payload={"mode": "ambiguous", "remaining": 2},
    )
    fault = expect_status("arm_pvs_fault", status, body, error, 200)
    if not isinstance(fault, dict) or fault.get("mode") != "ambiguous":
        raise CheckFailure(f"failed to arm pvs fault: {fault!r}")
    restart_service("fake-booking")
    restart_service("fake-pvs")
    status, body, error = request("GET", f"{BOOKING}/v1/admin/faults")
    booking_fault = expect_status("booking_fault_after_restart", status, body, error, 200)
    if booking_fault.get("mode") != "fail_before_commit" or booking_fault.get("remaining") != 3:
        raise CheckFailure(f"booking fault did not survive restart: {booking_fault!r}")
    status, body, error = request("GET", f"{PVS}/v1/admin/faults")
    pvs_fault = expect_status("pvs_fault_after_restart", status, body, error, 200)
    if pvs_fault.get("mode") != "ambiguous" or pvs_fault.get("remaining") != 2:
        raise CheckFailure(f"pvs fault did not survive restart: {pvs_fault!r}")
    slot = first_slot()
    status, body, error = create_booking(
        BOOKING, slot_id=slot["id"], key="restart-fault-survives"
    )
    expect_status("booking_fault_still_armed_after_restart", status, body, error, 503)
    status, body, error = create_task(PVS, key="restart-fault-survives")
    expect_status("pvs_fault_still_armed_after_restart", status, body, error, 504)
    check("configured_fault_survives_restart")


def main() -> int:
    try:
        compose("down", "--volumes", "--remove-orphans")
        compose("up", "-d", "--build", "--wait", *SERVICES)
        wait_stack()
        run_checks()
        print("restart-recovery: pass")
        return 0
    except CheckFailure as exc:
        print(f"restart-recovery: fail: {exc}", file=sys.stderr)
        return 1
    finally:
        compose("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    raise SystemExit(main())

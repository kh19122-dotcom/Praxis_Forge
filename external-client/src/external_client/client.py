from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BOOKING_URL = "http://chaos-booking:8090"
DEFAULT_PVS_URL = "http://chaos-pvs:8091"
DEFAULT_BOOKING_CHAOS_ADMIN_URL = "http://chaos-booking:8092"
DEFAULT_PVS_CHAOS_ADMIN_URL = "http://chaos-pvs:8093"
DEFAULT_LAB_DNS_NAMES = ("chaos-booking", "chaos-pvs", "forge-booking", "forge-pvs")
DEFAULT_FORBIDDEN_DNS_NAMES = ("fake-booking", "fake-pvs")
SYNTH_PATIENT = "synth-ada"
SYNTH_TITLE = "synth-chart-review"
SCHEMA = "praxis-forge.external-client-smoke.v1"


class SmokeFailure(RuntimeError):
    pass


def _csv_names(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return default
    names = tuple(item.strip() for item in raw.split(",") if item.strip())
    return names or default


def settings_from_env() -> dict[str, Any]:
    return {
        "booking_url": os.environ.get("FORGE_BOOKING_URL", DEFAULT_BOOKING_URL).rstrip("/"),
        "pvs_url": os.environ.get("FORGE_PVS_URL", DEFAULT_PVS_URL).rstrip("/"),
        "booking_chaos_admin_url": os.environ.get(
            "FORGE_BOOKING_CHAOS_ADMIN_URL",
            DEFAULT_BOOKING_CHAOS_ADMIN_URL,
        ).rstrip("/"),
        "pvs_chaos_admin_url": os.environ.get(
            "FORGE_PVS_CHAOS_ADMIN_URL",
            DEFAULT_PVS_CHAOS_ADMIN_URL,
        ).rstrip("/"),
        "lab_dns_names": _csv_names(os.environ.get("FORGE_LAB_DNS_NAMES"), DEFAULT_LAB_DNS_NAMES),
        "forbidden_dns_names": _csv_names(
            os.environ.get("FORGE_LAB_FORBIDDEN_DNS_NAMES"),
            DEFAULT_FORBIDDEN_DNS_NAMES,
        ),
        "skip_dns": os.environ.get("FORGE_SKIP_DNS", "").strip().lower() in {"1", "true", "yes"},
        "timeout": float(os.environ.get("FORGE_HTTP_TIMEOUT", "10")),
    }


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


def resolve_name(name: str) -> str:
    infos = socket.getaddrinfo(name, None)
    if not infos:
        raise OSError(f"no addresses for {name}")
    return str(infos[0][4][0])


def check_dns(names: tuple[str, ...], forbidden: tuple[str, ...]) -> dict[str, Any]:
    resolved: dict[str, str] = {}
    for name in names:
        try:
            resolved[name] = resolve_name(name)
        except OSError as exc:
            raise SmokeFailure(f"lab DNS name {name} did not resolve: {exc}") from exc
    blocked: dict[str, str] = {}
    for name in forbidden:
        try:
            address = resolve_name(name)
        except OSError:
            blocked[name] = "nxdomain"
            continue
        raise SmokeFailure(
            f"forbidden simulator DNS name {name} resolved to {address}; "
            "Fake Booking/Fake PVS must stay off the lab network"
        )
    return {"resolved": resolved, "forbidden": blocked}


def expect_status(
    label: str,
    status: int | None,
    body: Any,
    error: str | None,
    expected: int,
) -> Any:
    if status != expected:
        raise SmokeFailure(
            f"{label}: expected HTTP {expected}, got {status} error={error!r} body={body!r}"
        )
    return body


def expect_transport_error(label: str, status: int | None, error: str | None) -> str:
    if status is not None or not error:
        raise SmokeFailure(
            f"{label}: expected transport error, got status={status} error={error!r}"
        )
    return error


def _require_ok(label: str, payload: Any, *, service: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise SmokeFailure(f"{label}: unexpected body {payload!r}")
    if payload.get("service") != service:
        raise SmokeFailure(f"{label}: unexpected service {payload!r}")
    return payload


def _require_reset(label: str, payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("status") != "reset":
        raise SmokeFailure(f"{label}: unexpected body {payload!r}")


def run_smoke(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or settings_from_env()
    timeout = float(cfg["timeout"])
    checks: list[dict[str, str]] = []
    dns: dict[str, Any] | None = None

    def passed(name: str) -> None:
        checks.append({"name": name, "status": "pass"})

    if not cfg["skip_dns"]:
        dns = check_dns(tuple(cfg["lab_dns_names"]), tuple(cfg["forbidden_dns_names"]))
        passed("lab_dns_aliases")
        passed("simulator_dns_absent")

    booking = cfg["booking_url"]
    pvs = cfg["pvs_url"]
    booking_admin = cfg["booking_chaos_admin_url"]
    pvs_admin = cfg["pvs_chaos_admin_url"]

    status, body, error = request("GET", f"{booking}/healthz", timeout=timeout)
    booking_health = _require_ok(
        "booking_healthz",
        expect_status("booking_healthz", status, body, error, 200),
        service="fake-booking",
    )
    passed("booking_health")

    status, body, error = request("GET", f"{pvs}/healthz", timeout=timeout)
    pvs_health = _require_ok(
        "pvs_healthz",
        expect_status("pvs_healthz", status, body, error, 200),
        service="fake-pvs",
    )
    passed("pvs_health")

    for name, base in (("booking_reset", booking), ("pvs_reset", pvs)):
        status, body, error = request("POST", f"{base}/v1/admin/reset", timeout=timeout)
        _require_reset(name, expect_status(name, status, body, error, 200))
    passed("vendor_reset_through_chaos")

    for name, base in (("booking_chaos_reset", booking_admin), ("pvs_chaos_reset", pvs_admin)):
        status, body, error = request("POST", f"{base}/v1/admin/reset", timeout=timeout)
        _require_reset(name, expect_status(name, status, body, error, 200))
    passed("chaos_admin_reset")

    status, body, error = request("GET", f"{booking}/v1/slots", timeout=timeout)
    slots_payload = expect_status("list_slots", status, body, error, 200)
    slots = slots_payload.get("slots") if isinstance(slots_payload, dict) else None
    if not isinstance(slots, list) or not slots or not isinstance(slots[0], dict):
        raise SmokeFailure(f"no booking slots: {slots_payload!r}")
    slot_id = slots[0].get("id")
    if not isinstance(slot_id, str) or not slot_id:
        raise SmokeFailure(f"invalid slot: {slots[0]!r}")
    passed("booking_availability")

    status, body, error = request(
        "POST",
        f"{booking}/v1/bookings",
        payload={"slot_id": slot_id, "patient_ref": SYNTH_PATIENT},
        headers={"Idempotency-Key": "lab-booking-happy"},
        timeout=timeout,
    )
    created = expect_status("create_booking", status, body, error, 201)
    if not isinstance(created, dict) or created.get("status") != "confirmed":
        raise SmokeFailure(f"unexpected booking: {created!r}")
    booking_id = created.get("id")
    if not isinstance(booking_id, str) or not booking_id:
        raise SmokeFailure(f"booking missing id: {created!r}")
    status, body, error = request("GET", f"{booking}/v1/bookings/{booking_id}", timeout=timeout)
    fetched = expect_status("read_booking", status, body, error, 200)
    if not isinstance(fetched, dict) or fetched.get("id") != booking_id:
        raise SmokeFailure(f"booking read mismatch: {fetched!r}")
    status, body, error = request(
        "POST",
        f"{booking}/v1/bookings",
        payload={"slot_id": slot_id, "patient_ref": SYNTH_PATIENT},
        headers={"Idempotency-Key": "lab-booking-happy"},
        timeout=timeout,
    )
    replay = expect_status("replay_booking", status, body, error, 200)
    if not isinstance(replay, dict) or replay.get("id") != booking_id:
        raise SmokeFailure(f"booking idempotent replay mismatch: {replay!r}")
    passed("booking_write")
    passed("booking_idempotent_replay")

    status, body, error = request("GET", f"{pvs}/v1/patients", timeout=timeout)
    patients_payload = expect_status("list_patients", status, body, error, 200)
    patients = patients_payload.get("patients") if isinstance(patients_payload, dict) else None
    if not isinstance(patients, list) or SYNTH_PATIENT not in {
        item.get("id") for item in patients if isinstance(item, dict)
    }:
        raise SmokeFailure(f"missing {SYNTH_PATIENT}: {patients_payload!r}")
    status, body, error = request("GET", f"{pvs}/v1/patients/{SYNTH_PATIENT}", timeout=timeout)
    expect_status("read_patient", status, body, error, 200)
    status, body, error = request(
        "GET",
        f"{pvs}/v1/patients/{SYNTH_PATIENT}/encounters",
        timeout=timeout,
    )
    encounters = expect_status("list_encounters", status, body, error, 200)
    if not isinstance(encounters, dict) or not encounters.get("encounters"):
        raise SmokeFailure(f"no encounters: {encounters!r}")
    passed("pvs_read")

    status, body, error = request(
        "POST",
        f"{pvs}/v1/tasks",
        payload={"patient_id": SYNTH_PATIENT, "title": SYNTH_TITLE, "priority": "normal"},
        headers={"Idempotency-Key": "lab-pvs-happy"},
        timeout=timeout,
    )
    created_task = expect_status("create_task", status, body, error, 201)
    if not isinstance(created_task, dict) or created_task.get("status") != "open":
        raise SmokeFailure(f"unexpected task: {created_task!r}")
    task_id = created_task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise SmokeFailure(f"task missing id: {created_task!r}")
    status, body, error = request("GET", f"{pvs}/v1/tasks/{task_id}", timeout=timeout)
    fetched_task = expect_status("read_task", status, body, error, 200)
    if not isinstance(fetched_task, dict) or fetched_task.get("id") != task_id:
        raise SmokeFailure(f"task read mismatch: {fetched_task!r}")
    status, body, error = request(
        "POST",
        f"{pvs}/v1/tasks",
        payload={"patient_id": SYNTH_PATIENT, "title": SYNTH_TITLE, "priority": "normal"},
        headers={"Idempotency-Key": "lab-pvs-happy"},
        timeout=timeout,
    )
    replay_task = expect_status("replay_task", status, body, error, 200)
    if not isinstance(replay_task, dict) or replay_task.get("id") != task_id:
        raise SmokeFailure(f"pvs idempotent replay mismatch: {replay_task!r}")
    passed("pvs_write")
    passed("pvs_idempotent_replay")

    status, body, error = request(
        "PUT",
        f"{booking_admin}/v1/admin/faults",
        payload={
            "mode": "drop_after_upstream",
            "remaining": 1,
            "method": "POST",
            "path": "/v1/bookings",
        },
        timeout=timeout,
    )
    armed = expect_status("arm_drop_after_upstream", status, body, error, 200)
    if not isinstance(armed, dict) or armed.get("mode") != "drop_after_upstream":
        raise SmokeFailure(f"failed to arm chaos: {armed!r}")

    status, body, error = request("GET", f"{booking}/v1/slots", timeout=timeout)
    remaining_payload = expect_status("list_slots_for_chaos", status, body, error, 200)
    remaining = remaining_payload.get("slots") if isinstance(remaining_payload, dict) else None
    if not isinstance(remaining, list) or not remaining or not isinstance(remaining[0], dict):
        raise SmokeFailure(f"no remaining slots for chaos: {remaining_payload!r}")
    chaos_slot_id = remaining[0].get("id")
    if not isinstance(chaos_slot_id, str) or not chaos_slot_id:
        raise SmokeFailure(f"invalid chaos slot: {remaining[0]!r}")

    status, body, error = request(
        "POST",
        f"{booking}/v1/bookings",
        payload={"slot_id": chaos_slot_id, "patient_ref": SYNTH_PATIENT},
        headers={"Idempotency-Key": "lab-booking-drop-after"},
        timeout=timeout,
    )
    chaos_error = expect_transport_error("booking_drop_after_upstream", status, error)
    passed("transport_chaos_observed")

    return {
        "schema": SCHEMA,
        "status": "pass",
        "dns": dns,
        "checks": checks,
        "booking": {
            "id": booking_id,
            "slot_id": slot_id,
            "idempotent_replay": True,
            "health": booking_health,
        },
        "pvs": {
            "id": task_id,
            "patient_id": SYNTH_PATIENT,
            "idempotent_replay": True,
            "health": pvs_health,
        },
        "chaos": {
            "mode": "drop_after_upstream",
            "observed": True,
            "error": chaos_error,
        },
    }

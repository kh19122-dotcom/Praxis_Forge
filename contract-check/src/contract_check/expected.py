from __future__ import annotations

# Paths the scenario runner actually calls on the simulators. Must appear in
# both the packaged YAML contract and the runtime-generated JSON contract.
REQUIRED_OPERATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "fake-booking": (
        ("GET", "/healthz"),
        ("GET", "/v1/slots"),
        ("POST", "/v1/bookings"),
        ("GET", "/v1/bookings/{booking_id}"),
        ("GET", "/v1/admin/events"),
        ("PUT", "/v1/admin/faults"),
        ("POST", "/v1/admin/reset"),
    ),
    "fake-pvs": (
        ("GET", "/healthz"),
        ("GET", "/v1/patients"),
        ("GET", "/v1/patients/{patient_id}"),
        ("GET", "/v1/patients/{patient_id}/encounters"),
        ("POST", "/v1/tasks"),
        ("GET", "/v1/tasks/{task_id}"),
        ("GET", "/v1/admin/events"),
        ("PUT", "/v1/admin/faults"),
        ("POST", "/v1/admin/reset"),
    ),
}

IDEMPOTENT_WRITES: dict[str, tuple[str, str]] = {
    "fake-booking": ("POST", "/v1/bookings"),
    "fake-pvs": ("POST", "/v1/tasks"),
}

# Documented status codes from the packaged YAML contract of record. Runtime
# FastAPI JSON commonly omits these error codes; that representation gap is
# ignored. Dropping them from YAML is drift.
DOCUMENTED_STATUS_CODES: dict[str, dict[tuple[str, str], tuple[str, ...]]] = {
    "fake-booking": {
        ("GET", "/healthz"): ("200",),
        ("GET", "/v1/slots"): ("200",),
        ("POST", "/v1/bookings"): ("200", "201", "409", "503", "504"),
        ("GET", "/v1/bookings/{booking_id}"): ("200", "404"),
        ("GET", "/v1/admin/events"): ("200",),
        ("PUT", "/v1/admin/faults"): ("200",),
        ("POST", "/v1/admin/reset"): ("200",),
    },
    "fake-pvs": {
        ("GET", "/healthz"): ("200",),
        ("GET", "/v1/patients"): ("200", "422"),
        ("GET", "/v1/patients/{patient_id}"): ("200", "404", "422"),
        ("GET", "/v1/patients/{patient_id}/encounters"): ("200", "404"),
        ("POST", "/v1/tasks"): ("200", "201", "404", "409", "422", "503", "504"),
        ("GET", "/v1/tasks/{task_id}"): ("200", "404"),
        ("GET", "/v1/admin/events"): ("200",),
        ("PUT", "/v1/admin/faults"): ("200",),
        ("POST", "/v1/admin/reset"): ("200",),
    },
}

REQUEST_REQUIRED_FIELDS: dict[str, dict[tuple[str, str], tuple[str, ...]]] = {
    "fake-booking": {
        ("POST", "/v1/bookings"): ("slot_id", "patient_ref"),
    },
    "fake-pvs": {
        ("POST", "/v1/tasks"): ("patient_id", "title"),
    },
}

# Representative success-response required fields from runtime JSON schemas.
RESPONSE_REQUIRED_FIELDS: dict[
    str, dict[tuple[str, str, str], tuple[str, ...]]
] = {
    "fake-booking": {
        ("GET", "/healthz", "200"): ("status", "service", "seed"),
        ("GET", "/v1/slots", "200"): ("seed", "slots"),
        ("POST", "/v1/bookings", "201"): (
            "id",
            "slot_id",
            "resource_id",
            "start",
            "end",
            "patient_ref",
            "status",
            "idempotency_key",
        ),
        ("GET", "/v1/bookings/{booking_id}", "200"): (
            "id",
            "slot_id",
            "resource_id",
            "start",
            "end",
            "patient_ref",
            "status",
            "idempotency_key",
        ),
        ("GET", "/v1/admin/events", "200"): ("seed", "events"),
        ("PUT", "/v1/admin/faults", "200"): ("mode", "delay_ms", "remaining"),
        ("POST", "/v1/admin/reset", "200"): ("status", "seed"),
    },
    "fake-pvs": {
        ("GET", "/healthz", "200"): ("status", "service", "seed"),
        ("GET", "/v1/patients", "200"): ("seed", "patients"),
        ("GET", "/v1/patients/{patient_id}", "200"): ("id", "cohort", "site", "status"),
        ("GET", "/v1/patients/{patient_id}/encounters", "200"): (
            "seed",
            "patient_id",
            "encounters",
        ),
        ("POST", "/v1/tasks", "201"): (
            "id",
            "patient_id",
            "title",
            "priority",
            "status",
            "idempotency_key",
        ),
        ("GET", "/v1/tasks/{task_id}", "200"): (
            "id",
            "patient_id",
            "title",
            "priority",
            "status",
            "idempotency_key",
        ),
        ("GET", "/v1/admin/events", "200"): ("seed", "events"),
        ("PUT", "/v1/admin/faults", "200"): ("mode", "delay_ms", "remaining"),
        ("POST", "/v1/admin/reset", "200"): ("status", "seed"),
    },
}

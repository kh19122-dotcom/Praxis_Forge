from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

SLOT = {
    "id": "slot_alpha_0900",
    "resource_id": "res-alpha",
    "start": "2030-01-06T09:00:00Z",
    "end": "2030-01-06T10:00:00Z",
}

PATIENTS = [
    {"id": "synth-ada", "cohort": "cohort-alpha", "site": "site-north", "status": "active"},
    {"id": "synth-ben", "cohort": "cohort-alpha", "site": "site-north", "status": "active"},
]

ENCOUNTERS = {
    "synth-ada": [
        {
            "id": "enc_ada_1",
            "patient_id": "synth-ada",
            "occurred_at": "2030-01-06T09:00:00Z",
            "kind": "intake",
            "summary": "synth-encounter-1",
            "status": "completed",
        }
    ]
}


class _Service:
    def __init__(self, name: str, seed: str) -> None:
        self.name = name
        self.seed = seed
        self.events: list[dict[str, Any]] = []
        self.fault: dict[str, Any] = {
            "mode": "none",
            "delay_ms": 50,
            "remaining": 0,
            "idempotency_key": None,
        }
        self._seq = 0
        self._trace = 0

    def reset_common(self) -> None:
        self.events = []
        self.fault = {
            "mode": "none",
            "delay_ms": 50,
            "remaining": 0,
            "idempotency_key": None,
        }
        self._seq = 0
        self._trace = 0

    def next_trace(self) -> str:
        self._trace += 1
        return f"tr_{self._trace:06d}"

    def record(self, trace_id: str, event_type: str, **details: object) -> None:
        self._seq += 1
        self.events.append(
            {
                "seq": self._seq,
                "trace_id": trace_id,
                "type": event_type,
                "details": details,
            }
        )

    def consume_fault(self, idempotency_key: str) -> dict[str, Any]:
        current = dict(self.fault)
        if current["mode"] == "none" or current["remaining"] <= 0:
            return {"mode": "none", "delay_ms": current["delay_ms"], "remaining": 0}
        if current["idempotency_key"] and current["idempotency_key"] != idempotency_key:
            return {
                "mode": "none",
                "delay_ms": current["delay_ms"],
                "remaining": current["remaining"],
            }
        remaining = current["remaining"] - 1
        self.fault = {
            "mode": current["mode"] if remaining > 0 else "none",
            "delay_ms": current["delay_ms"],
            "remaining": remaining,
            "idempotency_key": current["idempotency_key"] if remaining > 0 else None,
        }
        return current

    def json_response(self, status: int, payload: dict[str, Any]) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def error(
        self,
        status: int,
        error: str,
        message: str,
        trace_id: str,
        **details: object,
    ) -> httpx.Response:
        body: dict[str, Any] = {"error": error, "message": message, "trace_id": trace_id}
        if details:
            body["details"] = details
        return self.json_response(status, body)

    def handle_admin(self, request: httpx.Request) -> httpx.Response | None:
        parsed = urlparse(str(request.url))
        if parsed.path == "/healthz" and request.method == "GET":
            return self.json_response(
                200, {"status": "ok", "service": self.name, "seed": self.seed}
            )
        if parsed.path == "/v1/admin/reset" and request.method == "POST":
            self.reset()
            return self.json_response(200, {"status": "reset", "seed": self.seed})
        if parsed.path == "/v1/admin/faults" and request.method == "PUT":
            payload = json.loads(request.content.decode() or "{}")
            mode = payload.get("mode", "none")
            remaining = 0 if mode == "none" else int(payload.get("remaining", 1))
            self.fault = {
                "mode": mode,
                "delay_ms": int(payload.get("delay_ms", 50)),
                "remaining": remaining,
                "idempotency_key": payload.get("idempotency_key"),
            }
            self.record(
                self.next_trace(),
                "fault_configured",
                mode=self.fault["mode"],
                delay_ms=self.fault["delay_ms"],
                remaining=self.fault["remaining"],
                idempotency_key=self.fault["idempotency_key"],
            )
            return self.json_response(200, self.fault)
        if parsed.path == "/v1/admin/events" and request.method == "GET":
            events = self.events
            query = parse_qs(parsed.query)
            if "trace_id" in query:
                trace_id = query["trace_id"][0]
                events = [event for event in events if event["trace_id"] == trace_id]
            return self.json_response(200, {"seed": self.seed, "events": events})
        return None

    def reset(self) -> None:
        raise NotImplementedError


class FakeBooking(_Service):
    def __init__(self) -> None:
        super().__init__("fake-booking", "obj-001")
        self.slot: dict[str, Any] = {}
        self.bookings: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}
        self.reset()

    def reset(self) -> None:
        self.reset_common()
        self.slot = {**SLOT, "booking_id": None}
        self.bookings = {}
        self.by_key = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        handled = self.handle_admin(request)
        if handled is not None:
            return handled
        parsed = urlparse(str(request.url))
        if parsed.path == "/v1/slots" and request.method == "GET":
            slots = []
            if self.slot["booking_id"] is None:
                slots.append({**self.slot, "available": True})
            return self.json_response(200, {"seed": self.seed, "slots": slots})
        if parsed.path == "/v1/bookings" and request.method == "POST":
            return self._create_booking(request)
        if parsed.path.startswith("/v1/bookings/") and request.method == "GET":
            booking_id = parsed.path.rsplit("/", 1)[-1]
            booking = self.bookings.get(booking_id)
            if booking is None:
                return self.error(404, "booking_not_found", "Unknown booking.", "tr_000000")
            return self.json_response(200, _public_booking(booking))
        return self.error(404, "not_found", "Unknown path.", "tr_000000")

    def _create_booking(self, request: httpx.Request) -> httpx.Response:
        key = request.headers["Idempotency-Key"]
        payload = json.loads(request.content.decode())
        slot_id = payload["slot_id"]
        patient_ref = payload["patient_ref"]
        trace_id = self.next_trace()
        self.record(
            trace_id,
            "booking_requested",
            idempotency_key=key,
            slot_id=slot_id,
            patient_ref=patient_ref,
        )
        fault = self.consume_fault(key)
        if fault["mode"] == "fail_before_commit":
            self.record(trace_id, "fault_injected", mode=fault["mode"])
            self.record(trace_id, "commit_skipped", reason="fail_before_commit")
            return self.error(
                503,
                "fail_before_commit",
                "Remote commit was not attempted.",
                trace_id,
                committed=False,
            )

        existing_id = self.by_key.get(key)
        if existing_id:
            existing = self.bookings[existing_id]
            if existing["slot_id"] != slot_id or existing["patient_ref"] != patient_ref:
                self.record(
                    trace_id,
                    "conflict",
                    reason="idempotency_conflict",
                    booking_id=existing_id,
                )
                return self.error(
                    409,
                    "idempotency_conflict",
                    "Idempotency key was reused with a different request body.",
                    trace_id,
                    booking_id=existing_id,
                    committed=False,
                )
            self.record(
                trace_id,
                "booking_replayed",
                booking_id=existing_id,
                slot_id=existing["slot_id"],
                idempotency_key=key,
                committed=True,
            )
            return self.json_response(200, _public_booking(existing))

        if slot_id != self.slot["id"]:
            self.record(trace_id, "commit_skipped", reason="slot_not_found")
            return self.error(404, "slot_not_found", "Unknown slot.", trace_id, slot_id=slot_id)
        if self.slot["booking_id"] is not None:
            self.record(
                trace_id,
                "conflict",
                reason="slot_conflict",
                existing_booking_id=self.slot["booking_id"],
            )
            return self.error(
                409,
                "slot_conflict",
                "Slot is already booked.",
                trace_id,
                existing_booking_id=self.slot["booking_id"],
                committed=False,
            )

        booking_id = f"bkg_{key}"
        booking = {
            "id": booking_id,
            "slot_id": slot_id,
            "resource_id": self.slot["resource_id"],
            "start": self.slot["start"],
            "end": self.slot["end"],
            "patient_ref": patient_ref,
            "status": "confirmed",
            "idempotency_key": key,
        }
        self.slot["booking_id"] = booking_id
        self.bookings[booking_id] = booking
        self.by_key[key] = booking_id
        self.record(
            trace_id,
            "booking_committed",
            booking_id=booking_id,
            slot_id=slot_id,
            idempotency_key=key,
            committed=True,
        )
        if fault["mode"] == "ambiguous":
            self.record(
                trace_id,
                "response_suppressed",
                reason="ambiguous_outcome",
                booking_id=booking_id,
                committed=True,
            )
            return self.error(
                504,
                "ambiguous_outcome",
                "Remote effect may have committed; client must inspect Forge evidence.",
                trace_id,
                committed=None,
            )
        return self.json_response(201, _public_booking(booking))


class FakePvs(_Service):
    def __init__(self) -> None:
        super().__init__("fake-pvs", "obj-002")
        self.tasks: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}
        self.reset()

    def reset(self) -> None:
        self.reset_common()
        self.tasks = {}
        self.by_key = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        handled = self.handle_admin(request)
        if handled is not None:
            return handled
        parsed = urlparse(str(request.url))
        if parsed.path == "/v1/patients" and request.method == "GET":
            return self.json_response(200, {"seed": self.seed, "patients": PATIENTS})
        if parsed.path.startswith("/v1/patients/") and parsed.path.endswith("/encounters"):
            patient_id = parsed.path.split("/")[3]
            encounters = ENCOUNTERS.get(patient_id)
            if encounters is None:
                return self.error(404, "patient_not_found", "Unknown patient.", "tr_000000")
            return self.json_response(
                200,
                {"seed": self.seed, "patient_id": patient_id, "encounters": encounters},
            )
        if parsed.path.startswith("/v1/patients/") and request.method == "GET":
            patient_id = parsed.path.rsplit("/", 1)[-1]
            patient = next((item for item in PATIENTS if item["id"] == patient_id), None)
            if patient is None:
                return self.error(404, "patient_not_found", "Unknown patient.", "tr_000000")
            return self.json_response(200, patient)
        if parsed.path == "/v1/tasks" and request.method == "POST":
            return self._create_task(request)
        if parsed.path.startswith("/v1/tasks/") and request.method == "GET":
            task_id = parsed.path.rsplit("/", 1)[-1]
            task = self.tasks.get(task_id)
            if task is None:
                return self.error(404, "task_not_found", "Unknown task.", "tr_000000")
            return self.json_response(200, _public_task(task))
        return self.error(404, "not_found", "Unknown path.", "tr_000000")

    def _create_task(self, request: httpx.Request) -> httpx.Response:
        key = request.headers["Idempotency-Key"]
        payload = json.loads(request.content.decode())
        patient_id = payload["patient_id"]
        title = payload["title"]
        priority = payload.get("priority", "normal")
        trace_id = self.next_trace()
        self.record(
            trace_id,
            "task_requested",
            idempotency_key=key,
            patient_id=patient_id,
            title=title,
            priority=priority,
        )
        fault = self.consume_fault(key)
        if fault["mode"] == "fail_before_commit":
            self.record(trace_id, "fault_injected", mode=fault["mode"])
            self.record(trace_id, "commit_skipped", reason="fail_before_commit")
            return self.error(
                503,
                "fail_before_commit",
                "Remote commit was not attempted.",
                trace_id,
                committed=False,
            )

        existing_id = self.by_key.get(key)
        if existing_id:
            existing = self.tasks[existing_id]
            if (
                existing["patient_id"] != patient_id
                or existing["title"] != title
                or existing["priority"] != priority
            ):
                self.record(
                    trace_id,
                    "conflict",
                    reason="idempotency_conflict",
                    task_id=existing_id,
                )
                return self.error(
                    409,
                    "idempotency_conflict",
                    "Idempotency key was reused with a different request body.",
                    trace_id,
                    task_id=existing_id,
                    committed=False,
                )
            self.record(
                trace_id,
                "task_replayed",
                task_id=existing_id,
                patient_id=existing["patient_id"],
                idempotency_key=key,
                committed=True,
            )
            return self.json_response(200, _public_task(existing))

        if patient_id not in {item["id"] for item in PATIENTS}:
            self.record(trace_id, "commit_skipped", reason="patient_not_found")
            return self.error(404, "patient_not_found", "Unknown patient.", trace_id)

        task_id = f"tsk_{key}"
        task = {
            "id": task_id,
            "patient_id": patient_id,
            "title": title,
            "priority": priority,
            "status": "open",
            "idempotency_key": key,
        }
        self.tasks[task_id] = task
        self.by_key[key] = task_id
        self.record(
            trace_id,
            "task_committed",
            task_id=task_id,
            patient_id=patient_id,
            idempotency_key=key,
            committed=True,
        )
        if fault["mode"] == "ambiguous":
            self.record(
                trace_id,
                "response_suppressed",
                reason="ambiguous_outcome",
                task_id=task_id,
                committed=True,
            )
            return self.error(
                504,
                "ambiguous_outcome",
                "Remote effect may have committed; client must inspect Forge evidence.",
                trace_id,
                committed=None,
            )
        return self.json_response(201, _public_task(task))


class FakeChaos:
    def __init__(self, name: str, upstream) -> None:
        self.name = name
        self.upstream = upstream
        self.reset()

    def reset(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.fault: dict[str, Any] = {
            "mode": "none",
            "delay_ms": 50,
            "remaining": 0,
            "method": None,
            "path": None,
            "idempotency_key": None,
        }
        self._seq = 0

    def record(self, event_type: str, **details: object) -> None:
        self._seq += 1
        self.events.append({"seq": self._seq, "type": event_type, "details": details})

    def consume(
        self, method: str, path: str, idempotency_key: str | None
    ) -> dict[str, Any] | None:
        current = dict(self.fault)
        if current["mode"] == "none" or current["remaining"] <= 0:
            return None
        if current["method"] and current["method"] != method:
            return None
        if current["path"] and current["path"] != path:
            return None
        if current["idempotency_key"] and current["idempotency_key"] != idempotency_key:
            return None
        remaining = current["remaining"] - 1
        self.fault = {
            "mode": current["mode"] if remaining > 0 else "none",
            "delay_ms": current["delay_ms"],
            "remaining": remaining,
            "method": current["method"] if remaining > 0 else None,
            "path": current["path"] if remaining > 0 else None,
            "idempotency_key": current["idempotency_key"] if remaining > 0 else None,
        }
        self.record(
            "fault_consumed",
            mode=current["mode"],
            remaining=remaining,
            method=method,
            path=path,
            idempotency_key=idempotency_key,
        )
        return current

    def json_response(self, status: int, payload: dict[str, Any]) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def handle(self, request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        path = parsed.path
        method = request.method.upper()
        key = request.headers.get("Idempotency-Key")
        self.record("request_received", method=method, path=path, idempotency_key=key)
        fault = None
        if not (method == "GET" and path == "/healthz"):
            fault = self.consume(method, path, key)
        if fault and fault["mode"] == "drop_before_upstream":
            self.record(
                "dropped_before_upstream",
                method=method,
                path=path,
                idempotency_key=key,
            )
            raise httpx.ConnectError("dropped before upstream", request=request)
        response = self.upstream(request)
        self.record(
            "upstream_completed",
            method=method,
            path=path,
            idempotency_key=key,
            upstream_status=response.status_code,
        )
        if fault and fault["mode"] == "drop_after_upstream":
            self.record(
                "dropped_after_upstream",
                method=method,
                path=path,
                idempotency_key=key,
                upstream_status=response.status_code,
            )
            raise httpx.ReadError("dropped after upstream", request=request)
        return response

    def handle_admin(self, request: httpx.Request) -> httpx.Response:
        parsed = urlparse(str(request.url))
        if parsed.path == "/healthz" and request.method == "GET":
            return self.json_response(
                200, {"status": "ok", "service": self.name, "upstream": "fake"}
            )
        if parsed.path == "/v1/admin/reset" and request.method == "POST":
            self.reset()
            return self.json_response(200, {"status": "reset", "service": self.name})
        if parsed.path == "/v1/admin/faults" and request.method == "GET":
            return self.json_response(200, self.fault)
        if parsed.path == "/v1/admin/faults" and request.method == "PUT":
            payload = json.loads(request.content.decode() or "{}")
            mode = payload.get("mode", "none")
            remaining = 0 if mode == "none" else int(payload.get("remaining", 1))
            method = payload.get("method")
            self.fault = {
                "mode": mode,
                "delay_ms": int(payload.get("delay_ms", 50)),
                "remaining": remaining,
                "method": method.upper() if isinstance(method, str) else None,
                "path": payload.get("path"),
                "idempotency_key": payload.get("idempotency_key"),
            }
            self.record("fault_configured", **self.fault)
            return self.json_response(200, self.fault)
        if parsed.path == "/v1/admin/events" and request.method == "GET":
            return self.json_response(200, {"service": self.name, "events": self.events})
        return self.json_response(404, {"error": "not_found"})


class FakeForge:
    def __init__(self) -> None:
        self.booking = FakeBooking()
        self.pvs = FakePvs()
        self.booking_chaos = FakeChaos("chaos-booking", self.booking.handle)
        self.pvs_chaos = FakeChaos("chaos-pvs", self.pvs.handle)


def _public_booking(booking: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": booking["id"],
        "slot_id": booking["slot_id"],
        "resource_id": booking["resource_id"],
        "start": booking["start"],
        "end": booking["end"],
        "patient_ref": booking["patient_ref"],
        "status": booking["status"],
        "idempotency_key": booking["idempotency_key"],
    }


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task["id"],
        "patient_id": task["patient_id"],
        "title": task["title"],
        "priority": task["priority"],
        "status": task["status"],
        "idempotency_key": task["idempotency_key"],
    }

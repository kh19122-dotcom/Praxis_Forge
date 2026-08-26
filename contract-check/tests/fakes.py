from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PROBE_BODIES = {
    "fake-booking": {
        "/healthz": {"status": "ok", "service": "fake-booking", "seed": "obj-001"},
        "/v1/slots": {"seed": "obj-001", "slots": []},
        "/v1/admin/events": {"seed": "obj-001", "events": []},
        "/v1/admin/faults": {
            "mode": "none",
            "delay_ms": 50,
            "remaining": 0,
            "idempotency_key": None,
        },
    },
    "fake-pvs": {
        "/healthz": {"status": "ok", "service": "fake-pvs", "seed": "obj-002"},
        "/v1/patients": {"seed": "obj-002", "patients": []},
        "/v1/admin/events": {"seed": "obj-002", "events": []},
        "/v1/admin/faults": {
            "mode": "none",
            "delay_ms": 50,
            "remaining": 0,
            "idempotency_key": None,
        },
    },
}


def load_fixture(service: str, kind: str) -> str:
    suffix = "json" if kind == "json" else "yaml"
    return (FIXTURES / f"{service}.openapi.{suffix}").read_text(encoding="utf-8")


def load_json_spec(service: str) -> dict[str, Any]:
    return json.loads(load_fixture(service, "json"))


class SpecServer:
    def __init__(
        self,
        *,
        booking_json: dict[str, Any] | None = None,
        booking_yaml: str | None = None,
        pvs_json: dict[str, Any] | None = None,
        pvs_yaml: str | None = None,
        booking_status: dict[str, int] | None = None,
        pvs_status: dict[str, int] | None = None,
    ) -> None:
        self.booking_json = (
            booking_json if booking_json is not None else load_json_spec("fake-booking")
        )
        self.pvs_json = pvs_json if pvs_json is not None else load_json_spec("fake-pvs")
        self.booking_yaml = (
            booking_yaml if booking_yaml is not None else load_fixture("fake-booking", "yaml")
        )
        self.pvs_yaml = pvs_yaml if pvs_yaml is not None else load_fixture("fake-pvs", "yaml")
        self.booking_status = booking_status or {}
        self.pvs_status = pvs_status or {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = urlparse(str(request.url)).hostname
        path = request.url.path
        if host == "booking.test":
            return self._serve(
                "fake-booking",
                path,
                self.booking_json,
                self.booking_yaml,
                self.booking_status,
            )
        if host == "pvs.test":
            return self._serve(
                "fake-pvs",
                path,
                self.pvs_json,
                self.pvs_yaml,
                self.pvs_status,
            )
        return httpx.Response(404, json={"error": "unknown_host"})

    def _serve(
        self,
        service: str,
        path: str,
        spec: dict[str, Any],
        yaml_text: str,
        status_overrides: dict[str, int],
    ) -> httpx.Response:
        status = status_overrides.get(path, 200)
        if path == "/openapi.json":
            return httpx.Response(status, json=spec)
        if path == "/openapi.yaml":
            return httpx.Response(
                status, text=yaml_text, headers={"content-type": "application/yaml"}
            )
        body = PROBE_BODIES[service].get(path)
        if body is not None:
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "not_found"})


def clients_for(server: SpecServer) -> dict[str, httpx.Client]:
    transport = httpx.MockTransport(server.handle)
    return {
        "fake-booking": httpx.Client(
            base_url="http://booking.test",
            transport=transport,
            timeout=2.0,
        ),
        "fake-pvs": httpx.Client(
            base_url="http://pvs.test",
            transport=transport,
            timeout=2.0,
        ),
    }

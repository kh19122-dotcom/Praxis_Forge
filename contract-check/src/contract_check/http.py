from __future__ import annotations

from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0

SAFE_PROBES: dict[str, tuple[str, ...]] = {
    "fake-booking": (
        "/healthz",
        "/v1/slots",
        "/v1/admin/events",
        "/v1/admin/faults",
    ),
    "fake-pvs": (
        "/healthz",
        "/v1/patients",
        "/v1/admin/events",
        "/v1/admin/faults",
    ),
}


class FetchError(Exception):
    def __init__(self, service: str, path: str, detail: str) -> None:
        self.service = service
        self.path = path
        self.detail = detail
        super().__init__(detail)


def fetch_service(
    service: str,
    base_url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    owns = client is None
    http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
    try:
        openapi_json = _get(http, service, "/openapi.json")
        openapi_yaml = _get(http, service, "/openapi.yaml")
        try:
            json_body = openapi_json.json()
        except ValueError as exc:
            raise FetchError(service, "/openapi.json", f"invalid JSON: {exc}") from exc
        yaml_text = openapi_yaml.text
        probes = []
        for path in SAFE_PROBES[service]:
            response = _get(http, service, path)
            probes.append({"path": path, "status": response.status_code})
        return {
            "service": service,
            "url": base_url.rstrip("/"),
            "openapi_json": json_body,
            "openapi_yaml_text": yaml_text,
            "fetched": {
                "/openapi.json": openapi_json.status_code,
                "/openapi.yaml": openapi_yaml.status_code,
            },
            "probes": probes,
        }
    finally:
        if owns:
            http.close()


def _get(client: httpx.Client, service: str, path: str) -> httpx.Response:
    try:
        response = client.get(path)
    except httpx.HTTPError as exc:
        raise FetchError(service, path, f"{type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise FetchError(
            service,
            path,
            f"expected 200, got {response.status_code}",
        )
    return response

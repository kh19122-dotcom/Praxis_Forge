from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass
class HttpCall:
    method: str
    url: str
    path: str
    status_code: int | None
    body: Any
    error: str | None = None

    @property
    def trace_id(self) -> str | None:
        if isinstance(self.body, dict):
            value = self.body.get("trace_id")
            if isinstance(value, str):
                return value
        return None


class ServiceClient:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> HttpCall:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(
                method,
                path,
                json=json,
                headers=headers,
                params=params,
            )
        except httpx.HTTPError as exc:
            return HttpCall(
                method=method.upper(),
                url=url,
                path=path,
                status_code=None,
                body=None,
                error=str(exc),
            )
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        return HttpCall(
            method=method.upper(),
            url=str(response.request.url),
            path=path,
            status_code=response.status_code,
            body=body,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

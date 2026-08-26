from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    upstream_url: str
    proxy_host: str = "0.0.0.0"
    proxy_port: int = 8090
    admin_host: str = "0.0.0.0"
    admin_port: int = 8092
    service_name: str = "chaos-proxy"
    upstream_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> Settings:
        upstream = os.environ.get("FORGE_UPSTREAM_URL", "").strip().rstrip("/")
        if not upstream:
            raise ValueError("FORGE_UPSTREAM_URL is required")
        return cls(
            upstream_url=upstream,
            proxy_host=os.environ.get("FORGE_PROXY_HOST", "0.0.0.0").strip() or "0.0.0.0",
            proxy_port=_env_int("FORGE_PROXY_PORT", 8090),
            admin_host=os.environ.get("FORGE_ADMIN_HOST", "0.0.0.0").strip() or "0.0.0.0",
            admin_port=_env_int("FORGE_ADMIN_PORT", 8092),
            service_name=(
                os.environ.get("FORGE_SERVICE_NAME", "chaos-proxy").strip() or "chaos-proxy"
            ),
            upstream_timeout_seconds=_env_float("FORGE_UPSTREAM_TIMEOUT_SECONDS", 30.0),
        )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA = "praxis-forge.contract-fingerprint.v1"
DEFAULT_FINGERPRINT_PATH = Path(__file__).with_name("data") / "fingerprints.json"


def canonical_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest_snapshot(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_dumps(snapshot).encode("utf-8")).hexdigest()


def service_record(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {"digest": digest_snapshot(snapshot), "snapshot": snapshot}


def build_document(
    services: dict[str, dict[str, Any]], *, canonical: str
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "canonical": canonical,
        "services": {
            name: service_record(snapshot) for name, snapshot in sorted(services.items())
        },
    }


def load_fingerprint(path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_FINGERPRINT_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"unexpected fingerprint schema in {target}")
    return payload

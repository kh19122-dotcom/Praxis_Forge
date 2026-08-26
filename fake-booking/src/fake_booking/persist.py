from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class RestoreError(ValueError):
    """Durable state exists but cannot be installed. The original file is left unchanged."""


class PersistenceCrash(RuntimeError):
    """Test-only simulated crash at a persistence boundary."""


def write_state(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp")
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def read_state(path: str) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        return None
    if not target.is_file():
        raise RestoreError("state path exists but is not a readable file")
    try:
        raw = target.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except OSError as exc:
        raise RestoreError("state file is unreadable") from exc
    except json.JSONDecodeError as exc:
        raise RestoreError("state file is truncated or not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RestoreError("state file must contain a JSON object")
    return payload

from __future__ import annotations

from hashlib import sha256


def digest(*parts: str, size: int = 12) -> str:
    material = "|".join(parts).encode("utf-8")
    return sha256(material).hexdigest()[:size]


def encounter_id(seed: str, patient_id: str, occurred_at: str, index: str) -> str:
    return f"enc_{digest(seed, 'encounter', patient_id, occurred_at, index)}"


def task_id(seed: str, idempotency_key: str) -> str:
    return f"tsk_{digest(seed, 'task', idempotency_key, size=16)}"

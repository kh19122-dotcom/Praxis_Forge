from __future__ import annotations

from hashlib import sha256


def digest(*parts: str, size: int = 12) -> str:
    material = "|".join(parts).encode("utf-8")
    return sha256(material).hexdigest()[:size]


def slot_id(seed: str, resource_id: str, start: str) -> str:
    return f"slot_{digest(seed, 'slot', resource_id, start)}"


def booking_id(seed: str, idempotency_key: str) -> str:
    return f"bkg_{digest(seed, 'booking', idempotency_key, size=16)}"

from __future__ import annotations

from hashlib import sha256

DOCUMENT_REQUEST_HASH_DOMAIN = "praxis.pvs.clinical-document.request-hash.v1"


def digest(*parts: str, size: int = 12) -> str:
    material = "|".join(parts).encode("utf-8")
    return sha256(material).hexdigest()[:size]


def encounter_id(seed: str, patient_id: str, occurred_at: str, index: str) -> str:
    return f"enc_{digest(seed, 'encounter', patient_id, occurred_at, index)}"


def task_id(seed: str, idempotency_key: str) -> str:
    return f"tsk_{digest(seed, 'task', idempotency_key, size=16)}"


def document_id(seed: str, idempotency_key: str) -> str:
    return f"doc_{digest(seed, 'document', idempotency_key, size=16)}"


def _length_prefixed(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(8, byteorder="big") + raw


def document_request_hash(
    synthetic_patient_id: str,
    synthetic_visit_id: str,
    document_ref: str,
    content_hash: str,
    rendered_text_sha256: str,
) -> str:
    material = b"".join(
        _length_prefixed(part)
        for part in (
            DOCUMENT_REQUEST_HASH_DOMAIN,
            synthetic_patient_id,
            synthetic_visit_id,
            document_ref,
            content_hash,
            rendered_text_sha256,
        )
    )
    return sha256(material).hexdigest()

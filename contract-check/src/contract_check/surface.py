from __future__ import annotations

from typing import Any

from contract_check.expected import IDEMPOTENT_WRITES, REQUIRED_OPERATIONS
from contract_check.normalize import extract_operations

CANONICAL = (
    "The packaged OpenAPI YAML served at /openapi.yaml is the documented contract "
    "of record. Runtime /openapi.json is FastAPI's generated view of the implemented "
    "HTTP surface. The gate compares a normalized semantic snapshot of both live "
    "documents rather than requiring byte-identical YAML and JSON."
)

COMPARED_DIMENSIONS = (
    "path_methods",
    "required_path",
    "idempotency_header",
    "parameter_schema",
    "request_shape",
    "response_shape",
    "status_codes",
    "fingerprint",
)

IGNORED_DIMENSIONS = (
    "info.title, info.description, info.version, and operation summary/description/tags",
    "schema titles, examples, servers, vendor extensions, and default values",
    "JSON Schema keywords not retained by normalize_schema, including additionalProperties",
    "byte-identical YAML vs generated JSON",
    "FastAPI HTTPValidationError envelope internals and generated 422 responses",
    "error status codes, and extra success codes such as idempotent-replay 200, "
    "present in packaged YAML but omitted from generated JSON",
    "numeric 0 vs 0.0 and other harmless JSON Schema representation differences",
)


def build_snapshot(
    service: str,
    yaml_spec: dict[str, Any],
    json_spec: dict[str, Any],
) -> dict[str, Any]:
    yaml_ops = extract_operations(yaml_spec)
    json_ops = extract_operations(json_spec)
    keys = sorted(set(yaml_ops) | set(json_ops))
    operations: dict[str, Any] = {}
    for key in keys:
        yaml_op = yaml_ops.get(key)
        json_op = json_ops.get(key)
        operations[key] = {
            "method": (yaml_op or json_op or {}).get("method"),
            "path": (yaml_op or json_op or {}).get("path"),
            "yaml": _public_op(yaml_op),
            "json": _public_op(json_op),
        }
    return {
        "service": service,
        "yaml_operations": sorted(yaml_ops),
        "json_operations": sorted(json_ops),
        "required_operations": [
            f"{method} {path}" for method, path in REQUIRED_OPERATIONS[service]
        ],
        "idempotent_write": (
            f"{IDEMPOTENT_WRITES[service][0]} {IDEMPOTENT_WRITES[service][1]}"
        ),
        "operations": operations,
    }


def _public_op(op: dict[str, Any] | None) -> dict[str, Any] | None:
    if op is None:
        return None
    request = op.get("request")
    responses = op.get("response_shapes") or {}
    success = {
        code: shape
        for code, shape in responses.items()
        if str(code).startswith("2")
    }
    return {
        "parameters": op.get("parameters") or [],
        "idempotency_key": op.get("idempotency_key"),
        "request_required": (request or {}).get("required") if request else None,
        "request_required_fields": (request or {}).get("required_fields") if request else [],
        "request_basic": (request or {}).get("basic") if request else None,
        "request_schema": (request or {}).get("schema") if request else None,
        "status_codes": op.get("status_codes") or [],
        "response_required_fields": {
            code: shape.get("required_fields") or [] for code, shape in success.items()
        },
        "response_schemas": {
            code: shape.get("schema") for code, shape in success.items()
        },
    }

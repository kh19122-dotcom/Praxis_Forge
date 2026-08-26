from __future__ import annotations

from typing import Any

import yaml

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
_MAX_SCHEMA_DEPTH = 8


def parse_json_spec(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("openapi.json is not an object")
    return payload


def parse_yaml_spec(text: str) -> dict[str, Any]:
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError("openapi.yaml is not an object")
    return loaded


def resolve_ref(
    spec: dict[str, Any], node: Any, *, _seen: frozenset[str] | None = None
) -> Any:
    if not isinstance(node, dict) or "$ref" not in node:
        return node
    ref = node["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return {"unresolved_ref": ref}
    seen = _seen or frozenset()
    if ref in seen:
        return {"unresolved_ref": ref}
    target: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(target, dict) or part not in target:
            return {"unresolved_ref": ref}
        target = target[part]
    return resolve_ref(spec, target, _seen=seen | {ref})


def _is_null_schema(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "null"


def normalize_schema(
    spec: dict[str, Any], schema: Any, *, depth: int = 0
) -> dict[str, Any] | None:
    if schema is None or depth > _MAX_SCHEMA_DEPTH:
        return None
    schema = resolve_ref(spec, schema)
    if not isinstance(schema, dict):
        return None
    if "anyOf" in schema or "oneOf" in schema:
        variants = schema.get("anyOf") or schema.get("oneOf") or []
        dict_variants = [item for item in variants if isinstance(item, dict)]
        non_null = [item for item in dict_variants if not _is_null_schema(item)]
        nullable = len(non_null) != len(dict_variants)
        if len(non_null) == 1:
            out = normalize_schema(spec, non_null[0], depth=depth + 1) or {}
            if nullable:
                out["nullable"] = True
            return out
        return {
            "anyOf": [normalize_schema(spec, item, depth=depth + 1) for item in non_null],
            **({"nullable": True} if nullable else {}),
        }

    out: dict[str, Any] = {}
    if schema.get("nullable") is True:
        out["nullable"] = True
    if "type" in schema:
        out["type"] = schema["type"]
    if "const" in schema:
        out["const"] = schema["const"]
    if "enum" in schema and isinstance(schema["enum"], list):
        out["enum"] = sorted(schema["enum"], key=lambda item: repr(item))
    if "pattern" in schema:
        out["pattern"] = schema["pattern"]
    for key in ("minLength", "maxLength", "minimum", "maximum"):
        if key in schema:
            out[key] = _as_int_if_whole(schema[key])
    if (
        "properties" in schema
        or "required" in schema
        or schema.get("type") == "object"
    ):
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            out["properties"] = {
                name: normalize_schema(spec, prop, depth=depth + 1)
                for name, prop in sorted(properties.items())
            }
        required = schema.get("required") or []
        if isinstance(required, list):
            out["required"] = sorted(str(item) for item in required)
    if "items" in schema or schema.get("type") == "array":
        if "items" in schema:
            out["items"] = normalize_schema(spec, schema["items"], depth=depth + 1)
    return out


def basic_shape(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    if not schema:
        return None
    properties = schema.get("properties") or {}
    shaped: dict[str, Any] = {}
    if "type" in schema:
        shaped["type"] = schema["type"]
    if "required" in schema:
        shaped["required"] = list(schema["required"])
    if isinstance(properties, dict) and properties:
        shaped["properties"] = {
            name: {
                key: value
                for key, value in {
                    "type": (prop or {}).get("type") if isinstance(prop, dict) else None,
                    "enum": (prop or {}).get("enum") if isinstance(prop, dict) else None,
                    "pattern": (prop or {}).get("pattern") if isinstance(prop, dict) else None,
                    "const": (prop or {}).get("const") if isinstance(prop, dict) else None,
                    "nullable": (prop or {}).get("nullable") if isinstance(prop, dict) else None,
                }.items()
                if value is not None
            }
            for name, prop in properties.items()
        }
    return shaped or None


def required_fields(schema: dict[str, Any] | None) -> list[str]:
    if not schema:
        return []
    required = schema.get("required") or []
    return list(required) if isinstance(required, list) else []


def extract_operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        return {}
    operations: dict[str, dict[str, Any]] = {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            key = f"{method.upper()} {path}"
            operations[key] = {
                "method": method.upper(),
                "path": path,
                "parameters": _parameters(spec, op),
                "idempotency_key": _idempotency(spec, op),
                "request": _request(spec, op),
                "status_codes": _status_codes(op),
                "response_shapes": _response_shapes(spec, op),
            }
    return dict(sorted(operations.items()))


def _parameters(spec: dict[str, Any], op: dict[str, Any]) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = []
    for raw in op.get("parameters") or []:
        param = resolve_ref(spec, raw)
        if not isinstance(param, dict):
            continue
        name = str(param.get("name", ""))
        location = str(param.get("in", ""))
        entry: dict[str, Any] = {
            "name": name,
            "in": location,
            "required": bool(param.get("required")),
        }
        if location == "header":
            schema = normalize_schema(spec, param.get("schema"))
            if schema:
                entry["schema"] = schema
        params.append(entry)
    return sorted(params, key=lambda item: (item["in"], item["name"]))


def _idempotency(spec: dict[str, Any], op: dict[str, Any]) -> dict[str, Any] | None:
    for param in _parameters(spec, op):
        if param["in"] == "header" and param["name"].lower() == "idempotency-key":
            return {
                "required": bool(param.get("required")),
                "schema": param.get("schema"),
            }
    return None


def _request(spec: dict[str, Any], op: dict[str, Any]) -> dict[str, Any] | None:
    raw = op.get("requestBody")
    if not raw:
        return None
    body = resolve_ref(spec, raw)
    if not isinstance(body, dict):
        return None
    content = body.get("content") or {}
    json_content = content.get("application/json") if isinstance(content, dict) else None
    schema = None
    if isinstance(json_content, dict):
        schema = normalize_schema(spec, json_content.get("schema"))
    return {
        "required": bool(body.get("required")),
        "schema": schema,
        "basic": basic_shape(schema),
        "required_fields": required_fields(schema),
    }


def _status_codes(op: dict[str, Any]) -> list[str]:
    responses = op.get("responses") or {}
    if not isinstance(responses, dict):
        return []
    return sorted(str(code) for code in responses)


def _response_shapes(spec: dict[str, Any], op: dict[str, Any]) -> dict[str, Any]:
    responses = op.get("responses") or {}
    if not isinstance(responses, dict):
        return {}
    shapes: dict[str, Any] = {}
    for code, raw in responses.items():
        response = resolve_ref(spec, raw)
        if not isinstance(response, dict):
            continue
        content = response.get("content") or {}
        if not isinstance(content, dict):
            continue
        json_content = content.get("application/json")
        if not isinstance(json_content, dict):
            continue
        schema = normalize_schema(spec, json_content.get("schema"))
        if schema:
            shapes[str(code)] = {
                "schema": schema,
                "required_fields": required_fields(schema),
                "basic": basic_shape(schema),
            }
    return dict(sorted(shapes.items()))


def _as_int_if_whole(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return value

from __future__ import annotations

from typing import Any

from contract_check.expected import (
    DOCUMENTED_STATUS_CODES,
    IDEMPOTENT_WRITES,
    REQUEST_REQUIRED_FIELDS,
    REQUIRED_OPERATIONS,
    RESPONSE_REQUIRED_FIELDS,
)
from contract_check.fingerprint import digest_snapshot
from contract_check.surface import build_snapshot


def compare_service(
    service: str,
    yaml_spec: dict[str, Any],
    json_spec: dict[str, Any],
    expected_record: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    snapshot = build_snapshot(service, yaml_spec, json_spec)
    mismatches: list[dict[str, Any]] = []
    yaml_ops = set(snapshot["yaml_operations"])
    json_ops = set(snapshot["json_operations"])
    if yaml_ops != json_ops:
        mismatches.append(
            _mismatch(
                service,
                "path_methods",
                detail="path/method set differs between packaged YAML and generated JSON",
                expected=sorted(yaml_ops),
                actual=sorted(json_ops),
            )
        )

    for method, path in REQUIRED_OPERATIONS[service]:
        key = f"{method} {path}"
        if key not in yaml_ops or key not in json_ops:
            mismatches.append(
                _mismatch(
                    service,
                    "required_path",
                    path=path,
                    method=method,
                    detail="required public/admin operation missing from YAML or JSON contract",
                    expected=key,
                    actual={"in_yaml": key in yaml_ops, "in_json": key in json_ops},
                )
            )

    write_method, write_path = IDEMPOTENT_WRITES[service]
    write_key = f"{write_method} {write_path}"
    for source_name in ("yaml", "json"):
        op = (snapshot["operations"].get(write_key) or {}).get(source_name)
        header = (op or {}).get("idempotency_key") if op else None
        if not header or not header.get("required"):
            mismatches.append(
                _mismatch(
                    service,
                    "idempotency_header",
                    path=write_path,
                    method=write_method,
                    detail=(
                        f"{source_name} contract missing required Idempotency-Key header"
                    ),
                    expected={
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": True,
                    },
                    actual=header,
                )
            )

    yaml_header = ((snapshot["operations"].get(write_key) or {}).get("yaml") or {}).get(
        "idempotency_key"
    )
    json_header = ((snapshot["operations"].get(write_key) or {}).get("json") or {}).get(
        "idempotency_key"
    )
    if yaml_header and json_header and yaml_header != json_header:
        mismatches.append(
            _mismatch(
                service,
                "idempotency_header",
                path=write_path,
                method=write_method,
                detail="Idempotency-Key header schema differs between YAML and JSON",
                expected=yaml_header,
                actual=json_header,
            )
        )

    for (method, path), fields in REQUEST_REQUIRED_FIELDS[service].items():
        key = f"{method} {path}"
        for source_name in ("yaml", "json"):
            op = (snapshot["operations"].get(key) or {}).get(source_name) or {}
            actual_fields = op.get("request_required_fields") or []
            missing = [field for field in fields if field not in actual_fields]
            if missing:
                mismatches.append(
                    _mismatch(
                        service,
                        "request_shape",
                        path=path,
                        method=method,
                        detail=(
                            f"{source_name} request body missing required fields {missing}"
                        ),
                        expected=list(fields),
                        actual=actual_fields,
                    )
                )

    for (method, path), codes in DOCUMENTED_STATUS_CODES[service].items():
        key = f"{method} {path}"
        yaml_op = (snapshot["operations"].get(key) or {}).get("yaml") or {}
        actual_codes = yaml_op.get("status_codes") or []
        missing = [code for code in codes if code not in actual_codes]
        if missing:
            mismatches.append(
                _mismatch(
                    service,
                    "status_codes",
                    path=path,
                    method=method,
                    detail="packaged YAML is missing documented status codes",
                    expected=list(codes),
                    actual=actual_codes,
                )
            )

    for (method, path, code), fields in RESPONSE_REQUIRED_FIELDS[service].items():
        key = f"{method} {path}"
        json_op = (snapshot["operations"].get(key) or {}).get("json") or {}
        actual_fields = (json_op.get("response_required_fields") or {}).get(code) or []
        missing = [field for field in fields if field not in actual_fields]
        if missing:
            mismatches.append(
                _mismatch(
                    service,
                    "response_shape",
                    path=path,
                    method=method,
                    detail=(
                        f"runtime JSON response {code} missing required fields {missing}"
                    ),
                    expected=list(fields),
                    actual=actual_fields,
                )
            )

    actual_digest = digest_snapshot(snapshot)
    if expected_record is None:
        mismatches.append(
            _mismatch(
                service,
                "fingerprint",
                detail="committed fingerprint is missing for service",
                expected=None,
                actual=actual_digest,
            )
        )
    else:
        expected_digest = expected_record.get("digest")
        expected_snapshot = expected_record.get("snapshot")
        if expected_digest != actual_digest or expected_snapshot != snapshot:
            mismatches.extend(
                _fingerprint_diffs(service, expected_snapshot or {}, snapshot)
            )
            mismatches.append(
                _mismatch(
                    service,
                    "fingerprint",
                    detail=(
                        "normalized contract fingerprint differs from committed metadata"
                    ),
                    expected=expected_digest,
                    actual=actual_digest,
                )
            )

    return snapshot, mismatches


def _fingerprint_diffs(
    service: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    if expected.get("yaml_operations") != actual.get("yaml_operations") or expected.get(
        "json_operations"
    ) != actual.get("json_operations"):
        diffs.append(
            _mismatch(
                service,
                "path_methods",
                detail="committed path/method set drifted",
                expected={
                    "yaml": expected.get("yaml_operations"),
                    "json": expected.get("json_operations"),
                },
                actual={
                    "yaml": actual.get("yaml_operations"),
                    "json": actual.get("json_operations"),
                },
            )
        )
    expected_ops = expected.get("operations") or {}
    actual_ops = actual.get("operations") or {}
    for key in sorted(set(expected_ops) | set(actual_ops)):
        method, _, path = key.partition(" ")
        exp = expected_ops.get(key) or {}
        act = actual_ops.get(key) or {}
        for source in ("yaml", "json"):
            exp_op = exp.get(source) or {}
            act_op = act.get(source) or {}
            if (exp_op.get("idempotency_key") or None) != (
                act_op.get("idempotency_key") or None
            ):
                diffs.append(
                    _mismatch(
                        service,
                        "idempotency_header",
                        path=path,
                        method=method,
                        detail=(
                            f"{source} Idempotency-Key drifted from committed fingerprint"
                        ),
                        expected=exp_op.get("idempotency_key"),
                        actual=act_op.get("idempotency_key"),
                    )
                )
            if (exp_op.get("request_required_fields") or []) != (
                act_op.get("request_required_fields") or []
            ) or (exp_op.get("request_basic") or None) != (
                act_op.get("request_basic") or None
            ):
                diffs.append(
                    _mismatch(
                        service,
                        "request_shape",
                        path=path,
                        method=method,
                        detail=(
                            f"{source} request shape drifted from committed fingerprint"
                        ),
                        expected={
                            "required_fields": exp_op.get("request_required_fields"),
                            "basic": exp_op.get("request_basic"),
                        },
                        actual={
                            "required_fields": act_op.get("request_required_fields"),
                            "basic": act_op.get("request_basic"),
                        },
                    )
                )
            if (exp_op.get("status_codes") or []) != (act_op.get("status_codes") or []):
                diffs.append(
                    _mismatch(
                        service,
                        "status_codes",
                        path=path,
                        method=method,
                        detail=(
                            f"{source} documented status codes drifted "
                            "from committed fingerprint"
                        ),
                        expected=exp_op.get("status_codes"),
                        actual=act_op.get("status_codes"),
                    )
                )
            if (exp_op.get("response_required_fields") or {}) != (
                act_op.get("response_required_fields") or {}
            ):
                diffs.append(
                    _mismatch(
                        service,
                        "response_shape",
                        path=path,
                        method=method,
                        detail=(
                            f"{source} response required fields drifted "
                            "from committed fingerprint"
                        ),
                        expected=exp_op.get("response_required_fields"),
                        actual=act_op.get("response_required_fields"),
                    )
                )
    return diffs


def _mismatch(
    service: str,
    dimension: str,
    *,
    detail: str,
    expected: Any,
    actual: Any,
    path: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    item = {
        "service": service,
        "dimension": dimension,
        "detail": detail,
        "expected": expected,
        "actual": actual,
    }
    if path is not None:
        item["path"] = path
    if method is not None:
        item["method"] = method
    return item

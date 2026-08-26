from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from contract_check.compare import compare_service
from contract_check.fingerprint import (
    DEFAULT_FINGERPRINT_PATH,
    build_document,
    digest_snapshot,
    load_fingerprint,
)
from contract_check.fingerprint import SCHEMA as FINGERPRINT_SCHEMA
from contract_check.http import DEFAULT_TIMEOUT_SECONDS, FetchError, fetch_service
from contract_check.normalize import parse_json_spec, parse_yaml_spec
from contract_check.surface import CANONICAL, COMPARED_DIMENSIONS, IGNORED_DIMENSIONS

REPORT_SCHEMA = "praxis-forge.contract-check.v1"
SERVICES = ("fake-booking", "fake-pvs")


def check_contracts(
    booking_url: str,
    pvs_url: str,
    *,
    fingerprint_path: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    clients: dict[str, httpx.Client] | None = None,
    update_fingerprint: bool = False,
) -> dict[str, Any]:
    path = fingerprint_path or DEFAULT_FINGERPRINT_PATH
    expected_doc: dict[str, Any] | None = None
    load_error: str | None = None
    if path.is_file():
        try:
            expected_doc = load_fingerprint(path)
        except (OSError, ValueError) as exc:
            load_error = str(exc)
    elif not update_fingerprint:
        load_error = f"committed fingerprint missing: {path}"

    urls = {"fake-booking": booking_url, "fake-pvs": pvs_url}
    services: dict[str, Any] = {}
    snapshots: dict[str, dict[str, Any]] = {}
    mismatches: list[dict[str, Any]] = []

    if load_error and not update_fingerprint:
        mismatches.append(
            {
                "service": None,
                "dimension": "fingerprint",
                "detail": load_error,
                "expected": FINGERPRINT_SCHEMA,
                "actual": None,
            }
        )

    for service in SERVICES:
        try:
            fetched = fetch_service(
                service,
                urls[service],
                timeout=timeout,
                client=None if clients is None else clients.get(service),
            )
            yaml_spec = parse_yaml_spec(fetched["openapi_yaml_text"])
            json_spec = parse_json_spec(fetched["openapi_json"])
        except (FetchError, ValueError) as exc:
            detail = (
                exc.detail
                if isinstance(exc, FetchError)
                else f"{service} contract parse failed: {exc}"
            )
            path_name = exc.path if isinstance(exc, FetchError) else "/openapi.json"
            item = {
                "service": service,
                "dimension": "fetch",
                "path": path_name,
                "detail": detail,
                "expected": 200,
                "actual": None,
            }
            mismatches.append(item)
            services[service] = {
                "status": "fail",
                "url": urls[service].rstrip("/"),
                "fetched": {},
                "probes": [],
                "digest": None,
                "mismatches": [item],
            }
            continue

        expected_record = None
        if expected_doc is not None and not update_fingerprint:
            record = (expected_doc.get("services") or {}).get(service)
            if isinstance(record, dict):
                expected_record = record
        snapshot, service_mismatches = compare_service(
            service, yaml_spec, json_spec, expected_record
        )
        snapshots[service] = snapshot
        digest = digest_snapshot(snapshot)
        services[service] = {
            "status": "pass" if not service_mismatches else "fail",
            "url": fetched["url"],
            "fetched": fetched["fetched"],
            "probes": fetched["probes"],
            "digest": digest,
            "mismatches": service_mismatches,
        }
        mismatches.extend(service_mismatches)

    if update_fingerprint and len(snapshots) == len(SERVICES):
        document = build_document(snapshots, canonical=CANONICAL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        for service, snapshot in snapshots.items():
            remaining = [
                item
                for item in services[service]["mismatches"]
                if item.get("dimension") != "fingerprint"
            ]
            services[service]["mismatches"] = remaining
            services[service]["status"] = "pass" if not remaining else "fail"
            services[service]["digest"] = digest_snapshot(snapshot)
        mismatches = [
            item for item in mismatches if item.get("dimension") != "fingerprint"
        ]

    status = (
        "pass"
        if not mismatches
        and all(item.get("status") == "pass" for item in services.values())
        else "fail"
    )
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "canonical": CANONICAL,
        "compared": list(COMPARED_DIMENSIONS),
        "ignored": list(IGNORED_DIMENSIONS),
        "fingerprint_file": str(path),
        "services": services,
        "mismatches": mismatches,
    }

from __future__ import annotations

import copy

import pytest
import yaml

from contract_check.compare import compare_service
from contract_check.fingerprint import digest_snapshot, load_fingerprint, service_record
from contract_check.normalize import extract_operations, parse_yaml_spec
from contract_check.surface import COMPARED_DIMENSIONS, IGNORED_DIMENSIONS, build_snapshot
from tests.fakes import SpecServer, load_fixture, load_json_spec
from tests.test_check import _run, _seed_fingerprint

SCHEMA_COMPLETENESS_CASES = (
    ("fake-booking", "GET", "/v1/slots", "200"),
    ("fake-pvs", "GET", "/v1/patients", "200"),
)

REPLAY_ONLY_CASES = (
    ("fake-booking", "POST", "/v1/bookings", "200"),
    ("fake-pvs", "POST", "/v1/tasks", "200"),
)


def _mismatch_hit(
    report: dict, *, dimension: str, service: str, path: str, method: str
) -> list[dict]:
    return [
        item
        for item in report["mismatches"]
        if item["dimension"] == dimension
        and item["service"] == service
        and item.get("path") == path
        and item.get("method") == method
    ]


def _spec_pair(service: str) -> tuple[dict, dict]:
    return (
        parse_yaml_spec(load_fixture(service, "yaml")),
        load_json_spec(service),
    )


def _remove_json_response_schema(
    spec: dict, *, method: str, path: str, code: str
) -> None:
    del spec["paths"][path][method.lower()]["responses"][code]["content"][
        "application/json"
    ]["schema"]


def _direct_response_hits(
    mismatches: list[dict], *, service: str, method: str, path: str, code: str
) -> list[dict]:
    return [
        item
        for item in mismatches
        if item["dimension"] == "response_shape"
        and item["service"] == service
        and item.get("method") == method
        and item.get("path") == path
        and f"JSON {code} response schema differs" in item["detail"]
        and "packaged YAML and generated JSON" in item["detail"]
    ]


@pytest.mark.parametrize("service,method,path,code", SCHEMA_COMPLETENESS_CASES)
def test_shared_json_success_schema_presence_baseline(
    service: str, method: str, path: str, code: str
) -> None:
    yaml_spec, json_spec = _spec_pair(service)
    snapshot, mismatches = compare_service(
        service,
        yaml_spec,
        json_spec,
        load_fingerprint()["services"][service],
    )
    assert not _direct_response_hits(
        mismatches, service=service, method=method, path=path, code=code
    )
    operation = snapshot["operations"][f"{method} {path}"]
    assert operation["yaml"]["response_schemas"][code] is not None
    assert operation["json"]["response_schemas"][code] is not None


@pytest.mark.parametrize("service,method,path,code", SCHEMA_COMPLETENESS_CASES)
def test_runtime_advertised_json_success_without_schema_is_direct_mismatch(
    service: str, method: str, path: str, code: str
) -> None:
    yaml_spec, json_spec = _spec_pair(service)
    _remove_json_response_schema(json_spec, method=method, path=path, code=code)
    snapshot, mismatches = compare_service(
        service,
        yaml_spec,
        json_spec,
        load_fingerprint()["services"][service],
    )
    operation = snapshot["operations"][f"{method} {path}"]
    assert code in operation["json"]["response_schemas"]
    assert operation["json"]["response_schemas"][code] is None
    hits = _direct_response_hits(
        mismatches, service=service, method=method, path=path, code=code
    )
    assert hits, mismatches
    assert hits[0]["expected"] is not None
    assert hits[0]["actual"] is None


@pytest.mark.parametrize("service,method,path,code", SCHEMA_COMPLETENESS_CASES)
def test_yaml_advertised_json_success_without_schema_is_direct_mismatch(
    service: str, method: str, path: str, code: str
) -> None:
    yaml_spec, json_spec = _spec_pair(service)
    _remove_json_response_schema(yaml_spec, method=method, path=path, code=code)
    snapshot, mismatches = compare_service(
        service,
        yaml_spec,
        json_spec,
        load_fingerprint()["services"][service],
    )
    operation = snapshot["operations"][f"{method} {path}"]
    assert code in operation["yaml"]["response_schemas"]
    assert operation["yaml"]["response_schemas"][code] is None
    hits = _direct_response_hits(
        mismatches, service=service, method=method, path=path, code=code
    )
    assert hits, mismatches
    assert hits[0]["expected"] is None
    assert hits[0]["actual"] is not None


@pytest.mark.parametrize("missing_from", ("yaml", "json"))
@pytest.mark.parametrize("service,method,path,code", SCHEMA_COMPLETENESS_CASES)
def test_self_consistent_asymmetric_fingerprint_cannot_mask_direct_response_mismatch(
    missing_from: str, service: str, method: str, path: str, code: str
) -> None:
    yaml_spec, json_spec = _spec_pair(service)
    target = yaml_spec if missing_from == "yaml" else json_spec
    _remove_json_response_schema(target, method=method, path=path, code=code)
    asymmetric_snapshot = build_snapshot(service, yaml_spec, json_spec)

    _snapshot, mismatches = compare_service(
        service,
        yaml_spec,
        json_spec,
        service_record(asymmetric_snapshot),
    )

    assert not [item for item in mismatches if item["dimension"] == "fingerprint"]
    assert _direct_response_hits(
        mismatches, service=service, method=method, path=path, code=code
    ), mismatches


def test_current_shared_json_success_responses_have_meaningful_schemas() -> None:
    for service in ("fake-booking", "fake-pvs"):
        yaml_spec, json_spec = _spec_pair(service)
        yaml_operations = extract_operations(yaml_spec)
        json_operations = extract_operations(json_spec)
        shared_pairs: list[tuple[str, str]] = []
        for operation_key in sorted(set(yaml_operations) & set(json_operations)):
            yaml_responses = yaml_operations[operation_key]["response_shapes"]
            json_responses = json_operations[operation_key]["response_shapes"]
            for code in sorted(set(yaml_responses) & set(json_responses)):
                if not code.startswith("2"):
                    continue
                shared_pairs.append((operation_key, code))
                assert yaml_responses[code]["schema"] is not None, (
                    service,
                    operation_key,
                    code,
                    "yaml",
                )
                assert json_responses[code]["schema"] is not None, (
                    service,
                    operation_key,
                    code,
                    "json",
                )
        assert shared_pairs, service


@pytest.mark.parametrize("service,method,path,code", REPLAY_ONLY_CASES)
def test_yaml_only_replay_success_code_remains_ignored(
    service: str, method: str, path: str, code: str
) -> None:
    yaml_spec, json_spec = _spec_pair(service)
    snapshot, mismatches = compare_service(
        service,
        yaml_spec,
        json_spec,
        load_fingerprint()["services"][service],
    )
    operation = snapshot["operations"][f"{method} {path}"]
    assert operation["yaml"]["response_schemas"][code] is not None
    assert code not in operation["json"]["response_schemas"]
    assert not _direct_response_hits(
        mismatches, service=service, method=method, path=path, code=code
    )


def test_compared_and_ignored_dimensions_are_explicit() -> None:
    assert "parameter_schema" in COMPARED_DIMENSIONS
    assert "request_shape" in COMPARED_DIMENSIONS
    assert "response_shape" in COMPARED_DIMENSIONS
    assert "fingerprint" in COMPARED_DIMENSIONS
    assert any("HTTPValidationError" in item for item in IGNORED_DIMENSIONS)
    assert any("full OpenAPI" not in item.lower() for item in IGNORED_DIMENSIONS)


def test_task_id_type_change_is_response_shape_mismatch(tmp_path) -> None:
    baseline = _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-pvs")
    mutated["components"]["schemas"]["Task"]["properties"]["id"]["type"] = "integer"
    code, report = _run(tmp_path, SpecServer(pvs_json=mutated))
    assert code == 1
    hit = _mismatch_hit(
        report,
        dimension="response_shape",
        service="fake-pvs",
        path="/v1/tasks",
        method="POST",
    )
    assert hit, report["mismatches"]
    assert any(
        (item.get("expected") or {}).get("type") == "string"
        or "string" in str(item.get("expected"))
        for item in hit
    )
    assert report["services"]["fake-pvs"]["digest"] != baseline["services"]["fake-pvs"]["digest"]


def test_incompatible_query_parameter_pattern_is_parameter_schema_mismatch(
    tmp_path,
) -> None:
    baseline = _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-pvs")
    for param in mutated["paths"]["/v1/patients"]["get"]["parameters"]:
        if param["name"] == "id":
            param["schema"]["anyOf"][0]["pattern"] = "^broken-[a-z]+$"
            break
    else:
        raise AssertionError("id query parameter missing from PVS fixture")
    code, report = _run(tmp_path, SpecServer(pvs_json=mutated))
    assert code == 1
    hit = _mismatch_hit(
        report,
        dimension="parameter_schema",
        service="fake-pvs",
        path="/v1/patients",
        method="GET",
    )
    assert hit, report["mismatches"]
    assert "broken-[a-z]+" in str(hit[0]["actual"]) or "broken-[a-z]+" in str(hit[0])
    assert report["services"]["fake-pvs"]["digest"] != baseline["services"]["fake-pvs"]["digest"]


def test_parameter_requiredness_change_is_parameter_schema_mismatch(tmp_path) -> None:
    baseline = _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-booking")
    for param in mutated["paths"]["/v1/slots"]["get"]["parameters"]:
        if param["name"] == "resource_id":
            param["required"] = True
            break
    else:
        raise AssertionError("resource_id query parameter missing from booking fixture")
    code, report = _run(tmp_path, SpecServer(booking_json=mutated))
    assert code == 1
    hit = _mismatch_hit(
        report,
        dimension="parameter_schema",
        service="fake-booking",
        path="/v1/slots",
        method="GET",
    )
    assert hit, report["mismatches"]
    booking_digest = report["services"]["fake-booking"]["digest"]
    assert booking_digest != baseline["services"]["fake-booking"]["digest"]


def test_request_property_constraint_change_is_request_shape_mismatch(tmp_path) -> None:
    baseline = _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-booking")
    mutated["components"]["schemas"]["BookingCreate"]["properties"]["slot_id"]["minLength"] = 99
    code, report = _run(tmp_path, SpecServer(booking_json=mutated))
    assert code == 1
    hit = _mismatch_hit(
        report,
        dimension="request_shape",
        service="fake-booking",
        path="/v1/bookings",
        method="POST",
    )
    assert hit, report["mismatches"]
    assert "99" in str(hit[0]["actual"]) or "99" in str(hit[0])
    booking_digest = report["services"]["fake-booking"]["digest"]
    assert booking_digest != baseline["services"]["fake-booking"]["digest"]


def test_nested_success_response_schema_change_is_response_shape_mismatch(tmp_path) -> None:
    baseline = _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-booking")
    mutated["components"]["schemas"]["Slot"]["properties"]["available"]["type"] = "string"
    code, report = _run(tmp_path, SpecServer(booking_json=mutated))
    assert code == 1
    hit = _mismatch_hit(
        report,
        dimension="response_shape",
        service="fake-booking",
        path="/v1/slots",
        method="GET",
    )
    assert hit, report["mismatches"]
    booking_digest = report["services"]["fake-booking"]["digest"]
    assert booking_digest != baseline["services"]["fake-booking"]["digest"]


def test_snapshot_retains_parameter_request_and_success_response_schemas() -> None:
    booking_yaml = parse_yaml_spec(load_fixture("fake-booking", "yaml"))
    booking_json = load_json_spec("fake-booking")
    snapshot = build_snapshot("fake-booking", booking_yaml, booking_json)
    create = snapshot["operations"]["POST /v1/bookings"]["json"]
    patient_ref = create["request_schema"]["properties"]["patient_ref"]
    assert patient_ref["pattern"] == "^synth-[a-z0-9-]+$"
    names = {item["name"] for item in create["parameters"]}
    assert "Idempotency-Key" in names
    slots = snapshot["operations"]["GET /v1/slots"]["json"]
    resource = next(item for item in slots["parameters"] if item["name"] == "resource_id")
    assert resource["in"] == "query"
    assert resource["required"] is False
    assert resource["schema"]["type"] == "string"
    slot_props = slots["response_schemas"]["200"]["properties"]["slots"]["items"]["properties"]
    assert "available" in slot_props
    pvs_yaml = parse_yaml_spec(load_fixture("fake-pvs", "yaml"))
    pvs_json = load_json_spec("fake-pvs")
    pvs = build_snapshot("fake-pvs", pvs_yaml, pvs_json)
    task = pvs["operations"]["POST /v1/tasks"]["json"]
    assert task["response_schemas"]["201"]["properties"]["id"]["type"] == "string"
    patients = pvs["operations"]["GET /v1/patients"]["json"]
    query_id = next(item for item in patients["parameters"] if item["name"] == "id")
    assert query_id["schema"]["pattern"] == "^synth-[a-z0-9-]+$"


def test_yaml_and_runtime_success_schemas_are_compared_directly(tmp_path) -> None:
    _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-pvs")
    mutated["components"]["schemas"]["Task"]["properties"]["id"]["type"] = "integer"
    code, report = _run(tmp_path, SpecServer(pvs_json=mutated))
    assert code == 1
    yaml_vs_runtime = [
        item
        for item in report["mismatches"]
        if item["dimension"] == "response_shape"
        and item["service"] == "fake-pvs"
        and "packaged YAML and generated JSON" in item["detail"]
    ]
    assert yaml_vs_runtime, report["mismatches"]


def test_cosmetic_schema_titles_examples_and_422_envelope_do_not_drift(tmp_path) -> None:
    baseline = _seed_fingerprint(tmp_path)
    booking_json = load_json_spec("fake-booking")
    pvs_json = load_json_spec("fake-pvs")
    booking_yaml = parse_yaml_spec(load_fixture("fake-booking", "yaml"))
    pvs_yaml = parse_yaml_spec(load_fixture("fake-pvs", "yaml"))
    original_booking = build_snapshot("fake-booking", booking_yaml, booking_json)
    original_pvs = build_snapshot("fake-pvs", pvs_yaml, pvs_json)

    mutated_booking_json = copy.deepcopy(booking_json)
    mutated_booking_json["components"]["schemas"]["Booking"]["title"] = "Cosmetic Booking"
    mutated_booking_json["components"]["schemas"]["Booking"]["example"] = {"id": "ignored"}
    mutated_booking_json["components"]["schemas"]["Event"]["properties"]["details"][
        "additionalProperties"
    ] = False
    mutated_booking_json["components"]["schemas"]["HTTPValidationError"]["properties"]["detail"][
        "title"
    ] = "Cosmetic Detail"
    mutated_booking_json["paths"]["/v1/slots"]["get"]["responses"]["422"]["description"] = "noise"
    mutated_pvs_json = copy.deepcopy(pvs_json)
    mutated_pvs_json["components"]["schemas"]["Task"]["title"] = "Cosmetic Task"
    mutated_pvs_json["components"]["schemas"]["Task"]["examples"] = [{"id": "ignored"}]
    mutated_pvs_json["info"]["x-ignored"] = "vendor-extension"

    mutated_booking_yaml = copy.deepcopy(booking_yaml)
    mutated_booking_yaml["components"]["schemas"]["Booking"]["title"] = "YAML Cosmetic Booking"
    mutated_booking_yaml["servers"] = [{"url": "http://example.invalid"}]
    mutated_pvs_yaml = copy.deepcopy(pvs_yaml)
    mutated_pvs_yaml["components"]["schemas"]["Task"]["title"] = "YAML Cosmetic Task"

    mutated_booking = build_snapshot(
        "fake-booking", mutated_booking_yaml, mutated_booking_json
    )
    mutated_pvs = build_snapshot("fake-pvs", mutated_pvs_yaml, mutated_pvs_json)
    assert digest_snapshot(mutated_booking) == digest_snapshot(original_booking)
    assert digest_snapshot(mutated_pvs) == digest_snapshot(original_pvs)

    code, report = _run(
        tmp_path,
        SpecServer(
            booking_json=mutated_booking_json,
            booking_yaml=yaml.safe_dump(mutated_booking_yaml, sort_keys=False),
            pvs_json=mutated_pvs_json,
            pvs_yaml=yaml.safe_dump(mutated_pvs_yaml, sort_keys=False),
        ),
    )
    assert code == 0, report["mismatches"]
    assert report["status"] == "pass"
    assert (
        report["services"]["fake-booking"]["digest"]
        == baseline["services"]["fake-booking"]["digest"]
    )
    assert report["services"]["fake-pvs"]["digest"] == baseline["services"]["fake-pvs"]["digest"]

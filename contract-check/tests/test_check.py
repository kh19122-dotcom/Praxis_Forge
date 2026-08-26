from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

from contract_check.check import REPORT_SCHEMA, check_contracts
from contract_check.cli import main
from contract_check.fingerprint import SCHEMA as FINGERPRINT_SCHEMA
from contract_check.fingerprint import digest_snapshot
from contract_check.normalize import parse_yaml_spec
from contract_check.surface import build_snapshot
from tests.fakes import SpecServer, clients_for, load_fixture, load_json_spec


def _run(tmp_path: Path, server: SpecServer, *, update: bool = False) -> tuple[int, dict]:
    fingerprint = tmp_path / "fingerprints.json"
    if not fingerprint.exists() and not update:
        raise AssertionError("test must seed a fingerprint unless updating")
    report = check_contracts(
        "http://booking.test",
        "http://pvs.test",
        fingerprint_path=fingerprint,
        clients=clients_for(server),
        update_fingerprint=update,
    )
    return (0 if report["status"] == "pass" else 1), report


def _seed_fingerprint(tmp_path: Path) -> dict:
    code, report = _run(tmp_path, SpecServer(), update=True)
    assert code == 0, report
    assert report["status"] == "pass"
    return report


def test_live_contracts_match_committed_fingerprint(tmp_path: Path) -> None:
    first = _seed_fingerprint(tmp_path)
    code, report = _run(tmp_path, SpecServer())
    assert code == 0
    assert report["schema"] == REPORT_SCHEMA
    assert report["status"] == "pass"
    assert report["mismatches"] == []
    assert report["services"]["fake-booking"]["status"] == "pass"
    assert report["services"]["fake-pvs"]["status"] == "pass"
    assert report["services"]["fake-booking"]["fetched"] == {
        "/openapi.json": 200,
        "/openapi.yaml": 200,
    }
    assert report["services"]["fake-pvs"]["fetched"] == {
        "/openapi.json": 200,
        "/openapi.yaml": 200,
    }
    assert (
        first["services"]["fake-booking"]["digest"]
        == report["services"]["fake-booking"]["digest"]
    )
    assert (
        first["services"]["fake-pvs"]["digest"]
        == report["services"]["fake-pvs"]["digest"]
    )


def test_fingerprint_file_is_deterministic(tmp_path: Path) -> None:
    _seed_fingerprint(tmp_path)
    first = (tmp_path / "fingerprints.json").read_text(encoding="utf-8")
    _seed_fingerprint(tmp_path)
    second = (tmp_path / "fingerprints.json").read_text(encoding="utf-8")
    assert first == second
    payload = json.loads(first)
    assert payload["schema"] == FINGERPRINT_SCHEMA
    for name, record in payload["services"].items():
        assert record["digest"] == digest_snapshot(record["snapshot"])
        assert record["snapshot"]["service"] == name


def test_packaged_fingerprint_matches_fixtures() -> None:
    from contract_check.compare import compare_service
    from contract_check.fingerprint import DEFAULT_FINGERPRINT_PATH, load_fingerprint
    from contract_check.normalize import parse_json_spec, parse_yaml_spec
    from tests.fakes import load_fixture

    expected = load_fingerprint()
    assert DEFAULT_FINGERPRINT_PATH.is_file()
    for service in ("fake-booking", "fake-pvs"):
        yaml_spec = parse_yaml_spec(load_fixture(service, "yaml"))
        json_spec = parse_json_spec(load_json_spec(service))
        _snapshot, mismatches = compare_service(
            service, yaml_spec, json_spec, expected["services"][service]
        )
        assert mismatches == []


def test_harmless_openapi_info_metadata_does_not_change_digest(tmp_path: Path) -> None:
    baseline = _seed_fingerprint(tmp_path)
    booking_json = load_json_spec("fake-booking")
    pvs_json = load_json_spec("fake-pvs")
    booking_yaml = parse_yaml_spec(load_fixture("fake-booking", "yaml"))
    pvs_yaml = parse_yaml_spec(load_fixture("fake-pvs", "yaml"))
    original_booking = build_snapshot("fake-booking", booking_yaml, booking_json)
    original_pvs = build_snapshot("fake-pvs", pvs_yaml, pvs_json)

    mutated_booking_json = copy.deepcopy(booking_json)
    mutated_booking_json["info"]["title"] = "Harmless Booking Title"
    mutated_booking_json["info"]["description"] = "Harmless booking description."
    mutated_booking_json["info"]["version"] = "9.9.9"
    mutated_pvs_json = copy.deepcopy(pvs_json)
    mutated_pvs_json["info"]["title"] = "Harmless PVS Title"
    mutated_pvs_json["info"]["description"] = "Harmless pvs description."
    mutated_pvs_json["info"]["version"] = "9.9.9"
    mutated_booking_yaml = copy.deepcopy(booking_yaml)
    mutated_booking_yaml["info"]["title"] = "Harmless YAML Booking Title"
    mutated_booking_yaml["info"]["description"] = "Harmless YAML booking description."
    mutated_booking_yaml["info"]["version"] = "8.8.8"
    mutated_pvs_yaml = copy.deepcopy(pvs_yaml)
    mutated_pvs_yaml["info"]["title"] = "Harmless YAML PVS Title"
    mutated_pvs_yaml["info"]["description"] = "Harmless YAML pvs description."
    mutated_pvs_yaml["info"]["version"] = "8.8.8"

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
    assert code == 0
    assert report["status"] == "pass"
    assert report["mismatches"] == []
    assert (
        report["services"]["fake-booking"]["digest"]
        == baseline["services"]["fake-booking"]["digest"]
    )
    assert (
        report["services"]["fake-pvs"]["digest"]
        == baseline["services"]["fake-pvs"]["digest"]
    )


def test_missing_path_is_mismatch(tmp_path: Path) -> None:
    _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-booking")
    del mutated["paths"]["/v1/bookings"]
    code, report = _run(tmp_path, SpecServer(booking_json=mutated))
    assert code == 1
    assert report["status"] == "fail"
    dimensions = {item["dimension"] for item in report["mismatches"]}
    assert "path_methods" in dimensions or "required_path" in dimensions
    assert any(
        item.get("service") == "fake-booking"
        and item.get("path") in {None, "/v1/bookings"}
        for item in report["mismatches"]
    )
    assert report["services"]["fake-booking"]["status"] == "fail"


def test_idempotency_header_mismatch_is_nonzero(tmp_path: Path) -> None:
    _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-pvs")
    mutated["paths"]["/v1/tasks"]["post"]["parameters"] = []
    code, report = _run(tmp_path, SpecServer(pvs_json=mutated))
    assert code == 1
    hit = [
        item
        for item in report["mismatches"]
        if item["dimension"] == "idempotency_header"
        and item["service"] == "fake-pvs"
        and item.get("path") == "/v1/tasks"
        and item.get("method") == "POST"
    ]
    assert hit, report["mismatches"]
    assert (
        "Idempotency-Key" in hit[0]["detail"]
        or hit[0]["expected"]["name"] == "Idempotency-Key"
    )


def test_request_shape_mismatch_identifies_fields(tmp_path: Path) -> None:
    _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-booking")
    mutated["components"]["schemas"]["BookingCreate"]["required"] = ["slot_id"]
    code, report = _run(tmp_path, SpecServer(booking_json=mutated))
    assert code == 1
    hit = [
        item
        for item in report["mismatches"]
        if item["dimension"] == "request_shape"
        and item["service"] == "fake-booking"
        and item.get("path") == "/v1/bookings"
        and item.get("method") == "POST"
    ]
    assert hit, report["mismatches"]
    assert "patient_ref" in str(hit[0]["expected"]) or "patient_ref" in str(
        hit[0]["detail"]
    )


def test_documented_status_code_mismatch(tmp_path: Path) -> None:
    _seed_fingerprint(tmp_path)
    yaml_text = (
        Path(__file__)
        .parent.joinpath("fixtures/fake-booking.openapi.yaml")
        .read_text(encoding="utf-8")
    )
    yaml_text = yaml_text.replace('"504":', '"599":')
    code, report = _run(tmp_path, SpecServer(booking_yaml=yaml_text))
    assert code == 1
    hit = [
        item
        for item in report["mismatches"]
        if item["dimension"] == "status_codes"
        and item["service"] == "fake-booking"
        and item.get("path") == "/v1/bookings"
        and item.get("method") == "POST"
    ]
    assert hit, report["mismatches"]
    assert "504" in str(hit[0]["expected"])


def test_response_shape_mismatch(tmp_path: Path) -> None:
    _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-pvs")
    mutated["components"]["schemas"]["Task"]["required"] = ["id"]
    code, report = _run(tmp_path, SpecServer(pvs_json=mutated))
    assert code == 1
    hit = [
        item
        for item in report["mismatches"]
        if item["dimension"] == "response_shape"
        and item["service"] == "fake-pvs"
        and item.get("path") == "/v1/tasks"
        and item.get("method") == "POST"
    ]
    assert hit, report["mismatches"]


def test_fetch_failure_is_nonzero(tmp_path: Path) -> None:
    _seed_fingerprint(tmp_path)
    code, report = _run(
        tmp_path,
        SpecServer(booking_status={"/openapi.json": 500}),
    )
    assert code == 1
    hit = [item for item in report["mismatches"] if item["dimension"] == "fetch"]
    assert hit
    assert hit[0]["service"] == "fake-booking"
    assert hit[0]["path"] == "/openapi.json"


def test_cli_exits_nonzero_on_mismatch(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    fingerprint = tmp_path / "fingerprints.json"
    _seed_fingerprint(tmp_path)
    mutated = load_json_spec("fake-booking")
    del mutated["paths"]["/v1/slots"]
    server = SpecServer(booking_json=mutated)
    clients = clients_for(server)

    def fake_check(*_args, **kwargs):
        kwargs.pop("update_fingerprint", None)
        return check_contracts(
            "http://booking.test",
            "http://pvs.test",
            fingerprint_path=fingerprint,
            clients=clients,
        )

    monkeypatch.setattr("contract_check.cli.check_contracts", fake_check)
    code = main(["--fingerprint-file", str(fingerprint)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == "fail"
    assert payload["mismatches"]
    assert any(item.get("service") == "fake-booking" for item in payload["mismatches"])


def test_packaged_and_generated_specs_are_both_inspected(tmp_path: Path) -> None:
    _seed_fingerprint(tmp_path)
    yaml_only = copy.deepcopy(load_json_spec("fake-booking"))
    # Keep JSON paths intact; break YAML path set.
    broken_yaml = (
        Path(__file__)
        .parent.joinpath("fixtures/fake-booking.openapi.yaml")
        .read_text(encoding="utf-8")
        .replace("  /v1/slots:\n", "  /v1/slots-renamed:\n")
    )
    code, report = _run(
        tmp_path, SpecServer(booking_json=yaml_only, booking_yaml=broken_yaml)
    )
    assert code == 1
    assert any(
        item["service"] == "fake-booking"
        and item["dimension"] in {"path_methods", "required_path"}
        for item in report["mismatches"]
    )

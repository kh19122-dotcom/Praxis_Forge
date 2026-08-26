from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scenario_runner.cli import main
from scenario_runner.http import ServiceClient
from scenario_runner.report import SuiteReport
from scenario_runner.runner import list_scenario_names, run_suite
from scenario_runner.scenarios import SCENARIOS
from scenario_runner.transport import TRANSPORT_SCENARIOS
from tests.fake_services import FakeForge

REQUIRED_SCENARIOS = {
    "combined-happy-path",
    "booking-fail-before-commit",
    "booking-ambiguous-recovery",
    "pvs-fail-before-commit",
    "pvs-ambiguous-recovery",
    "conflict-idempotency",
}

REQUIRED_TRANSPORT_SCENARIOS = {
    "booking-transport-drop-before-upstream",
    "booking-transport-drop-after-upstream",
    "pvs-transport-drop-after-upstream",
}


def test_named_scenarios_are_registered() -> None:
    assert set(list_scenario_names()) == REQUIRED_SCENARIOS
    assert list(SCENARIOS) == list(list_scenario_names())
    assert set(list_scenario_names("transport-chaos")) == REQUIRED_TRANSPORT_SCENARIOS
    assert list(TRANSPORT_SCENARIOS) == list(list_scenario_names("transport-chaos"))


def test_all_scenarios_pass_against_http_fakes(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
) -> None:
    report = run_suite(
        "http://booking.test",
        "http://pvs.test",
        booking_client=booking_client,
        pvs_client=pvs_client,
    )
    assert report.status == "pass"
    assert report.error is None
    assert report.booking_seed == "obj-001"
    assert report.pvs_seed == "obj-002"
    assert [scenario.name for scenario in report.scenarios] == list_scenario_names()
    assert all(scenario.status == "pass" for scenario in report.scenarios)
    for scenario in report.scenarios:
        assert scenario.steps
        assert all(step.status == "pass" for step in scenario.steps)
        assert any(step.trace_id for step in scenario.steps)


def test_happy_path_records_ids_from_both_services(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
) -> None:
    report = run_suite(
        "http://booking.test",
        "http://pvs.test",
        names=["combined-happy-path"],
        booking_client=booking_client,
        pvs_client=pvs_client,
    )
    scenario = report.scenarios[0]
    assert scenario.status == "pass"
    assert scenario.ids["booking_id"].startswith("bkg_")
    assert scenario.ids["task_id"].startswith("tsk_")
    assert scenario.ids["slot_id"]
    assert scenario.ids["booking_trace_id"].startswith("tr_")
    assert scenario.ids["task_trace_id"].startswith("tr_")


def test_transport_chaos_scenarios_pass_against_http_fakes(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
    booking_chaos_client: ServiceClient,
    pvs_chaos_client: ServiceClient,
    booking_chaos_admin_client: ServiceClient,
    pvs_chaos_admin_client: ServiceClient,
) -> None:
    report = run_suite(
        "http://booking.test",
        "http://pvs.test",
        suite="transport-chaos",
        booking_client=booking_client,
        pvs_client=pvs_client,
        booking_chaos_client=booking_chaos_client,
        pvs_chaos_client=pvs_chaos_client,
        booking_chaos_admin_client=booking_chaos_admin_client,
        pvs_chaos_admin_client=pvs_chaos_admin_client,
        booking_chaos_url="http://booking-chaos.test",
        pvs_chaos_url="http://pvs-chaos.test",
    )
    assert report.status == "pass"
    assert report.error is None
    assert [scenario.name for scenario in report.scenarios] == list(
        list_scenario_names("transport-chaos")
    )
    assert all(scenario.status == "pass" for scenario in report.scenarios)
    drop_before = next(
        scenario
        for scenario in report.scenarios
        if scenario.name == "booking-transport-drop-before-upstream"
    )
    drop_after = next(
        scenario
        for scenario in report.scenarios
        if scenario.name == "booking-transport-drop-after-upstream"
    )
    pvs_drop = next(
        scenario
        for scenario in report.scenarios
        if scenario.name == "pvs-transport-drop-after-upstream"
    )
    assert drop_before.ids["booking_id"].startswith("bkg_")
    assert drop_after.ids["booking_id"].startswith("bkg_")
    assert pvs_drop.ids["task_id"].startswith("tsk_")


def test_transport_chaos_requires_proxy_urls() -> None:
    report = run_suite(
        "http://booking.test",
        "http://pvs.test",
        suite="transport-chaos",
    )
    assert report.status == "fail"
    assert report.error
    assert "chaos" in report.error


def test_unknown_scenario_fails_without_http() -> None:
    report = run_suite("http://booking.test", "http://pvs.test", names=["not-a-scenario"])
    assert report.status == "fail"
    assert report.error == "unknown scenario(s): not-a-scenario"
    assert report.scenarios == []


def test_scenario_failure_is_machine_readable() -> None:
    forge = FakeForge()

    def empty_slots(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/slots":
            return httpx.Response(200, json={"seed": "obj-001", "slots": []})
        return forge.booking.handle(request)

    booking = ServiceClient(
        "booking",
        "http://booking.test",
        client=httpx.Client(
            transport=httpx.MockTransport(empty_slots),
            base_url="http://booking.test",
        ),
    )
    pvs = ServiceClient(
        "pvs",
        "http://pvs.test",
        client=httpx.Client(
            transport=httpx.MockTransport(forge.pvs.handle),
            base_url="http://pvs.test",
        ),
    )
    report = run_suite(
        "http://booking.test",
        "http://pvs.test",
        names=["combined-happy-path"],
        booking_client=booking,
        pvs_client=pvs,
    )
    assert report.status == "fail"
    payload = report.to_dict()
    assert payload["status"] == "fail"
    scenario = payload["scenarios"][0]
    assert scenario["name"] == "combined-happy-path"
    assert scenario["status"] == "fail"
    assert scenario["error"]
    failed_steps = [step for step in scenario["steps"] if step["status"] == "fail"]
    assert failed_steps
    assert failed_steps[-1]["name"] == "booking_has_available_slot"
    assert "http_status" in failed_steps[-1]
    assert "trace_id" in failed_steps[-1]


def test_cli_list_and_unreachable_services_exit_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["suite"] == "semantic"
    assert listed["scenarios"] == list_scenario_names()

    assert main(["--list", "--suite", "transport-chaos"]) == 0
    transport_listed = json.loads(capsys.readouterr().out)
    assert transport_listed["suite"] == "transport-chaos"
    assert transport_listed["scenarios"] == list_scenario_names("transport-chaos")

    code = main(
        [
            "--booking-url",
            "http://127.0.0.1:9",
            "--pvs-url",
            "http://127.0.0.1:9",
            "--timeout",
            "0.2",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == "fail"
    assert "booking health check failed" in payload["error"]


def test_cli_writes_json_report_on_success(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_run_suite(*_args, **_kwargs):
        return run_suite(
            "http://booking.test",
            "http://pvs.test",
            names=["combined-happy-path"],
            booking_client=booking_client,
            pvs_client=pvs_client,
        )

    monkeypatch.setattr("scenario_runner.cli.run_suite", fake_run_suite)
    assert main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["scenarios"][0]["name"] == "combined-happy-path"


def test_cli_nonzero_when_scenario_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "scenario_runner.cli.run_suite",
        lambda *_args, **_kwargs: SuiteReport(
            status="fail",
            booking_url="http://booking.test",
            pvs_url="http://pvs.test",
            error="forced failure",
        ),
    )
    assert main([]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "fail"
    assert payload["error"] == "forced failure"


def test_source_has_no_simulator_imports() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = ("fake_booking", "fake_pvs", "chaos_proxy", "praxisos", "PraxisOS", "docker.sock")
    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path} contains {token}"
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    for token in forbidden:
        assert token not in pyproject

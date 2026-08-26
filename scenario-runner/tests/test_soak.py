from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from scenario_runner.cli import main
from scenario_runner.http import ServiceClient
from scenario_runner.report import ScenarioResult, SuiteReport
from scenario_runner.runner import list_scenario_names, run_suite
from scenario_runner.soak import (
    DEFAULT_SOAK_ITERATIONS,
    MAX_SOAK_ITERATIONS,
    load_evidence,
    parse_replay_selector,
    reset_external_state,
    run_soak,
    validate_evidence_payload,
    write_evidence,
)
from tests.fake_services import FakeForge


def test_replay_selector_parsing() -> None:
    assert parse_replay_selector("2") == (None, 2)
    assert parse_replay_selector("semantic:3") == ("semantic", 3)
    assert parse_replay_selector("transport-chaos:1") == ("transport-chaos", 1)
    assert parse_replay_selector("all:2") == ("all", 2)
    with pytest.raises(ValueError, match="invalid replay selector"):
        parse_replay_selector("semantic")
    with pytest.raises(ValueError, match="replay index"):
        parse_replay_selector(f"semantic:{MAX_SOAK_ITERATIONS + 1}")


def test_semantic_soak_is_independent_and_deterministic(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
) -> None:
    report = run_soak(
        "http://booking.test",
        "http://pvs.test",
        iterations=2,
        suite="semantic",
        booking_client=booking_client,
        pvs_client=pvs_client,
    )
    assert report.status == "pass"
    assert report.mode == "soak"
    assert report.suites == ["semantic"]
    assert report.requested_iterations == 2
    assert report.completed_iterations == 2
    assert report.first_failure is None
    assert [item.replay_selector for item in report.iterations] == [
        "semantic:1",
        "semantic:2",
    ]
    assert [item.iteration for item in report.iterations] == [1, 2]
    assert all(item.status == "pass" for item in report.iterations)
    first_ids = None
    for item in report.iterations:
        assert [scenario.name for scenario in item.scenarios] == list_scenario_names()
        assert item.booking_seed == "obj-001"
        assert item.pvs_seed == "obj-002"
        happy = next(
            scenario for scenario in item.scenarios if scenario.name == "combined-happy-path"
        )
        if first_ids is None:
            first_ids = happy.ids
        else:
            assert happy.ids == first_ids


def test_soak_resets_external_state_each_iteration(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    original = reset_external_state

    def tracked(*args, include_chaos: bool = False, **kwargs):
        calls.append(include_chaos)
        return original(*args, include_chaos=include_chaos, **kwargs)

    monkeypatch.setattr("scenario_runner.soak.reset_external_state", tracked)
    report = run_soak(
        "http://booking.test",
        "http://pvs.test",
        iterations=3,
        suite="semantic",
        names=["combined-happy-path"],
        booking_client=booking_client,
        pvs_client=pvs_client,
    )
    assert report.status == "pass"
    assert report.completed_iterations == 3
    assert calls == [False, False, False]


def test_transport_soak_resets_proxies(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
    booking_chaos_client: ServiceClient,
    pvs_chaos_client: ServiceClient,
    booking_chaos_admin_client: ServiceClient,
    pvs_chaos_admin_client: ServiceClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    original = reset_external_state

    def tracked(*args, include_chaos: bool = False, **kwargs):
        calls.append(include_chaos)
        return original(*args, include_chaos=include_chaos, **kwargs)

    monkeypatch.setattr("scenario_runner.soak.reset_external_state", tracked)
    report = run_soak(
        "http://booking.test",
        "http://pvs.test",
        iterations=2,
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
    assert report.suites == ["transport-chaos"]
    assert [item.replay_selector for item in report.iterations] == [
        "transport-chaos:1",
        "transport-chaos:2",
    ]
    assert calls == [True, True]


def test_all_suites_run_per_iteration(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
    booking_chaos_client: ServiceClient,
    pvs_chaos_client: ServiceClient,
    booking_chaos_admin_client: ServiceClient,
    pvs_chaos_admin_client: ServiceClient,
) -> None:
    report = run_soak(
        "http://booking.test",
        "http://pvs.test",
        iterations=1,
        suite="all",
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
    assert report.suites == ["semantic", "transport-chaos"]
    assert report.completed_iterations == 1
    assert [item.suite for item in report.iterations] == ["semantic", "transport-chaos"]
    assert [item.replay_selector for item in report.iterations] == [
        "semantic:1",
        "transport-chaos:1",
    ]


def test_failing_iteration_is_identified_and_nonzero(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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

    def fake_run_soak(*_args, **_kwargs):
        return run_soak(
            "http://booking.test",
            "http://pvs.test",
            iterations=1,
            names=["combined-happy-path"],
            booking_client=booking,
            pvs_client=pvs,
        )

    monkeypatch.setattr("scenario_runner.cli.run_soak", fake_run_soak)
    evidence = tmp_path / "fail.json"
    code = main(["--soak", "--iterations", "1", "--evidence-file", str(evidence)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["status"] == "fail"
    assert payload["requested_iterations"] == 1
    assert payload["completed_iterations"] == 1
    assert payload["first_failure"]["iteration"] == 1
    assert payload["first_failure"]["suite"] == "semantic"
    assert payload["first_failure"]["scenario"] == "combined-happy-path"
    assert payload["first_failure"]["replay_selector"] == "semantic:1"
    assert payload["iterations"][0]["scenarios"][0]["name"] == "combined-happy-path"
    assert payload["iterations"][0]["scenarios"][0]["status"] == "fail"
    assert "steps" not in payload["iterations"][0]["scenarios"][0]
    loaded = load_evidence(evidence)
    assert loaded["first_failure"]["scenario"] == "combined-happy-path"
    assert loaded["iterations"][0]["scenarios"][0]["steps"]


def test_first_failure_keeps_first_iteration(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return SuiteReport(
                status="fail",
                booking_url="http://booking.test",
                pvs_url="http://pvs.test",
                suite="semantic",
                booking_seed="obj-001",
                pvs_seed="obj-002",
                scenarios=[
                    ScenarioResult(
                        name="combined-happy-path",
                        status="fail",
                        error="forced first-iteration failure",
                    )
                ],
                error=None,
            )
        return run_suite(*args, **kwargs)

    monkeypatch.setattr("scenario_runner.soak.run_suite", flaky)
    report = run_soak(
        "http://booking.test",
        "http://pvs.test",
        iterations=2,
        names=["combined-happy-path"],
        booking_client=booking_client,
        pvs_client=pvs_client,
    )
    assert report.status == "fail"
    assert report.completed_iterations == 2
    assert report.iterations[0].status == "fail"
    assert report.iterations[1].status == "pass"
    assert report.first_failure is not None
    assert report.first_failure.iteration == 1
    assert report.first_failure.scenario == "combined-happy-path"
    assert report.first_failure.replay_selector == "semantic:1"


def test_evidence_file_round_trip(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
    tmp_path: Path,
) -> None:
    report = run_soak(
        "http://booking.test",
        "http://pvs.test",
        iterations=2,
        names=["combined-happy-path"],
        booking_client=booking_client,
        pvs_client=pvs_client,
    )
    path = tmp_path / "nested" / "soak.json"
    write_evidence(path, report)
    loaded = load_evidence(path)
    assert loaded["schema"] == "praxis-forge.soak-evidence.v1"
    assert loaded["requested_iterations"] == 2
    assert loaded["completed_iterations"] == 2
    assert loaded["iterations"][1]["replay_selector"] == "semantic:2"
    assert loaded["iterations"][0]["scenarios"][0]["ids"]["booking_id"].startswith("bkg_")
    validate_evidence_payload(loaded)


def test_replay_uses_selector_without_hidden_state(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
) -> None:
    first = run_soak(
        "http://booking.test",
        "http://pvs.test",
        replay="semantic:2",
        names=["combined-happy-path"],
        booking_client=booking_client,
        pvs_client=pvs_client,
    )
    second = run_soak(
        "http://booking.test",
        "http://pvs.test",
        replay="semantic:2",
        names=["combined-happy-path"],
        booking_client=booking_client,
        pvs_client=pvs_client,
    )
    assert first.status == "pass"
    assert first.mode == "replay"
    assert first.requested_iterations == 1
    assert first.completed_iterations == 1
    assert first.replay_selector == "semantic:2"
    assert first.iterations[0].iteration == 2
    assert first.iterations[0].replay_selector == "semantic:2"
    assert second.iterations[0].scenarios[0].ids == first.iterations[0].scenarios[0].ids
    assert [step.name for step in second.iterations[0].scenarios[0].steps] == [
        step.name for step in first.iterations[0].scenarios[0].steps
    ]


def test_invalid_iteration_count_fails_without_http() -> None:
    report = run_soak(
        "http://booking.test",
        "http://pvs.test",
        iterations=0,
    )
    assert report.status == "fail"
    assert report.completed_iterations == 0
    assert report.error
    assert "iterations must be between" in report.error


def test_cli_soak_and_replay_stdout(
    booking_client: ServiceClient,
    pvs_client: ServiceClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def fake_run_soak(*_args, **kwargs):
        return run_soak(
            "http://booking.test",
            "http://pvs.test",
            iterations=kwargs.get("iterations", DEFAULT_SOAK_ITERATIONS),
            names=["combined-happy-path"],
            replay=kwargs.get("replay"),
            suite=kwargs.get("suite", "semantic"),
            booking_client=booking_client,
            pvs_client=pvs_client,
        )

    monkeypatch.setattr("scenario_runner.cli.run_soak", fake_run_soak)
    evidence = tmp_path / "soak.json"
    assert main(["--soak", "--iterations", "2", "--evidence-file", str(evidence)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "soak"
    assert payload["requested_iterations"] == 2
    assert payload["completed_iterations"] == 2
    assert payload["evidence_file"] == str(evidence)
    assert evidence.is_file()
    load_evidence(evidence)

    assert main(["--replay", "semantic:2", "--scenario", "combined-happy-path"]) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert replayed["mode"] == "replay"
    assert replayed["replay_selector"] == "semantic:2"
    assert replayed["iterations"][0]["iteration"] == 2


def test_cli_one_shot_remains_suite_report(
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
    assert "requested_iterations" not in payload
    assert "schema" not in payload

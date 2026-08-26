from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from scenario_runner.http import DEFAULT_TIMEOUT_SECONDS, ServiceClient
from scenario_runner.report import FirstFailure, IterationReport, SoakReport, SuiteReport
from scenario_runner.runner import list_scenario_names, run_suite

DEFAULT_SOAK_ITERATIONS = 3
MAX_SOAK_ITERATIONS = 20
REPLAY_SELECTOR = re.compile(
    r"^(?:(?P<suite>semantic|transport-chaos|all):)?(?P<index>[1-9]\d*)$"
)

_EVIDENCE_REQUIRED_KEYS = (
    "schema",
    "status",
    "mode",
    "suites",
    "requested_iterations",
    "completed_iterations",
    "replay_selector",
    "iterations",
    "first_failure",
)


def parse_replay_selector(value: str) -> tuple[str | None, int]:
    match = REPLAY_SELECTOR.fullmatch(value.strip())
    if match is None:
        raise ValueError(
            "invalid replay selector; expected SUITE:INDEX "
            "(semantic:2, transport-chaos:1, all:3) or a 1-based INDEX"
        )
    index = int(match.group("index"))
    if index > MAX_SOAK_ITERATIONS:
        raise ValueError(
            f"replay index must be between 1 and {MAX_SOAK_ITERATIONS}, got {index}"
        )
    return match.group("suite"), index


def expand_suites(suite: str) -> list[str]:
    if suite == "all":
        return ["semantic", "transport-chaos"]
    if suite in {"semantic", "transport-chaos"}:
        return [suite]
    raise ValueError(f"unknown suite: {suite}")


def replay_selector_for(suite: str, iteration: int) -> str:
    return f"{suite}:{iteration}"


def validate_iteration_count(iterations: int) -> None:
    if iterations < 1 or iterations > MAX_SOAK_ITERATIONS:
        raise ValueError(
            f"iterations must be between 1 and {MAX_SOAK_ITERATIONS}, got {iterations}"
        )


def validate_evidence_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("evidence must be a JSON object")
    missing = [key for key in _EVIDENCE_REQUIRED_KEYS if key not in payload]
    if missing:
        raise ValueError(f"evidence missing keys: {', '.join(missing)}")
    if payload.get("status") not in {"pass", "fail"}:
        raise ValueError("evidence status must be pass or fail")
    if payload.get("mode") not in {"soak", "replay"}:
        raise ValueError("evidence mode must be soak or replay")
    suites = payload.get("suites")
    if not isinstance(suites, list) or not all(isinstance(item, str) for item in suites):
        raise ValueError("evidence suites must be a list of strings")
    if not isinstance(payload.get("requested_iterations"), int):
        raise ValueError("evidence requested_iterations must be an int")
    if not isinstance(payload.get("completed_iterations"), int):
        raise ValueError("evidence completed_iterations must be an int")
    iterations = payload.get("iterations")
    if not isinstance(iterations, list):
        raise ValueError("evidence iterations must be a list")
    for index, item in enumerate(iterations):
        if not isinstance(item, dict):
            raise ValueError(f"evidence iterations[{index}] must be an object")
        for key in ("iteration", "suite", "replay_selector", "status", "scenarios"):
            if key not in item:
                raise ValueError(f"evidence iterations[{index}] missing {key}")
        if item.get("status") not in {"pass", "fail"}:
            raise ValueError(f"evidence iterations[{index}].status must be pass or fail")
        if not isinstance(item["scenarios"], list):
            raise ValueError(f"evidence iterations[{index}].scenarios must be a list")
        for scenario in item["scenarios"]:
            if not isinstance(scenario, dict):
                raise ValueError(f"evidence iterations[{index}] scenario must be an object")
            for key in ("name", "status", "ids", "steps"):
                if key not in scenario:
                    raise ValueError(
                        f"evidence iterations[{index}] scenario missing {key}"
                    )
    first_failure = payload.get("first_failure")
    if first_failure is not None:
        if not isinstance(first_failure, dict):
            raise ValueError("evidence first_failure must be an object or null")
        for key in ("iteration", "suite", "replay_selector", "scenario", "error"):
            if key not in first_failure:
                raise ValueError(f"evidence first_failure missing {key}")
    return payload


def write_evidence(path: Path, report: SoakReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(include_steps=True), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_evidence(path: Path) -> dict[str, Any]:
    return validate_evidence_payload(json.loads(path.read_text(encoding="utf-8")))


def reset_external_state(
    booking: ServiceClient,
    pvs: ServiceClient,
    *,
    booking_chaos_admin: ServiceClient | None = None,
    pvs_chaos_admin: ServiceClient | None = None,
    include_chaos: bool = False,
) -> str | None:
    targets: list[tuple[str, ServiceClient]] = [
        ("booking", booking),
        ("pvs", pvs),
    ]
    if include_chaos:
        if booking_chaos_admin is None or pvs_chaos_admin is None:
            return "chaos admin clients are required to reset transport-chaos state"
        targets.extend(
            (
                ("booking chaos", booking_chaos_admin),
                ("pvs chaos", pvs_chaos_admin),
            )
        )
    for label, client in targets:
        call = client.request("POST", "/v1/admin/reset")
        if call.error or call.status_code != 200:
            return f"{label} reset failed: {call.error or call.body!r}"
        if not isinstance(call.body, dict) or call.body.get("status") != "reset":
            return f"{label} reset failed: {call.body!r}"
    return None


def run_soak(
    booking_url: str,
    pvs_url: str,
    *,
    iterations: int = DEFAULT_SOAK_ITERATIONS,
    suite: str = "semantic",
    names: list[str] | None = None,
    replay: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    booking_client: ServiceClient | None = None,
    pvs_client: ServiceClient | None = None,
    booking_chaos_client: ServiceClient | None = None,
    pvs_chaos_client: ServiceClient | None = None,
    booking_chaos_admin_client: ServiceClient | None = None,
    pvs_chaos_admin_client: ServiceClient | None = None,
    booking_chaos_url: str | None = None,
    pvs_chaos_url: str | None = None,
    booking_chaos_admin_url: str | None = None,
    pvs_chaos_admin_url: str | None = None,
) -> SoakReport:
    mode = "replay" if replay else "soak"
    report = SoakReport(
        status="pass",
        mode=mode,
        suites=[],
        requested_iterations=1 if replay else iterations,
        booking_url=booking_url,
        pvs_url=pvs_url,
        booking_chaos_url=booking_chaos_url,
        pvs_chaos_url=pvs_chaos_url,
        replay_selector=replay,
    )
    try:
        if replay:
            replay_suite, replay_index = parse_replay_selector(replay)
            selected_suite = replay_suite or suite
            suite_names = _suite_runs(selected_suite, names)
            start = replay_index
            stop = replay_index
            report.suites = list(suite_names)
            report.replay_selector = (
                replay
                if replay_suite
                else replay_selector_for(
                    selected_suite if selected_suite != "selected" else suite_names[0],
                    replay_index,
                )
            )
        else:
            validate_iteration_count(iterations)
            suite_names = _suite_runs(suite, names)
            start = 1
            stop = iterations
            report.suites = list(suite_names)
    except ValueError as exc:
        report.status = "fail"
        report.error = str(exc)
        return report

    transport_needed = _needs_transport(suite_names, names)
    owned: list[ServiceClient] = []
    try:
        booking = booking_client or _owned(
            ServiceClient("booking", booking_url, timeout=timeout), owned
        )
        pvs = pvs_client or _owned(ServiceClient("pvs", pvs_url, timeout=timeout), owned)
        booking_chaos = booking_chaos_client
        pvs_chaos = pvs_chaos_client
        booking_admin = booking_chaos_admin_client
        pvs_admin = pvs_chaos_admin_client
        if transport_needed:
            missing: list[str] = []
            booking_chaos, missing = _optional_client(
                booking_chaos,
                owned,
                name="booking-chaos",
                url=booking_chaos_url,
                timeout=timeout,
                label="booking chaos URL",
                missing=missing,
            )
            pvs_chaos, missing = _optional_client(
                pvs_chaos,
                owned,
                name="pvs-chaos",
                url=pvs_chaos_url,
                timeout=timeout,
                label="pvs chaos URL",
                missing=missing,
            )
            booking_admin, missing = _optional_client(
                booking_admin,
                owned,
                name="booking-chaos-admin",
                url=booking_chaos_admin_url,
                timeout=timeout,
                label="booking chaos admin URL",
                missing=missing,
            )
            pvs_admin, missing = _optional_client(
                pvs_admin,
                owned,
                name="pvs-chaos-admin",
                url=pvs_chaos_admin_url,
                timeout=timeout,
                label="pvs chaos admin URL",
                missing=missing,
            )
            if missing:
                report.status = "fail"
                report.error = "transport-chaos scenarios require " + ", ".join(missing)
                return report

        for index in range(start, stop + 1):
            iteration_failed = False
            for suite_name in suite_names:
                include_chaos = _needs_transport([suite_name], names)
                reset_error = reset_external_state(
                    booking,
                    pvs,
                    booking_chaos_admin=booking_admin,
                    pvs_chaos_admin=pvs_admin,
                    include_chaos=include_chaos,
                )
                selector = replay_selector_for(suite_name, index)
                if reset_error:
                    item = IterationReport(
                        iteration=index,
                        suite=suite_name,
                        replay_selector=selector,
                        status="fail",
                        error=reset_error,
                    )
                    _record_iteration(report, item)
                    iteration_failed = True
                    continue
                suite_report = run_suite(
                    booking_url,
                    pvs_url,
                    names=names,
                    suite=suite_name if names is None else suite,
                    timeout=timeout,
                    booking_client=booking,
                    pvs_client=pvs,
                    booking_chaos_client=booking_chaos,
                    pvs_chaos_client=pvs_chaos,
                    booking_chaos_admin_client=booking_admin,
                    pvs_chaos_admin_client=pvs_admin,
                    booking_chaos_url=booking_chaos_url,
                    pvs_chaos_url=pvs_chaos_url,
                    booking_chaos_admin_url=booking_chaos_admin_url,
                    pvs_chaos_admin_url=pvs_chaos_admin_url,
                )
                item = _iteration_from_suite(index, suite_name, selector, suite_report)
                _record_iteration(report, item)
                if item.status == "fail":
                    iteration_failed = True
            report.completed_iterations += 1
            if iteration_failed:
                report.status = "fail"
        return report
    finally:
        for client in owned:
            client.close()


def compact_payload(report: SoakReport) -> dict[str, Any]:
    return report.to_dict(include_steps=False)


def _suite_runs(suite: str, names: list[str] | None) -> list[str]:
    if names is not None:
        return [suite]
    return expand_suites(suite)


def _needs_transport(suite_names: list[str], names: list[str] | None) -> bool:
    if any(item == "transport-chaos" for item in suite_names):
        return True
    if names:
        transport = set(list_scenario_names("transport-chaos"))
        return any(name in transport for name in names)
    return False


def _iteration_from_suite(
    index: int,
    suite_name: str,
    selector: str,
    suite_report: SuiteReport,
) -> IterationReport:
    return IterationReport(
        iteration=index,
        suite=suite_name,
        replay_selector=selector,
        status=suite_report.status,
        scenarios=list(suite_report.scenarios),
        booking_seed=suite_report.booking_seed,
        pvs_seed=suite_report.pvs_seed,
        error=suite_report.error,
    )


def _record_iteration(report: SoakReport, item: IterationReport) -> None:
    report.iterations.append(item)
    if item.booking_seed and report.booking_seed is None:
        report.booking_seed = item.booking_seed
    if item.pvs_seed and report.pvs_seed is None:
        report.pvs_seed = item.pvs_seed
    if item.status == "fail":
        report.status = "fail"
        if report.first_failure is None:
            failed = next(
                (scenario for scenario in item.scenarios if scenario.status == "fail"),
                None,
            )
            report.first_failure = FirstFailure(
                iteration=item.iteration,
                suite=item.suite,
                replay_selector=item.replay_selector,
                scenario=None if failed is None else failed.name,
                error=item.error if failed is None else failed.error or item.error,
            )


def _owned(client: ServiceClient, owned: list[ServiceClient]) -> ServiceClient:
    owned.append(client)
    return client


def _optional_client(
    existing: ServiceClient | None,
    owned: list[ServiceClient],
    *,
    name: str,
    url: str | None,
    timeout: float,
    label: str,
    missing: list[str],
) -> tuple[ServiceClient | None, list[str]]:
    if existing is not None:
        return existing, missing
    if not url:
        missing.append(label)
        return None, missing
    return _owned(ServiceClient(name, url, timeout=timeout), owned), missing

from __future__ import annotations

from scenario_runner.expect import CheckFailed, ScenarioContext
from scenario_runner.http import DEFAULT_TIMEOUT_SECONDS, ServiceClient
from scenario_runner.report import ScenarioResult, SuiteReport
from scenario_runner.scenarios import SCENARIOS


def list_scenario_names() -> list[str]:
    return list(SCENARIOS)


def run_suite(
    booking_url: str,
    pvs_url: str,
    *,
    names: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    booking_client: ServiceClient | None = None,
    pvs_client: ServiceClient | None = None,
) -> SuiteReport:
    selected = list_scenario_names() if names is None else names
    report = SuiteReport(
        status="pass",
        booking_url=booking_url,
        pvs_url=pvs_url,
    )
    unknown = [name for name in selected if name not in SCENARIOS]
    if unknown:
        report.status = "fail"
        report.error = f"unknown scenario(s): {', '.join(unknown)}"
        return report

    booking = booking_client or ServiceClient("booking", booking_url, timeout=timeout)
    pvs = pvs_client or ServiceClient("pvs", pvs_url, timeout=timeout)
    owns_clients = booking_client is None and pvs_client is None
    try:
        booking_health = booking.request("GET", "/healthz")
        pvs_health = pvs.request("GET", "/healthz")
        if booking_health.status_code == 200 and isinstance(booking_health.body, dict):
            seed = booking_health.body.get("seed")
            if isinstance(seed, str):
                report.booking_seed = seed
        if pvs_health.status_code == 200 and isinstance(pvs_health.body, dict):
            seed = pvs_health.body.get("seed")
            if isinstance(seed, str):
                report.pvs_seed = seed
        if booking_health.error or booking_health.status_code != 200:
            report.status = "fail"
            report.error = (
                "booking health check failed: "
                f"{booking_health.error or booking_health.body!r}"
            )
            return report
        if pvs_health.error or pvs_health.status_code != 200:
            report.status = "fail"
            report.error = f"pvs health check failed: {pvs_health.error or pvs_health.body!r}"
            return report

        for name in selected:
            report.scenarios.append(_run_one(name, booking, pvs))
        if any(scenario.status == "fail" for scenario in report.scenarios):
            report.status = "fail"
        return report
    finally:
        if owns_clients:
            booking.close()
            pvs.close()


def _run_one(name: str, booking: ServiceClient, pvs: ServiceClient) -> ScenarioResult:
    ctx = ScenarioContext()
    try:
        SCENARIOS[name](ctx, booking, pvs)
    except CheckFailed as exc:
        return ScenarioResult(
            name=name,
            status="fail",
            steps=ctx.steps,
            ids=ctx.ids,
            error=exc.step.detail or exc.step.name,
        )
    except Exception as exc:
        return ScenarioResult(
            name=name,
            status="fail",
            steps=ctx.steps,
            ids=ctx.ids,
            error=f"unhandled error: {exc}",
        )
    return ScenarioResult(name=name, status="pass", steps=ctx.steps, ids=ctx.ids)

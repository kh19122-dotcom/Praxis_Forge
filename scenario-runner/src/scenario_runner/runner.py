from __future__ import annotations

from collections.abc import Callable
from typing import Any

from scenario_runner.expect import CheckFailed, ScenarioContext
from scenario_runner.http import DEFAULT_TIMEOUT_SECONDS, ForgeSession, ServiceClient
from scenario_runner.report import ScenarioResult, SuiteReport
from scenario_runner.scenarios import SCENARIOS
from scenario_runner.transport import TRANSPORT_SCENARIOS

SUITES = {
    "semantic": list(SCENARIOS),
    "transport-chaos": list(TRANSPORT_SCENARIOS),
}


def list_scenario_names(suite: str = "semantic") -> list[str]:
    if suite == "all":
        return [*SCENARIOS, *TRANSPORT_SCENARIOS]
    if suite not in SUITES:
        raise ValueError(f"unknown suite: {suite}")
    return list(SUITES[suite])


def _catalog() -> dict[str, Callable[..., Any]]:
    return {**SCENARIOS, **TRANSPORT_SCENARIOS}


def run_suite(
    booking_url: str,
    pvs_url: str,
    *,
    names: list[str] | None = None,
    suite: str = "semantic",
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
) -> SuiteReport:
    catalog = _catalog()
    if names is None:
        try:
            selected = list_scenario_names(suite)
        except ValueError as exc:
            report = SuiteReport(
                status="fail",
                booking_url=booking_url,
                pvs_url=pvs_url,
                suite=suite,
            )
            report.error = str(exc)
            return report
        report_suite = suite
    else:
        selected = names
        report_suite = "selected"
    report = SuiteReport(
        status="pass",
        booking_url=booking_url,
        pvs_url=pvs_url,
        suite=report_suite,
        booking_chaos_url=booking_chaos_url,
        pvs_chaos_url=pvs_chaos_url,
    )
    unknown = [name for name in selected if name not in catalog]
    if unknown:
        report.status = "fail"
        report.error = f"unknown scenario(s): {', '.join(unknown)}"
        return report

    transport_needed = any(name in TRANSPORT_SCENARIOS for name in selected)
    owned: list[ServiceClient] = []
    booking = booking_client or _owned(
        ServiceClient("booking", booking_url, timeout=timeout), owned
    )
    pvs = pvs_client or _owned(ServiceClient("pvs", pvs_url, timeout=timeout), owned)
    booking_chaos = booking_chaos_client
    pvs_chaos = pvs_chaos_client
    booking_admin = booking_chaos_admin_client
    pvs_admin = pvs_chaos_admin_client

    try:
        if transport_needed:
            missing = []
            if booking_chaos is None:
                if not booking_chaos_url:
                    missing.append("booking chaos URL")
                else:
                    booking_chaos = _owned(
                        ServiceClient("booking-chaos", booking_chaos_url, timeout=timeout),
                        owned,
                    )
            if pvs_chaos is None:
                if not pvs_chaos_url:
                    missing.append("pvs chaos URL")
                else:
                    pvs_chaos = _owned(
                        ServiceClient("pvs-chaos", pvs_chaos_url, timeout=timeout),
                        owned,
                    )
            if booking_admin is None:
                if not booking_chaos_admin_url:
                    missing.append("booking chaos admin URL")
                else:
                    booking_admin = _owned(
                        ServiceClient(
                            "booking-chaos-admin", booking_chaos_admin_url, timeout=timeout
                        ),
                        owned,
                    )
            if pvs_admin is None:
                if not pvs_chaos_admin_url:
                    missing.append("pvs chaos admin URL")
                else:
                    pvs_admin = _owned(
                        ServiceClient(
                            "pvs-chaos-admin", pvs_chaos_admin_url, timeout=timeout
                        ),
                        owned,
                    )
            if missing:
                report.status = "fail"
                report.error = (
                    "transport-chaos scenarios require "
                    + ", ".join(missing)
                )
                return report

        session = ForgeSession(
            booking,
            pvs,
            booking_chaos=booking_chaos,
            pvs_chaos=pvs_chaos,
            booking_chaos_admin=booking_admin,
            pvs_chaos_admin=pvs_admin,
        )
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
        if transport_needed:
            for label, client in (
                ("booking chaos", booking_chaos),
                ("pvs chaos", pvs_chaos),
                ("booking chaos admin", booking_admin),
                ("pvs chaos admin", pvs_admin),
            ):
                assert client is not None
                health = client.request("GET", "/healthz")
                if health.error or health.status_code != 200:
                    report.status = "fail"
                    report.error = (
                        f"{label} health check failed: {health.error or health.body!r}"
                    )
                    return report

        for name in selected:
            report.scenarios.append(_run_one(name, session))
        if any(scenario.status == "fail" for scenario in report.scenarios):
            report.status = "fail"
        return report
    finally:
        for client in owned:
            client.close()


def _owned(client: ServiceClient, owned: list[ServiceClient]) -> ServiceClient:
    owned.append(client)
    return client


def _run_one(name: str, session: ForgeSession) -> ScenarioResult:
    ctx = ScenarioContext()
    try:
        if name in TRANSPORT_SCENARIOS:
            TRANSPORT_SCENARIOS[name](ctx, session)
        else:
            SCENARIOS[name](ctx, session.booking, session.pvs)
    except CheckFailed as failed:
        return ScenarioResult(
            name=name,
            status="fail",
            steps=ctx.steps,
            ids=ctx.ids,
            error=failed.step.detail or failed.step.name,
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

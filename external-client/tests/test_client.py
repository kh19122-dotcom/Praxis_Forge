from __future__ import annotations

import json

from external_client.cli import main
from external_client.client import SmokeFailure, check_dns, run_smoke
from tests.fakes import (
    FakeBookingHandler,
    FakeChaosAdminHandler,
    FakePvsHandler,
    FakeVendorState,
    serve,
)


def test_smoke_happy_path_idempotency_and_transport_drop() -> None:
    state = FakeVendorState()
    FakeBookingHandler.state = state
    FakePvsHandler.state = state
    FakeChaosAdminHandler.state = state
    FakeChaosAdminHandler.service_name = "chaos-booking"
    booking, booking_url = serve(FakeBookingHandler)
    pvs, pvs_url = serve(FakePvsHandler)
    admin, admin_url = serve(FakeChaosAdminHandler)
    pvs_admin_handler = type("PvsAdmin", (FakeChaosAdminHandler,), {"service_name": "chaos-pvs"})
    pvs_admin_handler.state = state
    pvs_admin, pvs_admin_url = serve(pvs_admin_handler)
    try:
        report = run_smoke(
            {
                "booking_url": booking_url,
                "pvs_url": pvs_url,
                "booking_chaos_admin_url": admin_url,
                "pvs_chaos_admin_url": pvs_admin_url,
                "lab_dns_names": (),
                "forbidden_dns_names": (),
                "skip_dns": True,
                "timeout": 2.0,
            }
        )
    finally:
        for server in (booking, pvs, admin, pvs_admin):
            server.shutdown()
            server.server_close()
    assert report["status"] == "pass"
    assert report["booking"]["idempotent_replay"] is True
    assert report["pvs"]["idempotent_replay"] is True
    assert report["chaos"]["observed"] is True
    names = [item["name"] for item in report["checks"]]
    assert "booking_idempotent_replay" in names
    assert "pvs_idempotent_replay" in names
    assert "transport_chaos_observed" in names


def test_cli_success_and_failure(
    capsys: object,
    monkeypatch: object,
) -> None:
    monkeypatch.setattr(
        "external_client.cli.run_smoke",
        lambda _config: {"schema": "praxis-forge.external-client-smoke.v1", "status": "pass"},
    )
    assert main(["--booking-url", "http://example.test"]) == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["status"] == "pass"

    def boom(_config):
        raise SmokeFailure("forced")

    monkeypatch.setattr("external_client.cli.run_smoke", boom)
    assert main([]) == 1
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    payload = json.loads(captured.out)
    assert payload["status"] == "fail"
    assert payload["error"] == "forced"


def test_dns_requires_aliases_and_rejects_simulators(monkeypatch: object) -> None:
    def fake_resolve(name: str) -> str:
        if name in {"chaos-booking", "chaos-pvs", "forge-booking", "forge-pvs"}:
            return "172.18.0.2"
        raise OSError("nxdomain")

    monkeypatch.setattr("external_client.client.resolve_name", fake_resolve)
    result = check_dns(
        ("chaos-booking", "chaos-pvs", "forge-booking", "forge-pvs"),
        ("fake-booking", "fake-pvs"),
    )
    assert result["resolved"]["forge-booking"] == "172.18.0.2"
    assert result["forbidden"]["fake-booking"] == "nxdomain"

    def leak(name: str) -> str:
        return "172.18.0.9"

    monkeypatch.setattr("external_client.client.resolve_name", leak)
    try:
        check_dns(("chaos-booking",), ("fake-booking",))
    except SmokeFailure as exc:
        assert "fake-booking" in str(exc)
    else:
        raise AssertionError("expected simulator DNS leak to fail")

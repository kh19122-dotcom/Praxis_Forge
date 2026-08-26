from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from external_client.client import (
    DEFAULT_BOOKING_CHAOS_ADMIN_URL,
    DEFAULT_BOOKING_URL,
    DEFAULT_PVS_CHAOS_ADMIN_URL,
    DEFAULT_PVS_URL,
    SCHEMA,
    SmokeFailure,
    run_smoke,
    settings_from_env,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="external-client",
        description=(
            "HTTP-only smoke client for the Praxis Forge cross-Compose lab network. "
            "Talks to chaos-proxied Booking/PVS vendor endpoints. Does not import "
            "simulator, chaos-proxy, scenario-runner, or private Praxis packages."
        ),
    )
    parser.add_argument(
        "--booking-url",
        default=os.environ.get("FORGE_BOOKING_URL", DEFAULT_BOOKING_URL),
        help=f"Chaos-proxied Booking data-plane URL (default: {DEFAULT_BOOKING_URL})",
    )
    parser.add_argument(
        "--pvs-url",
        default=os.environ.get("FORGE_PVS_URL", DEFAULT_PVS_URL),
        help=f"Chaos-proxied PVS data-plane URL (default: {DEFAULT_PVS_URL})",
    )
    parser.add_argument(
        "--booking-chaos-admin-url",
        default=os.environ.get(
            "FORGE_BOOKING_CHAOS_ADMIN_URL",
            DEFAULT_BOOKING_CHAOS_ADMIN_URL,
        ),
        help=f"Booking chaos admin URL (default: {DEFAULT_BOOKING_CHAOS_ADMIN_URL})",
    )
    parser.add_argument(
        "--pvs-chaos-admin-url",
        default=os.environ.get("FORGE_PVS_CHAOS_ADMIN_URL", DEFAULT_PVS_CHAOS_ADMIN_URL),
        help=f"PVS chaos admin URL (default: {DEFAULT_PVS_CHAOS_ADMIN_URL})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = settings_from_env()
    config["booking_url"] = args.booking_url.rstrip("/")
    config["pvs_url"] = args.pvs_url.rstrip("/")
    config["booking_chaos_admin_url"] = args.booking_chaos_admin_url.rstrip("/")
    config["pvs_chaos_admin_url"] = args.pvs_chaos_admin_url.rstrip("/")
    try:
        report = run_smoke(config)
    except SmokeFailure as exc:
        report = {"schema": SCHEMA, "status": "fail", "error": str(exc)}
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 1
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

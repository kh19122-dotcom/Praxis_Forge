from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from scenario_runner.http import DEFAULT_TIMEOUT_SECONDS
from scenario_runner.runner import list_scenario_names, run_suite

DEFAULT_BOOKING_URL = "http://127.0.0.1:8080"
DEFAULT_PVS_URL = "http://127.0.0.1:8081"
DEFAULT_BOOKING_CHAOS_URL = "http://127.0.0.1:8090"
DEFAULT_PVS_CHAOS_URL = "http://127.0.0.1:8091"
DEFAULT_BOOKING_CHAOS_ADMIN_URL = "http://127.0.0.1:8092"
DEFAULT_PVS_CHAOS_ADMIN_URL = "http://127.0.0.1:8093"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scenario-runner",
        description=(
            "Run deterministic HTTP-only scenarios against Fake Booking and Fake PVS."
        ),
    )
    parser.add_argument(
        "--booking-url",
        default=os.environ.get("FORGE_BOOKING_URL", DEFAULT_BOOKING_URL),
        help=f"Fake Booking base URL (default: {DEFAULT_BOOKING_URL})",
    )
    parser.add_argument(
        "--pvs-url",
        default=os.environ.get("FORGE_PVS_URL", DEFAULT_PVS_URL),
        help=f"Fake PVS base URL (default: {DEFAULT_PVS_URL})",
    )
    parser.add_argument(
        "--booking-chaos-url",
        default=os.environ.get("FORGE_BOOKING_CHAOS_URL", DEFAULT_BOOKING_CHAOS_URL),
        help=(
            "Booking chaos-proxy data-plane URL "
            f"(default: {DEFAULT_BOOKING_CHAOS_URL})"
        ),
    )
    parser.add_argument(
        "--pvs-chaos-url",
        default=os.environ.get("FORGE_PVS_CHAOS_URL", DEFAULT_PVS_CHAOS_URL),
        help=f"PVS chaos-proxy data-plane URL (default: {DEFAULT_PVS_CHAOS_URL})",
    )
    parser.add_argument(
        "--booking-chaos-admin-url",
        default=os.environ.get(
            "FORGE_BOOKING_CHAOS_ADMIN_URL", DEFAULT_BOOKING_CHAOS_ADMIN_URL
        ),
        help=(
            "Booking chaos-proxy admin URL "
            f"(default: {DEFAULT_BOOKING_CHAOS_ADMIN_URL})"
        ),
    )
    parser.add_argument(
        "--pvs-chaos-admin-url",
        default=os.environ.get(
            "FORGE_PVS_CHAOS_ADMIN_URL", DEFAULT_PVS_CHAOS_ADMIN_URL
        ),
        help=f"PVS chaos-proxy admin URL (default: {DEFAULT_PVS_CHAOS_ADMIN_URL})",
    )
    parser.add_argument(
        "--suite",
        choices=("semantic", "transport-chaos", "all"),
        default="semantic",
        help="Named suite to run when --scenario is omitted (default: semantic).",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        metavar="NAME",
        help=(
            "Scenario name to run; repeatable. "
            "Default: all named scenarios in --suite."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the named scenario list as JSON and exit.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        json.dump(
            {"suite": args.suite, "scenarios": list_scenario_names(args.suite)},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0
    report = run_suite(
        args.booking_url,
        args.pvs_url,
        names=args.scenarios,
        suite=args.suite,
        timeout=args.timeout,
        booking_chaos_url=args.booking_chaos_url,
        pvs_chaos_url=args.pvs_chaos_url,
        booking_chaos_admin_url=args.booking_chaos_admin_url,
        pvs_chaos_admin_url=args.pvs_chaos_admin_url,
    )
    json.dump(report.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

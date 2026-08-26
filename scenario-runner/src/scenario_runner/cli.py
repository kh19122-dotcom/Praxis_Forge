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
        "--scenario",
        action="append",
        dest="scenarios",
        metavar="NAME",
        help="Scenario name to run; repeatable. Default: all named scenarios.",
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
        json.dump({"scenarios": list_scenario_names()}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    report = run_suite(
        args.booking_url,
        args.pvs_url,
        names=args.scenarios,
        timeout=args.timeout,
    )
    json.dump(report.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

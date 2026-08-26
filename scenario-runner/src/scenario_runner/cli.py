from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from scenario_runner.http import DEFAULT_TIMEOUT_SECONDS
from scenario_runner.runner import list_scenario_names, run_suite
from scenario_runner.soak import (
    DEFAULT_SOAK_ITERATIONS,
    MAX_SOAK_ITERATIONS,
    compact_payload,
    run_soak,
    validate_evidence_payload,
    write_evidence,
)

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
    parser.add_argument(
        "--soak",
        action="store_true",
        help=(
            "Run a bounded deterministic soak "
            f"(default: {DEFAULT_SOAK_ITERATIONS} iterations, "
            f"max {MAX_SOAK_ITERATIONS}). "
            "Each iteration resets simulator/proxy state over HTTP."
        ),
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Soak iteration count. Implies --soak. "
            f"Default with --soak: {DEFAULT_SOAK_ITERATIONS}. "
            f"Max: {MAX_SOAK_ITERATIONS}."
        ),
    )
    parser.add_argument(
        "--evidence-file",
        default=os.environ.get("FORGE_EVIDENCE_FILE"),
        metavar="PATH",
        help=(
            "Write the soak/replay JSON evidence report to PATH. "
            "Stdout stays a concise machine-readable soak summary."
        ),
    )
    parser.add_argument(
        "--replay",
        default=None,
        metavar="SELECTOR",
        help=(
            "Replay one soak iteration/suite without hidden process state. "
            "SELECTOR is SUITE:INDEX (semantic:2) or a 1-based INDEX with --suite."
        ),
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

    soak_requested = bool(args.soak or args.replay or args.iterations is not None)
    if not soak_requested:
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

    iterations = (
        1
        if args.replay
        else DEFAULT_SOAK_ITERATIONS
        if args.iterations is None
        else args.iterations
    )
    report = run_soak(
        args.booking_url,
        args.pvs_url,
        iterations=iterations,
        names=args.scenarios,
        suite=args.suite,
        replay=args.replay,
        timeout=args.timeout,
        booking_chaos_url=args.booking_chaos_url,
        pvs_chaos_url=args.pvs_chaos_url,
        booking_chaos_admin_url=args.booking_chaos_admin_url,
        pvs_chaos_admin_url=args.pvs_chaos_admin_url,
    )
    evidence_path = Path(args.evidence_file) if args.evidence_file else None
    if evidence_path is not None:
        try:
            report.evidence_file = str(evidence_path)
            write_evidence(evidence_path, report)
            validate_evidence_payload(json.loads(evidence_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            report.status = "fail"
            report.error = f"failed to write evidence file: {exc}"
    json.dump(compact_payload(report), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

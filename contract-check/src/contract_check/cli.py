from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from contract_check.check import check_contracts
from contract_check.fingerprint import DEFAULT_FINGERPRINT_PATH
from contract_check.http import DEFAULT_TIMEOUT_SECONDS

DEFAULT_BOOKING_URL = "http://127.0.0.1:8080"
DEFAULT_PVS_URL = "http://127.0.0.1:8081"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contract-check",
        description=(
            "Validate Fake Booking and Fake PVS OpenAPI contracts over HTTP. "
            "Compares live /openapi.json and /openapi.yaml against a committed "
            "normalized fingerprint."
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
        "--fingerprint-file",
        default=os.environ.get("FORGE_CONTRACT_FINGERPRINT"),
        metavar="PATH",
        help="Committed fingerprint JSON (default: packaged fingerprints.json)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--update-fingerprint",
        action="store_true",
        help=(
            "Rewrite the committed fingerprint from the live normalized contracts. "
            "Use after an intentional contract-surface change, then review the diff."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    fingerprint_path = (
        Path(args.fingerprint_file)
        if args.fingerprint_file
        else DEFAULT_FINGERPRINT_PATH
    )
    report = check_contracts(
        args.booking_url,
        args.pvs_url,
        fingerprint_path=fingerprint_path,
        timeout=args.timeout,
        update_fingerprint=args.update_fingerprint,
    )
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

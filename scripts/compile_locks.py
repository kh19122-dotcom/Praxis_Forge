#!/usr/bin/env python3
"""Compile per-component pip constraint files with uv."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_pins import (
    BUILD_CONSTRAINTS,
    COMPONENTS,
    DEV_LOCK,
    PYTHON_VERSION,
    RUNTIME_LOCK,
)

COMPILE_COMMAND = "python3 scripts/compile_locks.py"


def compile_command(
    pyproject: Path,
    output: Path,
    *,
    extra: str | None = None,
    constraint: Path | None = None,
) -> list[str]:
    command = [
        "uv",
        "pip",
        "compile",
        "--python-version",
        PYTHON_VERSION,
        "--universal",
        "--generate-hashes",
        "--no-annotate",
        "--custom-compile-command",
        COMPILE_COMMAND,
        "--build-constraints",
        str(ROOT / BUILD_CONSTRAINTS),
        str(pyproject),
        "-o",
        str(output),
    ]
    if extra is not None:
        command.extend(["--extra", extra])
    if constraint is not None:
        command.extend(["-c", str(constraint)])
    return command


def compile_lock(
    pyproject: Path,
    output: Path,
    *,
    extra: str | None = None,
    constraint: Path | None = None,
) -> None:
    subprocess.run(
        compile_command(pyproject, output, extra=extra, constraint=constraint),
        check=True,
        cwd=ROOT,
    )


def compile_component(name: str) -> None:
    component = ROOT / name
    runtime = component / RUNTIME_LOCK
    dev = component / DEV_LOCK
    pyproject = component / "pyproject.toml"
    compile_lock(pyproject, runtime, extra=None, constraint=None)
    compile_lock(pyproject, dev, extra="dev", constraint=runtime)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "component",
        nargs="*",
        choices=COMPONENTS,
        help="Compile only these components (default: all).",
    )
    args = parser.parse_args()
    selected = args.component or list(COMPONENTS)
    for name in selected:
        compile_component(name)
        print(f"compiled {name}/{RUNTIME_LOCK} and {name}/{DEV_LOCK}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

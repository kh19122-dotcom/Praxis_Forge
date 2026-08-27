#!/usr/bin/env python3
"""Compare image pip freeze output against committed component locks."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_pins import COMPONENTS, DEV_LOCK, RUNTIME_LOCK

LOCK_RE = re.compile(
    r"^([A-Za-z0-9._-]+)==([^\\\s]+)"
    r"(?:\s*;\s*([^\\\n]+))?",
    re.MULTILINE,
)
FREEZE_RE = re.compile(r"^([A-Za-z0-9._-]+)==(.+)$", re.MULTILINE)
ALLOW_EXTRA = {"pip", "setuptools", "wheel"}


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    required: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for name, version, marker in LOCK_RE.findall(path.read_text(encoding="utf-8")):
        key = normalize(name)
        marker = marker.strip()
        if "sys_platform == 'win32'" in marker and "sys_platform !=" not in marker:
            skipped[key] = version
            continue
        required[key] = version
    return required, skipped


def parse_freeze(text: str) -> dict[str, str]:
    installed: dict[str, str] = {}
    for name, version in FREEZE_RE.findall(text):
        installed[normalize(name)] = version
    return installed


def docker_build(component: str, target: str, tag: str, platform: str | None) -> None:
    command = ["docker", "build"]
    if platform:
        command.extend(["--platform", platform])
    command.extend(
        [
            "--target",
            target,
            "-t",
            tag,
            str(ROOT / component),
        ]
    )
    subprocess.run(command, check=True, cwd=ROOT)


def docker_freeze(tag: str, platform: str | None = None) -> str:
    command = [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "python",
    ]
    if platform:
        command.extend(["--platform", platform])
    command.extend(
        [
            tag,
            "-m",
            "pip",
            "freeze",
            "--all",
        ]
    )
    completed = subprocess.run(
        command,
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def compare(component: str, target: str, freeze: str) -> list[str]:
    lock_name = RUNTIME_LOCK if target == "runtime" else DEV_LOCK
    required, skipped = parse_lock(ROOT / component / lock_name)
    installed = parse_freeze(freeze)
    errors: list[str] = []
    local = normalize(component)
    for name, version in required.items():
        found = installed.get(name)
        if found is None:
            errors.append(f"{component} {target}: missing {name}=={version}")
        elif found != version:
            errors.append(f"{component} {target}: {name}=={found} != locked {version}")
    extras = sorted(
        name
        for name in installed
        if name not in required
        and name not in skipped
        and name not in ALLOW_EXTRA
        and name != local
    )
    for name in extras:
        errors.append(f"{component} {target}: unexpected extra package {name}=={installed[name]}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "component",
        nargs="*",
        choices=COMPONENTS,
        help="Verify only these components (default: all).",
    )
    parser.add_argument(
        "--target",
        choices=("runtime", "test", "both"),
        default="both",
    )
    parser.add_argument("--platform", default="")
    parser.add_argument("--image", default="")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    selected = args.component or list(COMPONENTS)
    targets = ("runtime", "test") if args.target == "both" else (args.target,)
    if args.image and (len(selected) != 1 or len(targets) != 1):
        parser.error("--image requires exactly one component and one --target")
    platform = args.platform or None
    errors: list[str] = []
    for name in selected:
        for target in targets:
            tag = args.image or f"praxis-forge/{name}:{target}-probe"
            if not args.skip_build:
                docker_build(name, target, tag, platform)
            freeze = docker_freeze(tag, platform)
            print(f"## {name} {target} ({tag})")
            print(freeze.rstrip())
            errors.extend(compare(name, target, freeze))
    if errors:
        print("installed-version verification failed:", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        return 1
    print("installed-version verification ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

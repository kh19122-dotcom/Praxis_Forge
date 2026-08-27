#!/usr/bin/env python3
"""Validate committed Python/container build inputs for Praxis Forge."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_pins import (
    BUILD_CONSTRAINTS,
    CLI_COMPONENTS,
    COMPONENTS,
    DEV_LOCK,
    PYTHON_BASE_DIGEST,
    PYTHON_BASE_IMAGE,
    PYTHON_BASE_TAG,
    RUNTIME_LOCK,
    SETUPTOOLS_REQUIREMENT,
    TEST_SERVICES,
)
from compile_locks import compile_command

FROM_RE = re.compile(
    rf"^FROM {re.escape(PYTHON_BASE_IMAGE)} AS runtime\s*$",
    re.MULTILINE,
)
SETUPTOOLS_RE = re.compile(
    r'^requires = \["setuptools==84\.0\.0"\]\s*$',
    re.MULTILINE,
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CI_DEV_LOCK_INSTALL = "--require-hashes -r constraints-dev.txt"
CI_EDITABLE_NO_DEPS = "--no-deps -e ."
CI_PIP_PIN = "pip==25.0.1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _error(errors: list[str], message: str) -> None:
    errors.append(message)


def _check_files(errors: list[str]) -> None:
    build_constraints = ROOT / BUILD_CONSTRAINTS
    if not build_constraints.is_file():
        _error(errors, f"missing {BUILD_CONSTRAINTS}")
    elif SETUPTOOLS_REQUIREMENT not in _read(build_constraints):
        _error(errors, f"{BUILD_CONSTRAINTS} must pin {SETUPTOOLS_REQUIREMENT}")
    if not DIGEST_RE.fullmatch(PYTHON_BASE_DIGEST):
        _error(errors, f"invalid python base digest {PYTHON_BASE_DIGEST}")
    if PYTHON_BASE_TAG != "python:3.12-slim":
        _error(errors, f"unexpected python base tag {PYTHON_BASE_TAG}")
    for name in COMPONENTS:
        component = ROOT / name
        for filename in ("pyproject.toml", "Dockerfile", RUNTIME_LOCK, DEV_LOCK):
            path = component / filename
            if not path.is_file():
                _error(errors, f"missing {path.relative_to(ROOT)}")


def _check_pyprojects(errors: list[str]) -> None:
    for name in COMPONENTS:
        text = _read(ROOT / name / "pyproject.toml")
        if not SETUPTOOLS_RE.search(text):
            _error(errors, f"{name}/pyproject.toml must pin {SETUPTOOLS_REQUIREMENT}")
        if "setuptools>=" in text:
            _error(errors, f"{name}/pyproject.toml still uses an open-ended setuptools bound")


def _check_dockerfiles(errors: list[str]) -> None:
    for name in COMPONENTS:
        rel = f"{name}/Dockerfile"
        text = _read(ROOT / name / "Dockerfile")
        if not FROM_RE.search(text):
            _error(errors, f"{rel} must start from {PYTHON_BASE_IMAGE}")
        if re.search(r"FROM python:3\.12-slim(?!@)", text):
            _error(errors, f"{rel} still references an unpinned python:3.12-slim tag")
        if "pip install --upgrade pip" in text:
            _error(errors, f"{rel} must not upgrade pip")
        if f"COPY pyproject.toml {RUNTIME_LOCK} ./" not in text:
            _error(errors, f"{rel} runtime stage must COPY {RUNTIME_LOCK}")
        if f"COPY {DEV_LOCK} ./" not in text:
            _error(errors, f"{rel} test stage must COPY {DEV_LOCK}")
        if f"--require-hashes -r {RUNTIME_LOCK}" not in text:
            _error(errors, f"{rel} runtime install must consume hashed {RUNTIME_LOCK}")
        if f"--require-hashes -r {DEV_LOCK}" not in text:
            _error(errors, f"{rel} test install must consume hashed {DEV_LOCK}")
        if "FROM runtime AS test" not in text:
            _error(errors, f"{rel} must define a test stage")
            continue
        runtime_stage, test_stage = text.split("FROM runtime AS test", 1)
        if "ENTRYPOINT []" not in test_stage:
            _error(errors, f"{rel} test stage must clear ENTRYPOINT")
        if 'CMD ["pytest", "-q"]' not in test_stage:
            _error(errors, f"{rel} test stage must run pytest")
        if name in CLI_COMPONENTS:
            if f'ENTRYPOINT ["{name}"]' not in runtime_stage:
                _error(errors, f"{rel} runtime ENTRYPOINT for {name} is missing")
            if "ENTRYPOINT []" in runtime_stage:
                _error(errors, f"{rel} must not clear the runtime ENTRYPOINT")
        if name == "contract-check" and "COPY Dockerfile ./Dockerfile" not in test_stage:
            _error(errors, f"{rel} test stage must COPY Dockerfile for compose-binding tests")
        if name == "external-client":
            if "COPY Dockerfile ./Dockerfile" not in test_stage:
                _error(errors, f"{rel} test stage must COPY Dockerfile for isolation tests")
            if "COPY docker-compose.yml ./docker-compose.yml" not in test_stage:
                _error(errors, f"{rel} test stage must COPY component compose file")


def _check_compose(errors: list[str]) -> None:
    text = _read(ROOT / "docker-compose.yml")
    for service in TEST_SERVICES:
        if f"{service}:" not in text:
            _error(errors, f"docker-compose.yml missing {service}")
    if text.count("./docker-compose.yml:/docker-compose.yml:ro") < 6:
        _error(errors, "every test service must mount root docker-compose.yml read-only")
    if "./docker-compose.lab.yml:/docker-compose.lab.yml:ro" not in text:
        _error(errors, "external-client-tests must mount docker-compose.lab.yml read-only")
    if "./external-client/docker-compose.yml:/external-client/docker-compose.yml:ro" not in text:
        _error(errors, "external-client-tests must mount external-client/docker-compose.yml")


def _check_ci(errors: list[str]) -> None:
    text = _read(ROOT / ".github/workflows/ci.yml")
    if CI_DEV_LOCK_INSTALL not in text:
        _error(errors, "CI host package jobs must install from hashed constraints-dev.txt")
    if CI_EDITABLE_NO_DEPS not in text:
        _error(errors, "CI host package jobs must install the local package with --no-deps")
    if CI_PIP_PIN not in text:
        _error(errors, "CI host jobs must pin pip==25.0.1")
    if 'pip install -e ".[dev]"' in text or "pip install -e '.[dev]'" in text:
        _error(errors, "CI host package jobs still install unpinned extras")
    for name in COMPONENTS:
        if f"working-directory: {name}" not in text:
            _error(errors, f"CI missing host package job for {name}")
    compose_run = "docker compose --profile test run --rm --build"
    matrix_run = compose_run + " ${{ matrix.service }}"
    if matrix_run not in text:
        for service in TEST_SERVICES:
            needle = f"{compose_run} {service}"
            if needle not in text:
                _error(errors, f"CI must run {needle}")
    else:
        for service in TEST_SERVICES:
            if f"- {service}" not in text and service not in text:
                _error(errors, f"CI container-test matrix missing {service}")
    if "linux/amd64" not in text or "linux/arm64" not in text:
        _error(errors, "CI must smoke-build linux/amd64 and linux/arm64")
    if "scripts/validate_locks.py" not in text:
        _error(errors, "CI must run scripts/validate_locks.py")
    if "scripts/verify_installed_versions.py" not in text:
        _error(errors, "CI must run scripts/verify_installed_versions.py")
    if "pip install --upgrade pip" in text:
        _error(errors, "CI must not run an unbounded pip upgrade")


def _check_lock_freshness(errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="praxis-forge-lock-") as raw:
        tmp = Path(raw)
        for name in COMPONENTS:
            runtime = tmp / f"{name}-{RUNTIME_LOCK}"
            dev = tmp / f"{name}-{DEV_LOCK}"
            for extra, output, constraint in (
                (None, runtime, None),
                ("dev", dev, ROOT / name / RUNTIME_LOCK),
            ):
                command = compile_command(
                    ROOT / name / "pyproject.toml",
                    output,
                    extra=extra,
                    constraint=constraint,
                )
                env = os.environ.copy()
                env.setdefault("UV_NO_PROGRESS", "1")
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if completed.returncode != 0:
                    _error(
                        errors,
                        f"uv pip compile failed for {name} extra={extra!r}: "
                        f"{completed.stderr.strip() or completed.stdout.strip()}",
                    )
                    continue
                current_name = DEV_LOCK if extra else RUNTIME_LOCK
                if output.read_text(encoding="utf-8") != _read(ROOT / name / current_name):
                    _error(
                        errors,
                        f"{name}/{current_name} is stale; run `python3 scripts/compile_locks.py`",
                    )


def _check_base_manifest(errors: list[str]) -> None:
    url = f"https://registry-1.docker.io/v2/library/python/manifests/{PYTHON_BASE_DIGEST}"
    token_url = (
        "https://auth.docker.io/token?service=registry.docker.io"
        "&scope=repository:library/python:pull"
    )
    try:
        with urllib.request.urlopen(token_url, timeout=30) as response:
            token = json.loads(response.read().decode("utf-8"))["token"]
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": (
                    "application/vnd.oci.image.index.v1+json, "
                    "application/vnd.docker.distribution.manifest.list.v2+json"
                ),
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            digest = response.headers.get("Docker-Content-Digest", "")
    except Exception as exc:  # noqa: BLE001
        _error(errors, f"failed to inspect pinned python base manifest: {exc}")
        return
    if digest and digest != PYTHON_BASE_DIGEST:
        _error(errors, f"registry digest {digest} != pinned {PYTHON_BASE_DIGEST}")
    platforms = {
        (item.get("platform", {}).get("os"), item.get("platform", {}).get("architecture"))
        for item in payload.get("manifests", [])
    }
    if ("linux", "amd64") not in platforms or ("linux", "arm64") not in platforms:
        _error(
            errors,
            f"{PYTHON_BASE_IMAGE} is not a multi-arch manifest covering "
            f"linux/amd64 and linux/arm64: {sorted(platforms)}",
        )


def main() -> int:
    errors: list[str] = []
    _check_files(errors)
    _check_pyprojects(errors)
    _check_dockerfiles(errors)
    _check_compose(errors)
    _check_ci(errors)
    _check_base_manifest(errors)
    _check_lock_freshness(errors)
    if errors:
        print("lock validation failed:", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        return 1
    print("lock validation ok")
    print(f"python_base_image={PYTHON_BASE_IMAGE}")
    print(f"setuptools={SETUPTOOLS_REQUIREMENT}")
    print(f"components={','.join(COMPONENTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

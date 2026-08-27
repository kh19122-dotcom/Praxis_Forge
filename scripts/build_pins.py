"""Reviewed executable-environment pins for Praxis Forge images and locks."""

from __future__ import annotations

PYTHON_BASE_TAG = "python:3.12-slim"
PYTHON_BASE_DIGEST = "sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17"
PYTHON_BASE_IMAGE = f"{PYTHON_BASE_TAG}@{PYTHON_BASE_DIGEST}"
PYTHON_VERSION = "3.12"
SETUPTOOLS_VERSION = "84.0.0"
SETUPTOOLS_REQUIREMENT = f"setuptools=={SETUPTOOLS_VERSION}"
# pip 25.0.1 is shipped in PYTHON_BASE_IMAGE. Dockerfiles must not upgrade pip.
PIP_VERSION = "25.0.1"
UV_VERSION = "0.11.28"

COMPONENTS = (
    "fake-booking",
    "fake-pvs",
    "chaos-proxy",
    "scenario-runner",
    "contract-check",
    "external-client",
)

CLI_COMPONENTS = (
    "scenario-runner",
    "contract-check",
    "external-client",
)

TEST_SERVICES = tuple(f"{name}-tests" for name in COMPONENTS)

RUNTIME_LOCK = "constraints.txt"
DEV_LOCK = "constraints-dev.txt"
BUILD_CONSTRAINTS = "build-constraints.txt"

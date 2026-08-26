#!/usr/bin/env python3
"""Host-side cross-Compose lab gate.

Starts Praxis Forge under one Compose project with the lab overlay, then
runs the HTTP-only external-client under a second Compose project joined
only through the named praxis-forge-lab bridge. Host/CI talks to the
Docker CLI; application/test containers do not mount the Docker socket.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yml"
LAB_FILE = ROOT / "docker-compose.lab.yml"
CLIENT_COMPOSE = ROOT / "external-client" / "docker-compose.yml"
NETWORK = "praxis-forge-lab"
VENDOR_PROJECT = os.environ.get("FORGE_LAB_VENDOR_PROJECT", "praxis-forge-lab-vendor")
CLIENT_PROJECT = os.environ.get("FORGE_LAB_CLIENT_PROJECT", "praxis-forge-lab-client")
VENDOR_SERVICES = ("fake-booking", "fake-pvs", "chaos-booking", "chaos-pvs")
LAB_SERVICES = ("chaos-booking", "chaos-pvs")
OFF_LAB_SERVICES = ("fake-booking", "fake-pvs")
HOST_PORT_PUBLISH = re.compile(r"(?:\d+\.\d+\.\d+\.\d+:)?\d+:\d+\Z")
ALLOWED_PUBLISH = {
    "127.0.0.1:8080:8080",
    "127.0.0.1:8081:8081",
    "127.0.0.1:8090:8090",
    "127.0.0.1:8091:8091",
    "127.0.0.1:8092:8092",
    "127.0.0.1:8093:8093",
}


class CheckFailure(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd or ROOT),
        check=False,
        text=True,
        capture_output=capture,
    )
    if result.returncode != 0:
        if capture:
            sys.stderr.write(result.stdout or "")
            sys.stderr.write(result.stderr or "")
        raise CheckFailure(f"command failed ({result.returncode}): {' '.join(command)}")
    return result


def vendor_compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker",
            "compose",
            "-p",
            VENDOR_PROJECT,
            "-f",
            str(COMPOSE_FILE),
            "-f",
            str(LAB_FILE),
            *args,
        ],
        capture=capture,
    )


def client_compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return run(
        ["docker", "compose", "-p", CLIENT_PROJECT, "-f", str(CLIENT_COMPOSE), *args],
        capture=capture,
    )


def check(label: str) -> None:
    print(f"PASS {label}", flush=True)


def parse_ps(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise CheckFailure(f"unexpected compose ps json: {payload!r}")
        return payload
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def docker_json(command: list[str]) -> Any:
    result = run(command, capture=True)
    return json.loads(result.stdout)


def assert_no_docker_socket(text: str, label: str) -> None:
    if "docker.sock" in text or "/var/run/docker.sock" in text:
        raise CheckFailure(f"{label} mentions docker.sock")


def assert_loopback_publishes(text: str, label: str) -> None:
    if "0.0.0.0:" in text:
        raise CheckFailure(f"{label} binds 0.0.0.0 on the host")
    for raw in text.splitlines():
        stripped = raw.split("#", 1)[0].strip().lstrip("-").strip().strip("\"'")
        if HOST_PORT_PUBLISH.fullmatch(stripped) and stripped not in ALLOWED_PUBLISH:
            raise CheckFailure(f"{label} unexpected publish {stripped}")


def network_container_names() -> list[str]:
    payload = docker_json(["docker", "network", "inspect", NETWORK])
    if not isinstance(payload, list) or not payload:
        raise CheckFailure(f"network inspect missing {NETWORK}")
    net = payload[0]
    if net.get("Name") != NETWORK:
        raise CheckFailure(f"expected network name {NETWORK}, got {net.get('Name')!r}")
    if net.get("Driver") != "bridge":
        raise CheckFailure(f"expected bridge driver, got {net.get('Driver')!r}")
    containers = net.get("Containers") or {}
    names = []
    for item in containers.values():
        if isinstance(item, dict) and item.get("Name"):
            names.append(str(item["Name"]))
    return names


def service_container(project: str, service: str) -> str:
    if project == VENDOR_PROJECT:
        result = vendor_compose("ps", "-a", "--format", "json", capture=True)
    else:
        result = client_compose("ps", "-a", "--format", "json", capture=True)
    rows = parse_ps(result.stdout)
    for row in rows:
        if row.get("Service") == service:
            name = row.get("Name")
            if isinstance(name, str) and name:
                return name
    raise CheckFailure(f"service {service} not found in project {project}")


def inspect_networks(container: str) -> dict[str, Any]:
    payload = docker_json(["docker", "inspect", container])
    if not isinstance(payload, list) or not payload:
        raise CheckFailure(f"inspect missing {container}")
    networks = payload[0].get("NetworkSettings", {}).get("Networks") or {}
    if not isinstance(networks, dict):
        raise CheckFailure(f"{container} networks missing")
    return networks


def inspect_mounts(container: str) -> list[dict[str, Any]]:
    payload = docker_json(["docker", "inspect", container])
    mounts = payload[0].get("Mounts") or []
    if not isinstance(mounts, list):
        raise CheckFailure(f"{container} mounts missing")
    return mounts


def assert_no_socket_mount(container: str) -> None:
    for mount in inspect_mounts(container):
        source = str(mount.get("Source") or "")
        dest = str(mount.get("Destination") or "")
        if "docker.sock" in source or "docker.sock" in dest:
            raise CheckFailure(f"{container} mounts docker.sock ({source} -> {dest})")


def assert_published_loopback(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        publishers = row.get("Publishers") or []
        if not isinstance(publishers, list):
            continue
        for item in publishers:
            if not isinstance(item, dict):
                continue
            published = item.get("PublishedPort")
            if not published:
                continue
            url = item.get("URL") or item.get("url")
            if url not in {"127.0.0.1", "::1"}:
                raise CheckFailure(
                    f"{row.get('Service')} published {published} on {url!r}, expected 127.0.0.1"
                )


def extract_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise CheckFailure(f"external-client produced no JSON: {text!r}")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise CheckFailure(f"external-client JSON is not an object: {payload!r}")
    return payload


def verify_compose_files() -> None:
    default_text = COMPOSE_FILE.read_text(encoding="utf-8")
    lab_text = LAB_FILE.read_text(encoding="utf-8")
    client_text = CLIENT_COMPOSE.read_text(encoding="utf-8")
    assert_no_docker_socket(default_text, "docker-compose.yml")
    assert_no_docker_socket(lab_text, "docker-compose.lab.yml")
    assert_no_docker_socket(client_text, "external-client/docker-compose.yml")
    if "praxis-forge-lab" in default_text:
        raise CheckFailure("default docker-compose.yml must not attach the lab network")
    if "127.0.0.1:8080:8080" not in default_text or "127.0.0.1:8090:8090" not in default_text:
        raise CheckFailure("default loopback publishes missing")
    assert_loopback_publishes(default_text, "docker-compose.yml")
    if "0.0.0.0:" in lab_text or "0.0.0.0:" in client_text:
        raise CheckFailure("lab/client compose binds 0.0.0.0")
    if "name: praxis-forge-lab" not in lab_text or "driver: bridge" not in lab_text:
        raise CheckFailure("lab overlay missing named bridge network")
    lab_services = lab_text.split("services:", 1)[-1]
    if "fake-booking:" in lab_services or "fake-pvs:" in lab_services:
        raise CheckFailure("lab overlay attaches Fake Booking/Fake PVS")
    if "external: true" not in client_text:
        raise CheckFailure("external-client must join praxis-forge-lab as external")
    check("compose_files_loopback_and_no_docker_socket")


def verify_vendor_attachments() -> None:
    names = network_container_names()
    joined = " ".join(names)
    for service in LAB_SERVICES:
        if service not in joined:
            raise CheckFailure(f"{service} is not attached to {NETWORK}: {names}")
    for service in OFF_LAB_SERVICES:
        if service in joined:
            raise CheckFailure(f"{service} unexpectedly attached to {NETWORK}: {names}")
    for service in VENDOR_SERVICES:
        container = service_container(VENDOR_PROJECT, service)
        assert_no_socket_mount(container)
        networks = inspect_networks(container)
        on_lab = NETWORK in networks
        if service in LAB_SERVICES and not on_lab:
            raise CheckFailure(f"{container} missing {NETWORK}")
        if service in OFF_LAB_SERVICES and on_lab:
            raise CheckFailure(f"{container} attached to {NETWORK}")
        if service in LAB_SERVICES:
            aliases = networks[NETWORK].get("Aliases") or []
            expected = {service, "forge-booking" if service == "chaos-booking" else "forge-pvs"}
            missing = expected.difference(set(aliases))
            if missing:
                raise CheckFailure(f"{container} missing DNS aliases {sorted(missing)}: {aliases}")
    rows = parse_ps(vendor_compose("ps", "--format", "json", capture=True).stdout)
    assert_published_loopback(rows)
    check("lab_network_bridge_and_attachments")
    check("default_host_publishes_loopback")
    check("simulators_off_lab_network")
    check("no_docker_socket_mounts")


def run_external_client() -> dict[str, Any]:
    if VENDOR_PROJECT == CLIENT_PROJECT:
        raise CheckFailure("vendor and client Compose project names must differ")
    result = client_compose("run", "--rm", "--build", "external-client", capture=True)
    sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    report = extract_json(result.stdout)
    if report.get("status") != "pass":
        raise CheckFailure(f"external-client failed: {report!r}")
    if report.get("schema") != "praxis-forge.external-client-smoke.v1":
        raise CheckFailure(f"unexpected client schema: {report.get('schema')!r}")
    names = [item.get("name") for item in report.get("checks") or [] if isinstance(item, dict)]
    required = {
        "lab_dns_aliases",
        "simulator_dns_absent",
        "booking_health",
        "pvs_health",
        "booking_write",
        "booking_idempotent_replay",
        "pvs_write",
        "pvs_idempotent_replay",
        "transport_chaos_observed",
    }
    missing = sorted(required.difference(names))
    if missing:
        raise CheckFailure(f"external-client missing checks {missing}")
    if not report.get("booking", {}).get("idempotent_replay"):
        raise CheckFailure("booking idempotent replay not reported")
    if not report.get("pvs", {}).get("idempotent_replay"):
        raise CheckFailure("pvs idempotent replay not reported")
    if not report.get("chaos", {}).get("observed"):
        raise CheckFailure("transport chaos was not observed")
    dns = report.get("dns") or {}
    resolved = dns.get("resolved") or {}
    for name in ("chaos-booking", "chaos-pvs", "forge-booking", "forge-pvs"):
        if name not in resolved:
            raise CheckFailure(f"client did not resolve {name}: {dns!r}")
    check("cross_project_dns_and_http")
    check("happy_path_and_idempotency")
    check("transport_chaos_visible_to_external_client")
    return report


def remove_lab_network() -> None:
    inspect = subprocess.run(
        ["docker", "network", "inspect", NETWORK],
        check=False,
        text=True,
        capture_output=True,
    )
    if inspect.returncode != 0:
        return
    subprocess.run(
        ["docker", "network", "rm", NETWORK],
        check=False,
        text=True,
        capture_output=True,
    )


def teardown() -> None:
    try:
        client_compose("down", "--remove-orphans", "--volumes")
    except CheckFailure as exc:
        print(f"client teardown: {exc}", file=sys.stderr)
    try:
        vendor_compose("down", "--remove-orphans", "--volumes")
    except CheckFailure as exc:
        print(f"vendor teardown: {exc}", file=sys.stderr)
    remove_lab_network()


def main() -> int:
    if VENDOR_PROJECT == CLIENT_PROJECT:
        print("cross-compose-lab: fail: project names must be distinct", file=sys.stderr)
        return 1
    print(f"vendor_project={VENDOR_PROJECT}", flush=True)
    print(f"client_project={CLIENT_PROJECT}", flush=True)
    print(f"lab_network={NETWORK}", flush=True)
    try:
        teardown()
        verify_compose_files()
        vendor_compose(
            "up",
            "-d",
            "--build",
            "--wait",
            *VENDOR_SERVICES,
        )
        verify_vendor_attachments()
        run_external_client()
        print("cross-compose-lab: pass")
        return 0
    except CheckFailure as exc:
        print(f"cross-compose-lab: fail: {exc}", file=sys.stderr)
        return 1
    finally:
        teardown()


if __name__ == "__main__":
    raise SystemExit(main())

# Praxis Forge

Praxis Forge is a public, standalone synthetic healthcare vendor simulator for integration, failure, and chaos testing.

This repository is intentionally independent from PraxisOS. It must not import private PraxisOS code, real patient data, private infrastructure details, or production credentials.

Development is orchestrated through scoped objectives, feature branches, pull requests, CI, and review before merge.

## Fake Booking

The first vertical slice is a deterministic HTTP booking-provider simulator.

- Synthetic calendar origin: `2030-01-06T00:00:00Z`
- Default seed: `obj-001` (`FORGE_SEED`)
- Patient references must match `synth-[a-z0-9-]+`
- Slot and booking identifiers are derived from the seed; they repeat for a fixed seed
- Failure injection and event traces are admin/test controls, not a product dashboard

Live contract:

- OpenAPI JSON: `http://127.0.0.1:8080/openapi.json`
- OpenAPI YAML: `http://127.0.0.1:8080/openapi.yaml`
- Swagger UI: `http://127.0.0.1:8080/docs`
- Source YAML: `fake-booking/src/fake_booking/openapi.yaml`

### Run with Docker Compose

From the repository root:

```bash
docker compose up --build fake-booking
```

The default Compose publish is loopback-only (`127.0.0.1:8080:8080`). The API is available at `http://127.0.0.1:8080` and is not published on other host interfaces. Check liveness with:

```bash
curl -s http://127.0.0.1:8080/healthz
```

List seeded slots:

```bash
curl -s http://127.0.0.1:8080/v1/slots
```

Create a booking (requires `Idempotency-Key`):

```bash
curl -s -X POST http://127.0.0.1:8080/v1/bookings \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-key-0001' \
  -d '{"slot_id":"<slot-id>","patient_ref":"synth-ada"}'
```

Inspect the remote event trace:

```bash
curl -s http://127.0.0.1:8080/v1/admin/events
```

Reset catalog, bookings, faults, and events:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/admin/reset
```

### Deterministic failure injection

`PUT /v1/admin/faults` accepts:

| `mode` | Effect |
|---|---|
| `none` | No fault |
| `fail_before_commit` | `503 fail_before_commit`; remote commit is not attempted |
| `delay` | Response is delayed by `delay_ms`, then the booking commits normally |
| `ambiguous` | Booking commits, then the HTTP response is `504 ambiguous_outcome` (`committed: null`). Read `/v1/admin/events` or `/v1/bookings/{id}` for the actual remote state |

`remaining` is the number of matching create-booking calls that consume the fault. Optional `idempotency_key` scopes the fault to one client key.

### Tests

Docker (from the repository root):

```bash
docker compose --profile test run --rm fake-booking-tests
```

Local (Python 3.12+):

```bash
cd fake-booking
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

The test suite covers happy path, slot conflict, idempotent retry, fail-before-commit, delayed response, and ambiguous remote-effect recovery from Forge evidence.

## Fake PVS

The second vertical slice is a deterministic HTTP PVS-like simulator. It is not a real PVS vendor emulator and does not claim production compatibility.

- Synthetic calendar origin: `2030-01-06T00:00:00Z`
- Default seed: `obj-002` (`FORGE_SEED`)
- Patient identifiers, search keys, and task titles must match `synth-[a-z0-9-]+`
- Seeded patients and encounters are derived from the seed; they repeat for a fixed seed
- The representative remote write is synthetic staff-task creation
- Failure injection and event traces are admin/test controls, not a product dashboard

Live contract:

- OpenAPI JSON: `http://127.0.0.1:8081/openapi.json`
- OpenAPI YAML: `http://127.0.0.1:8081/openapi.yaml`
- Swagger UI: `http://127.0.0.1:8081/docs`
- Source YAML: `fake-pvs/src/fake_pvs/openapi.yaml`

### Run with Docker Compose

From the repository root:

```bash
docker compose up --build fake-pvs
```

Run both simulators:

```bash
docker compose up --build fake-booking fake-pvs
```

The default Compose publish is loopback-only (`127.0.0.1:8081:8081`). The API is available at `http://127.0.0.1:8081` and is not published on other host interfaces. Check liveness with:

```bash
curl -s http://127.0.0.1:8081/healthz
```

List seeded patients (optional `cohort`, `site`, or `id` filters):

```bash
curl -s http://127.0.0.1:8081/v1/patients
curl -s 'http://127.0.0.1:8081/v1/patients?cohort=cohort-alpha'
```

Read one patient and that patient's seeded encounters:

```bash
curl -s http://127.0.0.1:8081/v1/patients/synth-ada
curl -s http://127.0.0.1:8081/v1/patients/synth-ada/encounters
```

Create a staff task (requires `Idempotency-Key`):

```bash
curl -s -X POST http://127.0.0.1:8081/v1/tasks \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-task-0001' \
  -d '{"patient_id":"synth-ada","title":"synth-chart-review","priority":"normal"}'
```

Inspect the remote event trace:

```bash
curl -s http://127.0.0.1:8081/v1/admin/events
```

Reset corpus, tasks, faults, and events:

```bash
curl -s -X POST http://127.0.0.1:8081/v1/admin/reset
```

### Deterministic failure injection

`PUT /v1/admin/faults` accepts:

| `mode` | Effect |
|---|---|
| `none` | No fault |
| `fail_before_commit` | `503 fail_before_commit`; remote commit is not attempted |
| `delay` | Response is delayed by `delay_ms`, then the task commits normally |
| `ambiguous` | Task commits, then the HTTP response is `504 ambiguous_outcome` (`committed: null`). Read `/v1/admin/events` or `/v1/tasks/{id}` for the actual remote state |

`remaining` is the number of matching create-task calls that consume the fault. Optional `idempotency_key` scopes the fault to one client key.

### Tests

Docker (from the repository root):

```bash
docker compose --profile test run --rm fake-pvs-tests
```

Local (Python 3.12+):

```bash
cd fake-pvs
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

The test suite covers patient/encounter reads, synthetic-identifier enforcement, idempotent task creation, missing-patient/validation/conflict/infrastructure failures, delayed response, and ambiguous remote-effect recovery from Forge evidence.

## Scenario runner

The third vertical slice is a standalone HTTP-only CLI that treats Fake Booking and Fake PVS as external vendors. It does not import either simulator package.

Fixed seeds expected by the named suite:

- Fake Booking: `obj-001`
- Fake PVS: `obj-002`

Named scenarios:

| Name | What it proves |
|---|---|
| `combined-happy-path` | Successful booking and PVS task flow against both services |
| `booking-fail-before-commit` | Booking `503` leaves no remote booking; same key succeeds on retry |
| `booking-ambiguous-recovery` | Booking `504` is client-uncertain; evidence APIs show the commit |
| `pvs-fail-before-commit` | PVS task `503` leaves no remote task; same key succeeds on retry |
| `pvs-ambiguous-recovery` | PVS task `504` is client-uncertain; evidence APIs show the commit |
| `conflict-idempotency` | Slot conflict plus idempotent replay/conflict on both services |

Each scenario starts with `POST /v1/admin/reset` on both services and injects faults only through `PUT /v1/admin/faults`. The CLI prints one JSON object to stdout. Process exit code `0` means the suite passed; any scenario or health-check failure exits `1`.

### Run the suite against Compose services

macOS and Linux, from a fresh checkout of the repository root:

```bash
docker compose --profile scenario run --rm --build scenario-runner
```

That command starts both simulators, waits for their health checks, and runs every named scenario over the Compose network (`http://fake-booking:8080` and `http://fake-pvs:8081`). Admin/reset/fault ports remain loopback-only on the host.

List scenario names:

```bash
docker compose --profile scenario run --rm --build scenario-runner --list
```

Run one named scenario:

```bash
docker compose --profile scenario run --rm --build scenario-runner --scenario combined-happy-path
```

### Run from the host against published loopback ports

Start the simulators, then install and run the CLI with Python 3.12+:

```bash
docker compose up --build -d fake-booking fake-pvs

cd scenario-runner
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
scenario-runner --list
scenario-runner
scenario-runner --scenario combined-happy-path
```

The defaults are `http://127.0.0.1:8080` and `http://127.0.0.1:8081`. Override with `--booking-url`, `--pvs-url`, or `FORGE_BOOKING_URL` / `FORGE_PVS_URL`.

### Tests

Docker (from the repository root):

```bash
docker compose --profile test run --rm scenario-runner-tests
```

Local (Python 3.12+):

```bash
cd scenario-runner
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

The unit suite uses in-process HTTP fakes. CI also runs the named suite against real Compose containers.

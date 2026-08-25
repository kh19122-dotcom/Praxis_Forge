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

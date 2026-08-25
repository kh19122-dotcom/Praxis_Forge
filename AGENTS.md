# Praxis Forge Engineering Contract

## Project boundary

Praxis Forge is an external-system simulator. Treat it as a vendor/test environment, not as part of PraxisOS authority or production topology.

Hard rules:

- Synthetic data only. Never add real patient data, production exports, or identifying clinical records.
- No private PraxisOS source, ADR text, credentials, internal hostnames, Tailscale details, or private infrastructure configuration.
- No dependency on PraxisOS packages. Integration must happen through explicit external contracts such as HTTP/OpenAPI.
- Do not claim production safety, regulatory compliance, or production PVS/Samedi compatibility unless separately evidenced.
- Failure and chaos behavior must be deterministic and testable when a seed/configuration is fixed.

## Git workflow

- `main` is the integration branch.
- Implementation work happens on scoped feature branches.
- Do not force-push, amend reviewed history, or rewrite shared history unless explicitly instructed.
- Open a PR for implementation work. Keep the PR limited to one objective.
- Report exact base SHA and head SHA in handoff/review notes.
- Merge only after required tests/CI and independent review are complete.

## Worker role

Workers implement, test, commit, push, and open/update the PR. Workers do not self-authorize merge.

## Orchestrator role

The orchestrator owns objective definition, scope control, branch/PR integration, review gates, and merge decisions.

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fake_pvs.ids import encounter_id
from fake_pvs.settings import Settings

# Explicit synthetic identifiers only. Labels are simulator tokens, not personal names.
PATIENT_SPECS: tuple[tuple[str, str, str], ...] = (
    ("ada", "cohort-alpha", "site-north"),
    ("ben", "cohort-alpha", "site-north"),
    ("cal", "cohort-beta", "site-north"),
    ("deb", "cohort-beta", "site-south"),
    ("eli", "cohort-gamma", "site-south"),
    ("fay", "cohort-gamma", "site-east"),
)

ENCOUNTER_KINDS = ("intake", "follow_up", "review")


def patient_id_for(label: str) -> str:
    return f"synth-{label}"


def generate_patients(_settings: Settings) -> dict[str, dict]:
    patients: dict[str, dict] = {}
    for label, cohort, site in PATIENT_SPECS:
        pid = patient_id_for(label)
        patients[pid] = {
            "id": pid,
            "cohort": cohort,
            "site": site,
            "status": "active",
        }
    return patients


def generate_encounters(settings: Settings) -> dict[str, dict]:
    origin = date.fromisoformat(settings.origin_date)
    encounters: dict[str, dict] = {}
    for patient_index, (label, _cohort, _site) in enumerate(PATIENT_SPECS):
        pid = patient_id_for(label)
        day = origin + timedelta(days=patient_index)
        for encounter_index in range(settings.encounters_per_patient):
            occurred = datetime(
                day.year,
                day.month,
                day.day,
                9 + encounter_index,
                0,
                0,
                tzinfo=UTC,
            )
            occurred_iso = _iso(occurred)
            eid = encounter_id(settings.seed, pid, occurred_iso, str(encounter_index))
            encounters[eid] = {
                "id": eid,
                "patient_id": pid,
                "occurred_at": occurred_iso,
                "kind": ENCOUNTER_KINDS[encounter_index % len(ENCOUNTER_KINDS)],
                "summary": f"synth-encounter-{encounter_index + 1}",
                "status": "completed",
            }
    return encounters


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

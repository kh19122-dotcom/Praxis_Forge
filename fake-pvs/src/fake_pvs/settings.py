from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_SEED = "obj-002"
DEFAULT_ENCOUNTERS_PER_PATIENT = 3
# Synthetic calendar origin: a Monday far from any live clinic schedule.
DEFAULT_ORIGIN_DATE = "2030-01-06"


@dataclass(frozen=True)
class Settings:
    seed: str = DEFAULT_SEED
    encounters_per_patient: int = DEFAULT_ENCOUNTERS_PER_PATIENT
    origin_date: str = DEFAULT_ORIGIN_DATE

    @classmethod
    def from_env(cls, seed: str | None = None) -> Settings:
        return cls(seed=seed or os.environ.get("FORGE_SEED", DEFAULT_SEED))

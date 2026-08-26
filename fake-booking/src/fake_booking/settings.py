from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_SEED = "obj-001"
DEFAULT_RESOURCES = ("res-alpha", "res-beta")
DEFAULT_SLOT_HOURS = (9, 10, 11, 14, 15)
DEFAULT_SLOT_DAYS = 5
# Synthetic calendar origin: a Monday far from any live clinic schedule.
DEFAULT_ORIGIN_DATE = "2030-01-06"


@dataclass(frozen=True)
class Settings:
    seed: str = DEFAULT_SEED
    resources: tuple[str, ...] = DEFAULT_RESOURCES
    slot_hours: tuple[int, ...] = DEFAULT_SLOT_HOURS
    slot_days: int = DEFAULT_SLOT_DAYS
    origin_date: str = DEFAULT_ORIGIN_DATE
    state_path: str | None = None

    @classmethod
    def from_env(cls, seed: str | None = None) -> Settings:
        raw_path = os.environ.get("FORGE_STATE_PATH", "").strip()
        return cls(
            seed=seed or os.environ.get("FORGE_SEED", DEFAULT_SEED),
            state_path=raw_path or None,
        )

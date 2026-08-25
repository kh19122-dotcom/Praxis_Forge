from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fake_booking.ids import slot_id
from fake_booking.settings import Settings


def generate_slots(settings: Settings) -> dict[str, dict]:
    origin = date.fromisoformat(settings.origin_date)
    slots: dict[str, dict] = {}
    for day_offset in range(settings.slot_days):
        day = origin + timedelta(days=day_offset)
        for resource_id in settings.resources:
            for hour in settings.slot_hours:
                start = datetime(day.year, day.month, day.day, hour, 0, 0, tzinfo=UTC)
                end = start + timedelta(hours=1)
                start_iso = _iso(start)
                sid = slot_id(settings.seed, resource_id, start_iso)
                slots[sid] = {
                    "id": sid,
                    "resource_id": resource_id,
                    "start": start_iso,
                    "end": _iso(end),
                    "booking_id": None,
                }
    return slots


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

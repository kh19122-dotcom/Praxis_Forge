from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any

MODES = frozenset(
    {
        "none",
        "delay",
        "drop_before_upstream",
        "drop_after_upstream",
    }
)


class EpochStale(Exception):
    """Raised when a request tries to mutate a newer reset epoch."""


@dataclass(frozen=True)
class Fault:
    mode: str = "none"
    remaining: int = 0
    delay_ms: int = 50
    method: str | None = None
    path: str | None = None
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FaultController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._fault = Fault()
        self._events: list[dict[str, Any]] = []
        self._seq = 0
        self._epoch = 0
        self._resetting = False
        self._in_flight: dict[int, int] = {}

    def in_flight_total(self) -> int:
        with self._lock:
            return sum(self._in_flight.values())

    def is_resetting(self) -> bool:
        with self._lock:
            return self._resetting

    def begin(self) -> int:
        with self._cond:
            while self._resetting:
                self._cond.wait()
            epoch = self._epoch
            self._in_flight[epoch] = self._in_flight.get(epoch, 0) + 1
            return epoch

    def finish(self, epoch: int) -> None:
        with self._cond:
            remaining = self._in_flight.get(epoch, 0)
            if remaining <= 1:
                self._in_flight.pop(epoch, None)
            else:
                self._in_flight[epoch] = remaining - 1
            self._cond.notify_all()

    def snapshot(self) -> Fault:
        with self._lock:
            return self._fault

    def events(self, *, event_type: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events)
        if event_type is None:
            return events
        return [event for event in events if event.get("type") == event_type]

    def reset(self) -> Fault:
        with self._cond:
            self._resetting = True
            stale = self._epoch
            self._epoch += 1
            while self._in_flight.get(stale, 0) > 0:
                self._cond.wait()
            self._fault = Fault()
            self._events = []
            self._seq = 0
            self._resetting = False
            self._cond.notify_all()
            return self._fault

    def configure(self, payload: dict[str, Any], *, epoch: int | None = None) -> Fault:
        mode = payload.get("mode", "none")
        if mode not in MODES:
            raise ValueError(f"unsupported mode: {mode!r}")
        remaining_raw = payload.get("remaining", 0 if mode == "none" else 1)
        delay_raw = payload.get("delay_ms", 50)
        remaining = int(remaining_raw)
        delay_ms = int(delay_raw)
        if remaining < 0:
            raise ValueError("remaining must be >= 0")
        if delay_ms < 0:
            raise ValueError("delay_ms must be >= 0")
        if mode == "none":
            remaining = 0
        method = _optional_str(payload.get("method"))
        path = _optional_str(payload.get("path"))
        idempotency_key = _optional_str(payload.get("idempotency_key"))
        if method is not None:
            method = method.upper()
        fault = Fault(
            mode=mode,
            remaining=remaining,
            delay_ms=delay_ms,
            method=method,
            path=path,
            idempotency_key=idempotency_key,
        )
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                raise EpochStale()
            self._fault = fault
            self._record_locked(
                "fault_configured",
                mode=fault.mode,
                remaining=fault.remaining,
                delay_ms=fault.delay_ms,
                method=fault.method,
                path=fault.path,
                idempotency_key=fault.idempotency_key,
            )
            return fault

    def consume(
        self,
        method: str,
        path: str,
        idempotency_key: str | None,
        *,
        epoch: int | None = None,
    ) -> Fault | None:
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                return None
            current = self._fault
            if current.mode == "none" or current.remaining <= 0:
                return None
            if current.method and current.method != method.upper():
                return None
            if current.path and current.path != path:
                return None
            if current.idempotency_key and current.idempotency_key != idempotency_key:
                return None
            remaining = current.remaining - 1
            if remaining <= 0:
                self._fault = Fault(delay_ms=current.delay_ms)
            else:
                self._fault = Fault(
                    mode=current.mode,
                    remaining=remaining,
                    delay_ms=current.delay_ms,
                    method=current.method,
                    path=current.path,
                    idempotency_key=current.idempotency_key,
                )
            self._record_locked(
                "fault_consumed",
                mode=current.mode,
                remaining=remaining,
                method=method.upper(),
                path=path,
                idempotency_key=idempotency_key,
            )
            return current

    def record(self, event_type: str, *, epoch: int | None = None, **details: object) -> None:
        with self._lock:
            if epoch is not None and epoch != self._epoch:
                return
            self._record_locked(event_type, **details)

    def _record_locked(self, event_type: str, **details: object) -> None:
        self._seq += 1
        self._events.append(
            {
                "seq": self._seq,
                "type": event_type,
                "details": details,
            }
        )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected string")
    stripped = value.strip()
    return stripped or None

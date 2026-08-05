from __future__ import annotations

import heapq
import itertools
import json
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from auto_parser.sources.base import SourceError
from auto_parser.sqlite_support import configure_sqlite_connection


RATE_LIMIT_BASE_MINUTES = 15
RATE_LIMIT_MAX_MINUTES = 240
HOURLY_REQUEST_BUDGET = 120
_STATE_KEY = "avito_request_governor_v1"
_PRIORITIES = {
    "interactive_detail": 0,
    "interactive_image": 1,
    "search": 2,
    "refresh_incomplete_detail": 3,
    "incomplete_detail": 3,
    "detail": 3,
    "refresh_incomplete_image": 4,
    "incomplete_image": 4,
    "validation": 5,
    "refresh_stale_detail": 6,
    "stale_detail": 6,
    "refresh_stale_image": 7,
    "stale_image": 7,
    "image": 7,
}
_DELAYS = {
    "interactive_detail": (3.0, 6.0),
    "interactive_image": (3.0, 6.0),
    "search": (8.0, 16.0),
    "refresh_incomplete_detail": (6.0, 12.0),
    "incomplete_detail": (6.0, 12.0),
    "detail": (6.0, 12.0),
    "refresh_incomplete_image": (4.0, 8.0),
    "incomplete_image": (4.0, 8.0),
    "validation": (8.0, 15.0),
    "refresh_stale_detail": (8.0, 15.0),
    "stale_detail": (8.0, 15.0),
    "refresh_stale_image": (4.0, 8.0),
    "stale_image": (4.0, 8.0),
    "image": (4.0, 8.0),
}
_LANES = {
    "search": "search",
    "interactive_detail": "background",
    "interactive_image": "background",
    "refresh_incomplete_detail": "background",
    "refresh_incomplete_image": "background",
    "incomplete_detail": "background",
    "incomplete_image": "background",
    "detail": "background",
    "validation": "background",
    "refresh_stale_detail": "background",
    "refresh_stale_image": "background",
    "stale_detail": "background",
    "stale_image": "background",
    "image": "background",
}
_FORCED_KINDS = {
    "interactive_detail",
    "interactive_image",
    "refresh_incomplete_detail",
    "refresh_incomplete_image",
    "refresh_stale_detail",
    "refresh_stale_image",
}


def _lane(kind: str) -> str:
    return _LANES.get(kind, "background")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


class RequestDeferredError(SourceError):
    def __init__(self, retry_at: datetime, kind: str = "search") -> None:
        self.retry_at = retry_at
        self.kind = kind
        self.lane = _lane(kind)
        seconds = max(1, round((retry_at - _utc_now()).total_seconds()))
        super().__init__(
            "Запросы к Avito временно приостановлены; "
            f"повтор возможен через {seconds} с"
        )


@dataclass
class _SharedGovernorState:
    condition: threading.Condition = field(
        default_factory=lambda: threading.Condition(threading.Lock())
    )
    queue: list[tuple[int, int, object]] = field(default_factory=list)
    active: bool = False
    next_allowed_at: float = 0.0
    cooldowns: dict[str, datetime] = field(default_factory=dict)
    strikes: dict[str, int] = field(default_factory=dict)
    successes: dict[str, int] = field(default_factory=dict)
    request_times: dict[str, list[float]] = field(default_factory=dict)


_SHARED_STATES: dict[str, _SharedGovernorState] = {}
_SHARED_STATES_LOCK = threading.Lock()
_SEQUENCE = itertools.count()


class RequestGovernor:
    def __init__(
        self,
        database: str | Path | None = None,
        *,
        namespace: str = "avito",
    ) -> None:
        self.database = Path(database).resolve() if database else None
        self.namespace = namespace.strip().lower() or "avito"
        self._state_key = (
            _STATE_KEY
            if self.namespace == "avito"
            else f"request_governor_v1:{self.namespace}"
        )
        base_key = str(self.database) if self.database else "process-default"
        key = f"{base_key}:{self.namespace}"
        with _SHARED_STATES_LOCK:
            self._state = _SHARED_STATES.setdefault(
                key,
                _SharedGovernorState(),
            )
        self._load_persisted_state()

    @contextmanager
    def request(self, kind: str) -> Iterator[None]:
        priority = _PRIORITIES.get(kind, _PRIORITIES["detail"])
        lane = _lane(kind)
        bypass_limits = kind in _FORCED_KINDS
        token = object()
        with self._state.condition:
            self._refresh_cooldown_locked()
            cooldown = self._state.cooldowns.get(lane)
            if (
                not bypass_limits
                and cooldown is not None
                and cooldown > _utc_now()
            ):
                raise RequestDeferredError(cooldown, kind)
            budget_retry = self._budget_retry_at_locked(lane)
            if not bypass_limits and budget_retry is not None:
                raise RequestDeferredError(budget_retry, kind)
            heapq.heappush(
                self._state.queue,
                (priority, next(_SEQUENCE), token),
            )
            while True:
                self._refresh_cooldown_locked()
                cooldown = self._state.cooldowns.get(lane)
                if (
                    not bypass_limits
                    and cooldown is not None
                    and cooldown > _utc_now()
                ):
                    self._remove_waiter_locked(token)
                    raise RequestDeferredError(cooldown, kind)
                budget_retry = self._budget_retry_at_locked(lane)
                if not bypass_limits and budget_retry is not None:
                    self._remove_waiter_locked(token)
                    raise RequestDeferredError(budget_retry, kind)
                is_first = (
                    bool(self._state.queue)
                    and self._state.queue[0][2] is token
                )
                delay = (
                    0.0
                    if bypass_limits
                    else self._state.next_allowed_at - time.monotonic()
                )
                if is_first and not self._state.active and delay <= 0:
                    heapq.heappop(self._state.queue)
                    self._state.active = True
                    self._state.request_times.setdefault(lane, []).append(
                        time.time()
                    )
                    self._persist_locked()
                    break
                self._state.condition.wait(
                    timeout=max(0.05, min(max(delay, 0.05), 1.0))
                )
        try:
            yield
        finally:
            lower, upper = _DELAYS.get(kind, _DELAYS["detail"])
            with self._state.condition:
                self._state.active = False
                self._state.next_allowed_at = (
                    time.monotonic() + random.uniform(lower, upper)
                )
                self._state.condition.notify_all()

    @property
    def cooldown_until(self) -> datetime | None:
        return self.cooldown_until_for("search")

    def cooldown_until_for(self, kind: str) -> datetime | None:
        lane = _lane(kind)
        with self._state.condition:
            self._refresh_cooldown_locked()
            budget_retry = self._budget_retry_at_locked(lane)
            candidates = [
                value
                for value in (
                    self._state.cooldowns.get(lane),
                    budget_retry,
                )
                if value is not None and value > _utc_now()
            ]
            return max(candidates) if candidates else None

    def record_rate_limit(
        self,
        *,
        retry_after_seconds: int | None = None,
        kind: str = "search",
    ) -> datetime:
        lane = _lane(kind)
        with self._state.condition:
            strikes = self._state.strikes.get(lane, 0) + 1
            self._state.strikes[lane] = strikes
            self._state.successes[lane] = 0
            exponential_seconds = min(
                RATE_LIMIT_MAX_MINUTES * 60,
                RATE_LIMIT_BASE_MINUTES
                * 60
                * (2 ** (strikes - 1)),
            )
            delay_seconds = max(
                exponential_seconds,
                retry_after_seconds or 0,
            )
            proposed = _utc_now() + timedelta(seconds=delay_seconds)
            if (
                self._state.cooldowns.get(lane) is None
                or proposed > self._state.cooldowns[lane]
            ):
                self._state.cooldowns[lane] = proposed
            self._persist_locked()
            self._state.condition.notify_all()
            return self._state.cooldowns[lane]

    def record_success(self, kind: str = "search") -> None:
        lane = _lane(kind)
        with self._state.condition:
            successes = self._state.successes.get(lane, 0) + 1
            self._state.successes[lane] = successes
            changed = False
            if (
                self._state.cooldowns.get(lane) is not None
                and self._state.cooldowns[lane] <= _utc_now()
            ):
                self._state.cooldowns.pop(lane, None)
                changed = True
            if successes >= 10 and self._state.strikes.get(lane, 0):
                self._state.strikes[lane] -= 1
                self._state.successes[lane] = 0
                changed = True
            if changed:
                self._persist_locked()

    def _remove_waiter_locked(self, token: object) -> None:
        self._state.queue = [
            waiter for waiter in self._state.queue if waiter[2] is not token
        ]
        heapq.heapify(self._state.queue)

    def _budget_retry_at_locked(self, lane: str) -> datetime | None:
        threshold = time.time() - 3600
        request_times = [
            value
            for value in self._state.request_times.get(lane, [])
            if value > threshold
        ]
        self._state.request_times[lane] = request_times
        if len(request_times) < HOURLY_REQUEST_BUDGET:
            return None
        retry_timestamp = min(request_times) + 3600
        return datetime.fromtimestamp(retry_timestamp, tz=timezone.utc)

    def _connect(self) -> sqlite3.Connection | None:
        if self.database is None:
            return None
        connection = sqlite3.connect(self.database, timeout=10)
        configure_sqlite_connection(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        return connection

    def _load_persisted_state(self) -> None:
        with self._state.condition:
            self._refresh_cooldown_locked()

    def _refresh_cooldown_locked(self) -> None:
        try:
            connection = self._connect()
            if connection is None:
                return
            try:
                row = connection.execute(
                    "SELECT value FROM app_metadata WHERE key = ?",
                    (self._state_key,),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return
        if row is None:
            return
        try:
            payload = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return
        persisted_cooldowns = payload.get("cooldowns")
        if not isinstance(persisted_cooldowns, dict):
            persisted_cooldowns = {
                "background": payload.get("cooldown_until")
            }
        for lane, raw_value in persisted_cooldowns.items():
            persisted_until = _parse_datetime(raw_value)
            if (
                persisted_until is not None
                and (
                    self._state.cooldowns.get(lane) is None
                    or persisted_until > self._state.cooldowns[lane]
                )
            ):
                self._state.cooldowns[lane] = persisted_until

        persisted_strikes = payload.get("strikes", {})
        if not isinstance(persisted_strikes, dict):
            persisted_strikes = {"background": persisted_strikes}
        for lane, value in persisted_strikes.items():
            self._state.strikes[lane] = max(
                self._state.strikes.get(lane, 0),
                int(value or 0),
            )

        persisted_times = payload.get("request_times", {})
        if isinstance(persisted_times, list):
            persisted_times = {"background": persisted_times}
        if isinstance(persisted_times, dict):
            threshold = time.time() - 3600
            for lane, values in persisted_times.items():
                if not isinstance(values, list):
                    continue
                self._state.request_times[lane] = sorted(
                    {
                        *self._state.request_times.get(lane, []),
                        *(
                            float(value)
                            for value in values
                            if isinstance(value, (int, float))
                            and float(value) > threshold
                        ),
                    }
                )

    def _persist_locked(self) -> None:
        try:
            connection = self._connect()
            if connection is None:
                return
            try:
                connection.execute(
                    """
                    INSERT INTO app_metadata (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (
                        self._state_key,
                        json.dumps(
                            {
                                "cooldowns": {
                                    lane: value.isoformat()
                                    for lane, value
                                    in self._state.cooldowns.items()
                                },
                                "strikes": self._state.strikes,
                                "request_times": self._state.request_times,
                            }
                        ),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
        except sqlite3.Error:
            return

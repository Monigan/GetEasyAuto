from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ListingActivity:
    stage: str
    priority: str
    state: str
    updated_at: float
    title: str | None = None
    url: str | None = None


_LOCK = threading.Lock()
_ACTIVITIES: dict[tuple[str, str], ListingActivity] = {}
_RUNNING_STALE_SECONDS = 10 * 60
_FINISHED_STALE_SECONDS = 20 * 60


def _is_stale(activity: ListingActivity) -> bool:
    ttl = (
        _RUNNING_STALE_SECONDS
        if activity.state == "running"
        else _FINISHED_STALE_SECONDS
    )
    return time.monotonic() - activity.updated_at > ttl


def set_listing_activity(
    source: str,
    external_id: str,
    stage: str,
    *,
    priority: str,
    state: str = "running",
    title: str | None = None,
    url: str | None = None,
) -> None:
    with _LOCK:
        _ACTIVITIES[(source, external_id)] = ListingActivity(
            stage=stage,
            priority=priority,
            state=state,
            updated_at=time.monotonic(),
            title=title,
            url=url,
        )


def finish_listing_activity(
    source: str,
    external_id: str,
    stage: str,
    *,
    priority: str,
    state: str,
    title: str | None = None,
    url: str | None = None,
) -> None:
    if state not in {"success", "error"}:
        raise ValueError("Finished activity state must be success or error")
    set_listing_activity(
        source,
        external_id,
        stage,
        priority=priority,
        state=state,
        title=title,
        url=url,
    )


def clear_listing_activity(source: str, external_id: str) -> None:
    with _LOCK:
        _ACTIVITIES.pop((source, external_id), None)


def listing_activity(source: str, external_id: str) -> ListingActivity | None:
    key = (source, external_id)
    with _LOCK:
        activity = _ACTIVITIES.get(key)
        if activity is not None and _is_stale(activity):
            _ACTIVITIES.pop(key, None)
            return None
        return activity


def listing_activities() -> dict[tuple[str, str], ListingActivity]:
    with _LOCK:
        stale = [
            key
            for key, activity in _ACTIVITIES.items()
            if _is_stale(activity)
        ]
        for key in stale:
            _ACTIVITIES.pop(key, None)
        return dict(_ACTIVITIES)

"""Helpers for normalizing Radarr/Sonarr webhook payloads."""

from __future__ import annotations

from typing import Any


def normalize_event_type(event_type: str | None) -> str:
    return (event_type or "").strip()


def is_known_event(event_type: str, known_events: set[str]) -> bool:
    if not event_type:
        return False
    lowered = event_type.lower()
    return any(candidate.lower() == lowered for candidate in known_events)


def get_delete_reason(payload: dict[str, Any]) -> str:
    for key in ("deleteReason", "DeleteReason", "reason", "Reason"):
        value = payload.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def should_skip_file_delete(delete_reason: str, skip_reasons: set[str]) -> bool:
    if not delete_reason:
        return False
    return delete_reason.lower() in {reason.lower() for reason in skip_reasons}


def get_nested_object(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def get_field(obj: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in obj and obj[key] is not None:
            return obj[key]
    return None


def extract_radarr_movie(payload: dict[str, Any]) -> dict[str, Any]:
    movie = get_nested_object(payload, "movie", "Movie")
    if movie:
        return movie

    remote_movie = get_nested_object(payload, "remoteMovie", "RemoteMovie")
    if remote_movie:
        return remote_movie

    return {}


def extract_sonarr_series(payload: dict[str, Any]) -> dict[str, Any]:
    series = get_nested_object(payload, "series", "Series")
    if series:
        return series

    episodes = payload.get("episodes") or payload.get("Episodes") or []
    if episodes and isinstance(episodes[0], dict):
        series_id = get_field(episodes[0], "seriesId", "SeriesId")
        if series_id is not None:
            return {"id": series_id}

    return {}

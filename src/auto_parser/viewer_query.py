from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from auto_parser.sqlite_support import configure_sqlite_connection


POWER_EXPRESSION = (
    "CASE WHEN json_valid(l.attributes_json) "
    "THEN CAST(json_extract(l.attributes_json, '$.\"Мощность\"') AS INTEGER) "
    "END"
)
SORTS = {
    "newest": "last_seen_at DESC",
    "recently_updated": "last_seen_at DESC",
    "recently_added": "first_seen_at DESC",
    "price_asc": "price IS NULL, price ASC",
    "price_desc": "price IS NULL, price DESC",
    "mileage_asc": "mileage_km IS NULL, mileage_km ASC",
    "mileage_desc": "mileage_km IS NULL, mileage_km DESC",
    "power_asc": f"{POWER_EXPRESSION} IS NULL, {POWER_EXPRESSION} ASC",
    "power_desc": f"{POWER_EXPRESSION} IS NULL, {POWER_EXPRESSION} DESC",
}
ATTRIBUTE_FILTERS = {
    "year": "Год выпуска",
    "fuel": "Тип двигателя",
    "engine_volume": "Объём двигателя",
    "transmission": "Коробка передач",
    "drive": "Привод",
    "body": "Тип кузова",
    "color": "Цвет",
    "owners": "Владельцев по ПТС",
    "pts": "ПТС",
    "condition": "Состояние",
}


def normalize_text(value: Any) -> str:
    """Return comparable text, repairing values saved by the legacy importer."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        repaired = text.encode("latin1").decode("cp1251")
    except (UnicodeEncodeError, UnicodeDecodeError):
        repaired = text
    original_cyrillic = sum("а" <= char.casefold() <= "я" or char.casefold() == "ё" for char in text)
    repaired_cyrillic = sum("а" <= char.casefold() <= "я" or char.casefold() == "ё" for char in repaired)
    return (repaired if repaired_cyrillic > original_cyrillic else text).casefold()


def integer(value: str | None, *, minimum: int = 0) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return max(minimum, int(value))
    except ValueError:
        return None


def build_filters(
    query: dict[str, list[str]],
    *,
    apply_visibility: bool = False,
    default_status: str | None = None,
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    values: list[Any] = []
    source_hidden = False
    profile_id = integer(query.get("profile_id", [None])[0])
    if profile_id is not None and profile_id > 0:
        conditions.append(
            "EXISTS ("
            "SELECT 1 FROM search_profile_listings spl "
            "WHERE spl.profile_id = ? "
            "AND spl.source = l.source "
            "AND spl.external_id = l.external_id"
            ")"
        )
        values.append(profile_id)
    if apply_visibility:
        visibility = query.get("visibility", ["visible"])[0].strip()
        if visibility == "source_hidden":
            conditions.append("l.status = 'hidden'")
            source_hidden = True
        elif visibility == "hidden":
            conditions.append("l.hidden = 1")
        elif visibility != "all":
            conditions.append("l.hidden = 0")

    if query.get("favorite", [""])[0].strip() == "1":
        conditions.append(
            "EXISTS (SELECT 1 FROM listing_user_data ud "
            "WHERE ud.source = l.source "
            "AND ud.external_id = l.external_id "
            "AND ud.favorite = 1)"
        )

    for name, expression in (
        ("min_price", "l.price >= ?"),
        ("max_price", "l.price <= ?"),
        ("min_mileage", "l.mileage_km >= ?"),
        ("max_mileage", "l.mileage_km <= ?"),
    ):
        value = integer(query.get(name, [None])[0])
        if value is not None:
            conditions.append(expression)
            values.append(value)

    for name, expression in (
        ("location", "NORMALIZE_TEXT(l.location) LIKE ?"),
        ("brand", "NORMALIZE_TEXT(l.brand) LIKE ?"),
        ("model", "NORMALIZE_TEXT(l.model) LIKE ?"),
    ):
        value = query.get(name, [""])[0].strip()
        if value:
            conditions.append(expression)
            values.append(f"%{normalize_text(value)}%")

    for name, expression in (
        ("source", "l.source = ?"),
        ("external_id", "l.external_id = ?"),
    ):
        value = query.get(name, [""])[0].strip()
        if value:
            conditions.append(expression)
            values.append(value)

    first_seen_date = query.get("first_seen_date", [""])[0].strip()
    if first_seen_date:
        conditions.append("substr(l.first_seen_at, 1, 10) = ?")
        values.append(first_seen_date)

    status = query.get("status", [""])[0].strip()
    if status:
        conditions.append("l.status = ?")
        values.append(status)
    elif default_status and not source_hidden:
        conditions.append("l.status = ?")
        values.append(default_status)

    for parameter, attribute_name in ATTRIBUTE_FILTERS.items():
        selected = [
            value.strip()
            for value in query.get(parameter, [])
            if value.strip()
        ]
        if not selected:
            continue
        placeholders = ", ".join("?" for _ in selected)
        conditions.append(
            "CASE WHEN json_valid(l.attributes_json) "
            "THEN json_extract(l.attributes_json, ?) "
            f"END IN ({placeholders})"
        )
        values.append(f'$."{attribute_name}"')
        values.extend(selected)

    search = query.get("q", [""])[0].strip().casefold()
    if search:
        conditions.append(
            "(LOWER(l.title) LIKE ? "
            "OR LOWER(COALESCE(l.description, '')) LIKE ? "
            "OR LOWER(COALESCE(l.brand, '')) LIKE ? "
            "OR LOWER(COALESCE(l.model, '')) LIKE ? "
            "OR LOWER(COALESCE(l.location, '')) LIKE ? "
            "OR LOWER(COALESCE(l.attributes_json, '')) LIKE ?)"
        )
        pattern = f"%{search}%"
        values.extend([pattern] * 6)

    return (" WHERE " + " AND ".join(conditions) if conditions else ""), values


@contextmanager
def open_database(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.create_function("NORMALIZE_TEXT", 1, normalize_text, deterministic=True)
    configure_sqlite_connection(connection)
    try:
        yield connection
    finally:
        connection.close()

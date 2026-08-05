from __future__ import annotations

import argparse
import json
import logging
import math
import mimetypes
import re
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote_plus, urlparse

from auto_parser.activity import (
    clear_listing_activity,
    listing_activities,
    listing_activity,
    set_listing_activity,
)
from auto_parser.images import (
    deduplicate_image_urls,
)
from auto_parser.garage_api import GarageApiMixin
from auto_parser.models import utc_now_iso
from auto_parser.notification_api import NotificationApiMixin
from auto_parser.parts_api import PartsApiMixin
from auto_parser.request_governor import RequestGovernor
from auto_parser.service import SearchService
from auto_parser.spare_parts import listing_spare_parts_payload
from auto_parser.sources import (
    ALL_CARS_QUERY,
    source_from_name,
    source_name_from_url,
)
from auto_parser.sources.base import SourceError
from auto_parser.storage import ListingRepository


WEB_ROOT = Path(__file__).with_name("web")
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
def _integer(value: str | None, *, minimum: int = 0) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return max(minimum, int(value))
    except ValueError:
        return None


def _filters(
    query: dict[str, list[str]],
    *,
    apply_visibility: bool = False,
    default_status: str | None = None,
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    values: list[Any] = []
    source_hidden = False
    profile_id = _integer(query.get("profile_id", [None])[0])
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
    mapping = (
        ("min_price", "l.price >= ?"),
        ("max_price", "l.price <= ?"),
        ("min_mileage", "l.mileage_km >= ?"),
        ("max_mileage", "l.mileage_km <= ?"),
    )
    for name, expression in mapping:
        value = _integer(query.get(name, [None])[0])
        if value is not None:
            conditions.append(expression)
            values.append(value)

    location = query.get("location", [""])[0].strip()
    if location:
        conditions.append("l.location = ?")
        values.append(location)

    brand = query.get("brand", [""])[0].strip()
    if brand:
        conditions.append("l.brand = ?")
        values.append(brand)

    model = query.get("model", [""])[0].strip()
    if model:
        conditions.append("l.model = ?")
        values.append(model)

    source = query.get("source", [""])[0].strip()
    external_id = query.get("external_id", [""])[0].strip()
    if source:
        conditions.append("l.source = ?")
        values.append(source)
    if external_id:
        conditions.append("l.external_id = ?")
        values.append(external_id)

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
            "(LOWER(l.title) LIKE ? OR LOWER(COALESCE(l.description, '')) LIKE ?)"
        )
        pattern = f"%{search}%"
        values.extend((pattern, pattern))

    return (" WHERE " + " AND ".join(conditions) if conditions else ""), values


@contextmanager
def _open_database(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _histogram(
    values: list[int], boundaries: list[int], labels: list[str]
) -> list[dict[str, Any]]:
    counts = [0] * len(labels)
    for value in values:
        index = len(boundaries)
        for candidate, boundary in enumerate(boundaries):
            if value < boundary:
                index = candidate
                break
        counts[index] += 1
    result = []
    for index, (label, count) in enumerate(zip(labels, counts)):
        result.append(
            {
                "label": label,
                "value": count,
                "min_value": boundaries[index - 1] if index else None,
                "max_value": (
                    boundaries[index] - 1
                    if index < len(boundaries)
                    else None
                ),
            }
        )
    return result


def _correlation(pairs: list[tuple[int, int]]) -> float | None:
    if len(pairs) < 2:
        return None
    prices = [pair[0] for pair in pairs]
    mileages = [pair[1] for pair in pairs]
    mean_price = statistics.fmean(prices)
    mean_mileage = statistics.fmean(mileages)
    numerator = sum(
        (price - mean_price) * (mileage - mean_mileage)
        for price, mileage in pairs
    )
    denominator = math.sqrt(
        sum((price - mean_price) ** 2 for price in prices)
        * sum((mileage - mean_mileage) ** 2 for mileage in mileages)
    )
    return round(numerator / denominator, 3) if denominator else None


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return round(
        ordered[lower] * (1 - fraction) + ordered[upper] * fraction
    )


def _repair_legacy_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        repaired = text.encode("latin1").decode("cp1251")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    original_cyrillic = sum("А" <= char <= "я" or char == "ё" for char in text)
    repaired_cyrillic = sum(
        "А" <= char <= "я" or char == "ё" for char in repaired
    )
    return repaired if repaired_cyrillic > original_cyrillic else text


def _row_attributes(row: sqlite3.Row) -> dict[str, str]:
    try:
        raw = json.loads(row["attributes_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return {
        _repair_legacy_text(key): _repair_legacy_text(value)
        for key, value in raw.items()
    }


def _listing_year(row: Any, attributes: dict[str, str]) -> int | None:
    raw = attributes.get("Год выпуска", "")
    match = re.search(r"\b((?:19|20)\d{2})\b", raw)
    if match is None:
        match = re.search(
            r"\b((?:19|20)\d{2})\b",
            _repair_legacy_text(row["title"]),
        )
    if match is None:
        return None
    year = int(match.group(1))
    return year if 1950 <= year <= datetime.now().year + 1 else None


def _drive2_url(brand: str | None, model: str | None, year: int | None) -> str | None:
    query = " ".join(
        filter(None, (brand, model, str(year) if year else None, "бортжурнал"))
    )
    if not query:
        return None
    return f"https://www.drive2.ru/search/?text={quote_plus(query)}"


def _top_distribution(
    values: list[str],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    return [
        {"label": label, "value": value}
        for label, value in Counter(item for item in values if item).most_common(
            limit
        )
    ]


class ViewerHandler(
    GarageApiMixin,
    PartsApiMixin,
    NotificationApiMixin,
    BaseHTTPRequestHandler,
):
    database: Path
    cache_dir: Path

    def handle(self) -> None:
        try:
            super().handle()
        except ConnectionError:
            client = getattr(self, "client_address", ("unknown",))[0]
            print(
                f"[viewer] Клиент {client} закрыл соединение до получения ответа"
            )

    def log_message(self, format: str, *args: object) -> None:
        message = format % args
        routine_path = self.path.split("?", 1)[0]
        routine_poll = (
            (
                self.command == "GET"
                and routine_path
                in {
                    "/api/listings",
                    "/api/refresh-activity",
                    "/api/stats",
                    "/api/search-profiles",
                    "/api/notifications",
                }
            )
            or (
                self.command == "POST"
                and routine_path == "/api/displayed-listings"
            )
        ) and " 200 " in f" {message} "
        if routine_poll:
            logging.getLogger(__name__).debug(
                "[viewer] %s - %s",
                self.address_string(),
                message,
            )
            return
        print(f"[viewer] {self.address_string()} - {message}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if self._garage_get_route(parsed.path):
            return
        if self._notification_get_route(parsed.path):
            return
        if parsed.path == "/api/listings":
            self._listings(parse_qs(parsed.query))
        elif parsed.path == "/api/sold":
            self._sold(parse_qs(parsed.query))
        elif parsed.path == "/api/refresh-activity":
            self._refresh_activity()
        elif parsed.path == "/api/stats":
            self._stats(parse_qs(parsed.query))
        elif parsed.path == "/api/market":
            self._market(parse_qs(parsed.query))
        elif parsed.path == "/api/export-analysis":
            self._export_analysis(parse_qs(parsed.query))
        elif parsed.path == "/api/meta":
            self._meta()
        elif parsed.path == "/api/search-profiles":
            self._search_profiles()
        elif parsed.path == "/api/vehicle-analyses":
            self._vehicle_analyses()
        elif parsed.path == "/api/parts/cars":
            self._parts_cars()
        elif parsed.path == "/api/parts":
            self._parts(parse_qs(parsed.query))
        elif parsed.path == "/api/spare-parts":
            self._spare_parts_catalog(parse_qs(parsed.query))
        elif parsed.path.startswith("/media/"):
            self._media(parsed.path)
        else:
            self._static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if self._garage_post_route(parsed.path):
            return
        if self._notification_post_route(parsed.path):
            return
        if parsed.path == "/api/search-profiles":
            self._create_search_profile()
            return
        if parsed.path == "/api/vehicle-analysis":
            self._save_vehicle_analysis()
            return
        if parsed.path == "/api/displayed-listings":
            self._set_displayed_listings()
            return
        if parsed.path == "/api/parts":
            self._create_part()
            return
        if parsed.path == "/api/parts/seed":
            self._seed_parts()
            return
        if parsed.path == "/api/spare-parts/import":
            self._import_spare_parts()
            return
        if parsed.path == "/api/spare-parts/import-html":
            self._import_spare_parts_html()
            return
        if parsed.path == "/api/spare-parts/import-for-car":
            self._import_spare_parts_for_car()
            return
        parts = parsed.path.strip("/").split("/")
        if (
            len(parts) == 5
            and parts[:2] == ["api", "listings"]
            and parts[4] == "images"
        ):
            prepare = (
                parse_qs(parsed.query).get("prepare", ["0"])[0] == "1"
            )
            self._cache_listing_gallery(parts[2], parts[3], prepare=prepare)
            return
        if (
            len(parts) == 4
            and parts[:2] == ["api", "search-profiles"]
            and parts[3] == "run"
        ):
            self._run_search_profile(_integer(parts[2]))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        if self._garage_patch_route(path):
            return
        if self._notification_patch_route(path):
            return
        parts = path.strip("/").split("/")
        if (
            len(parts) == 5
            and parts[:2] == ["api", "listings"]
            and parts[4] == "visibility"
        ):
            self._set_listing_visibility(parts[2], parts[3])
            return
        if (
            len(parts) == 5
            and parts[:2] == ["api", "listings"]
            and parts[4] == "sale"
        ):
            self._set_listing_sale(parts[2], parts[3])
            return
        if len(parts) == 3 and parts[:2] == ["api", "search-profiles"]:
            self._update_search_profile(_integer(parts[2]))
            return
        if len(parts) == 3 and parts[:2] == ["api", "parts"]:
            self._update_part(_integer(parts[2]))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if self._garage_delete_route(path):
            return
        if self._notification_delete_route(path):
            return
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "search-profiles"]:
            profile_id = _integer(parts[2])
            if profile_id is None:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            with ListingRepository(self.database) as repository:
                deleted = repository.delete_search_profile(profile_id)
            self._json({"deleted": deleted})
            return
        if len(parts) == 3 and parts[:2] == ["api", "parts"]:
            self._delete_part(_integer(parts[2]))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _download_json(self, payload: Any, filename: str) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{filename}"',
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _listings(self, query: dict[str, list[str]]) -> None:
        where, values = _filters(
            query,
            apply_visibility=True,
            default_status="active",
        )
        page = _integer(query.get("page", ["1"])[0], minimum=1) or 1
        page_size = min(
            _integer(query.get("page_size", ["24"])[0], minimum=1) or 24,
            100,
        )
        sort = SORTS.get(
            query.get("sort", ["recently_updated"])[0],
            SORTS["recently_updated"],
        )
        with _open_database(self.database) as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM listings l{where}", values
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT l.*,
                    (SELECT COUNT(*) FROM listing_images i
                     WHERE i.source = l.source
                       AND i.external_id = l.external_id
                       AND i.local_path IS NOT NULL) AS image_count
                    ,
                    (SELECT COUNT(*) FROM listing_images i
                     WHERE i.source = l.source
                       AND i.external_id = l.external_id) AS image_source_count
                    ,
                    (SELECT g.id FROM garage_cars g
                     WHERE g.listing_source = l.source
                       AND g.listing_external_id = l.external_id
                     LIMIT 1) AS garage_id
                FROM listings l
                {where}
                ORDER BY {sort}
                LIMIT ? OFFSET ?
                """,
                [*values, page_size, (page - 1) * page_size],
            ).fetchall()
        items = []
        with ListingRepository(self.database) as repository:
            for row in rows:
                item = dict(row)
                try:
                    item["attributes"] = json.loads(
                        item.pop("attributes_json") or "{}"
                    )
                except (json.JSONDecodeError, TypeError):
                    item["attributes"] = {}
                year = _listing_year(item, item["attributes"])
                item["year"] = year
                item["published_at_inferred"] = not bool(item["published_at"])
                item["published_at"] = (
                    item["published_at"] or item["first_seen_at"]
                )
                exact_trim = item["attributes"].get("Комплектация")
                assigned_trim = repository.listing_trim_assignment(
                    item["source"],
                    item["external_id"],
                )
                analysis_trim = exact_trim or assigned_trim
                item["trim_exact"] = bool(exact_trim)
                item["analysis_trim_name"] = analysis_trim
                item["trim_options"] = (
                    [
                        {
                            "name": exact_trim,
                            "attributes": item["attributes"],
                            "source_url": (
                                item["url"]
                                if item["source"] == "drom"
                                else None
                            ),
                        }
                    ]
                    if exact_trim
                    else repository.matching_trims(
                        item["brand"],
                        item["model"],
                        year,
                    )
                )
                item["drive2_url"] = _drive2_url(
                    item["brand"],
                    item["model"],
                    year,
                )
                item["vehicle_analysis"] = repository.vehicle_analysis(
                    item["brand"],
                    item["model"],
                    trim_name=analysis_trim,
                )
                item["listing_assessment"] = (
                    repository.listing_vehicle_assessment(
                        item["source"],
                        item["external_id"],
                    )
                )
                item["spare_parts"] = listing_spare_parts_payload(
                    repository,
                    item,
                )
                item["thumbnail_url"] = (
                    f"/media/{item['source']}/{item['external_id']}/0"
                    if item["image_count"]
                    else None
                )
                item["image_pending"] = (
                    item["image_source_count"] > item["image_count"]
                )
                item["images"] = [
                    f"/media/{item['source']}/{item['external_id']}/{index}"
                    for index in range(item["image_count"])
                ]
                activity = listing_activity(
                    item["source"],
                    item["external_id"],
                )
                item["update_stage"] = activity.stage if activity else None
                item["update_priority"] = (
                    activity.priority if activity else None
                )
                item["update_state"] = activity.state if activity else None
                items.append(item)
        self._json(
            {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "pages": math.ceil(total / page_size) if total else 0,
            }
        )

    def _refresh_activity(self) -> None:
        activities = listing_activities()
        items = []
        listing_rows: dict[tuple[str, str], sqlite3.Row] = {}
        with _open_database(self.database) as connection:
            keys = list(activities)
            if keys:
                conditions = " OR ".join(
                    "(source = ? AND external_id = ?)" for _ in keys
                )
                parameters = [value for key in keys for value in key]
                rows = connection.execute(
                    f"""
                    SELECT source, external_id, title, url FROM listings
                    WHERE {conditions}
                    """,
                    parameters,
                ).fetchall()
                listing_rows = {
                    (row["source"], row["external_id"]): row
                    for row in rows
                }
            for (source, external_id), activity in activities.items():
                row = listing_rows.get((source, external_id))
                items.append(
                    {
                        "source": source,
                        "external_id": external_id,
                        "title": (
                            activity.title
                            or (
                                _repair_legacy_text(row["title"])
                                if row
                                else external_id
                            )
                        ),
                        "url": activity.url or (row["url"] if row else None),
                        "stage": activity.stage,
                        "priority": activity.priority,
                        "state": activity.state,
                        "age_seconds": max(
                            0,
                            round(time.monotonic() - activity.updated_at),
                        ),
                    }
                )
        state_order = {"running": 0, "error": 1, "success": 2}
        items.sort(
            key=lambda item: (
                state_order.get(item["state"], 3),
                item["age_seconds"],
            )
        )
        self._json(
            {
                "items": items,
                "running_count": sum(
                    item["state"] == "running" for item in items
                ),
            }
        )

    def _set_displayed_listings(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        client_id = str(payload.get("client_id") or "").strip()
        raw_items = payload.get("items")
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", client_id)
            or not isinstance(raw_items, list)
            or len(raw_items) > 100
        ):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        listings: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            source = str(raw_item.get("source") or "").strip().lower()
            external_id = str(
                raw_item.get("external_id") or ""
            ).strip()
            key = (source, external_id)
            if (
                source not in {"avito", "auto_ru", "drom"}
                or not external_id
                or len(external_id) > 200
            ):
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            if key not in seen:
                seen.add(key)
                listings.append(key)
        with ListingRepository(self.database) as repository:
            now = datetime.now(timezone.utc)
            repository.set_displayed_listings(
                client_id,
                listings,
                seen_at=now.isoformat(),
                stale_before=(now - timedelta(days=1)).isoformat(),
            )
        self._json({"tracked": len(listings)})

    def _save_vehicle_analysis(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        source = str(payload.get("source") or "").strip().lower()
        external_id = str(payload.get("external_id") or "").strip()
        analysis = payload.get("analysis")
        if (
            source not in {"avito", "auto_ru", "drom"}
            or not external_id
            or not isinstance(analysis, dict)
        ):
            self._json(
                {"error": "Ожидались объявление и JSON-объект анализа"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        with ListingRepository(self.database) as repository:
            listing = repository.get_listing(source, external_id)
            if listing is None:
                self._json(
                    {"error": "Объявление не найдено"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            year = _listing_year(
                {"title": listing.title},
                listing.attributes,
            )
            if not listing.brand or not listing.model:
                self._json(
                    {"error": "Не удалось определить марку и модель"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            model_analysis = analysis.get("model_analysis")
            listing_assessment = analysis.get("listing_assessment")
            if not isinstance(model_analysis, dict):
                model_analysis = analysis
            exact_trim = listing.attributes.get("Комплектация")
            assigned_trim = repository.listing_trim_assignment(
                source,
                external_id,
            )
            trim_name = str(
                payload.get("trim_name")
                or exact_trim
                or assigned_trim
                or ""
            ).strip()
            allowed_trims = {
                trim["name"]
                for trim in repository.matching_trims(
                    listing.brand,
                    listing.model,
                    year,
                )
            }
            if exact_trim:
                allowed_trims.add(exact_trim)
            if not trim_name or trim_name not in allowed_trims:
                self._json(
                    {"error": "Выберите подтверждённую комплектацию"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if exact_trim and trim_name != exact_trim:
                self._json(
                    {"error": "Комплектация объявления уже определена"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if not exact_trim:
                repository.set_listing_trim_assignment(
                    source,
                    external_id,
                    trim_name,
                    assigned_at=utc_now_iso(),
                )
            repository.save_vehicle_analysis(
                listing.brand,
                listing.model,
                trim_name,
                model_analysis,
                updated_at=utc_now_iso(),
            )
            if isinstance(listing_assessment, dict):
                repository.save_listing_vehicle_assessment(
                    source,
                    external_id,
                    listing_assessment,
                    description_snapshot=listing.description,
                    updated_at=utc_now_iso(),
                )
            saved = repository.vehicle_analysis(
                listing.brand,
                listing.model,
                trim_name=trim_name,
            )
            saved_assessment = repository.listing_vehicle_assessment(
                source,
                external_id,
            )
        self._json(
            {
                "saved": True,
                "vehicle_analysis": saved,
                "listing_assessment": saved_assessment,
                "analysis_trim_name": trim_name,
            }
        )

    def _vehicle_analyses(self) -> None:
        with ListingRepository(self.database) as repository:
            items = repository.vehicle_analyses()
        self._json(
            {
                "items": items,
                "summary": {
                    "vehicle_groups": len(items),
                    "weak_points": sum(
                        len(item["analysis"].get("weak_points") or [])
                        for item in items
                    ),
                },
            }
        )

    def _stats(self, query: dict[str, list[str]]) -> None:
        where, values = _filters(query, default_status="active")
        with _open_database(self.database) as connection:
            rows = connection.execute(
                f"""
                SELECT l.*,
                    EXISTS(
                        SELECT 1 FROM listing_images i
                        WHERE i.source = l.source
                          AND i.external_id = l.external_id
                          AND i.local_path IS NOT NULL
                    ) AS has_image
                FROM listings l{where}
                """,
                values,
            ).fetchall()
            history = connection.execute(
                """
                SELECT source, external_id, price, observed_at
                FROM price_history
                WHERE price IS NOT NULL
                ORDER BY source, external_id, observed_at DESC
                """
            ).fetchall()

        prices = [row["price"] for row in rows if row["price"] is not None]
        mileages = [
            row["mileage_km"] for row in rows if row["mileage_km"] is not None
        ]
        pairs = [
            (row["price"], row["mileage_km"])
            for row in rows
            if row["price"] is not None and row["mileage_km"] is not None
        ]
        selected_ids = {(row["source"], row["external_id"]) for row in rows}
        grouped_history: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        for record in history:
            key = (record["source"], record["external_id"])
            if key in selected_ids and len(grouped_history[key]) < 2:
                grouped_history[key].append(record)

        row_by_id = {(row["source"], row["external_id"]): row for row in rows}
        reductions = []
        for key, records in grouped_history.items():
            if len(records) == 2 and records[0]["price"] < records[1]["price"]:
                listing = row_by_id[key]
                reductions.append(
                    {
                        "title": listing["title"],
                        "url": listing["url"],
                        "current": records[0]["price"],
                        "previous": records[1]["price"],
                        "delta": records[0]["price"] - records[1]["price"],
                    }
                )
        reductions.sort(key=lambda item: item["delta"])

        locations = Counter(
            row["location"] or "Не указано" for row in rows
        ).most_common(7)
        self._json(
            {
                "count": len(rows),
                "avg_price": round(statistics.fmean(prices)) if prices else None,
                "median_price": round(statistics.median(prices)) if prices else None,
                "median_mileage": (
                    round(statistics.median(mileages)) if mileages else None
                ),
                "with_description": sum(bool(row["description"]) for row in rows),
                "with_image": sum(bool(row["has_image"]) for row in rows),
                "price_mileage_correlation": _correlation(pairs),
                "price_histogram": _histogram(
                    prices,
                    [500_000, 1_000_000, 2_000_000, 5_000_000],
                    ["< 500 тыс.", "0,5–1 млн", "1–2 млн", "2–5 млн", "> 5 млн"],
                ),
                "mileage_histogram": _histogram(
                    mileages,
                    [50_000, 100_000, 200_000, 300_000],
                    ["< 50 тыс.", "50–100 тыс.", "100–200 тыс.", "200–300 тыс.", "> 300 тыс."],
                ),
                "locations": [
                    {"label": label, "value": value}
                    for label, value in locations
                ],
                "reductions": reductions[:5],
            }
        )

    def _market(self, query: dict[str, list[str]]) -> None:
        where, values = _filters(query, default_status="active")
        sold_query = dict(query)
        sold_query.pop("status", None)
        sold_where, sold_values = _filters(
            sold_query,
            default_status="sold",
        )
        with _open_database(self.database) as connection:
            rows = connection.execute(
                f"""
                SELECT l.*,
                    EXISTS(
                        SELECT 1 FROM listing_images i
                        WHERE i.source = l.source
                          AND i.external_id = l.external_id
                          AND i.local_path IS NOT NULL
                    ) AS has_image
                FROM listings l{where}
                """,
                values,
            ).fetchall()
            history = connection.execute(
                """
                SELECT source, external_id, price, observed_at
                FROM price_history
                WHERE price IS NOT NULL
                ORDER BY source, external_id, observed_at DESC
                """
            ).fetchall()
            sold_rows = connection.execute(
                f"SELECT l.* FROM listings l{sold_where}",
                sold_values,
            ).fetchall()

        selected_ids = {(row["source"], row["external_id"]) for row in rows}
        records = []
        for row in rows:
            attributes = _row_attributes(row)
            records.append(
                {
                    "row": row,
                    "attributes": attributes,
                    "year": _listing_year(row, attributes),
                    "brand": _repair_legacy_text(row["brand"]) or "Не указано",
                    "model": _repair_legacy_text(row["model"]) or "Не указано",
                    "title": _repair_legacy_text(row["title"]),
                    "location": (
                        _repair_legacy_text(row["location"]) or "Не указано"
                    ),
                }
            )

        prices = [
            record["row"]["price"]
            for record in records
            if record["row"]["price"] is not None
        ]
        mileages = [
            record["row"]["mileage_km"]
            for record in records
            if record["row"]["mileage_km"] is not None
        ]
        price_mileage_pairs = [
            (record["row"]["price"], record["row"]["mileage_km"])
            for record in records
            if record["row"]["price"] is not None
            and record["row"]["mileage_km"] is not None
        ]

        grouped_history: dict[
            tuple[str, str], list[sqlite3.Row]
        ] = defaultdict(list)
        history_by_day: dict[str, list[int]] = defaultdict(list)
        for item in history:
            key = (item["source"], item["external_id"])
            if key not in selected_ids:
                continue
            if len(grouped_history[key]) < 2:
                grouped_history[key].append(item)
            history_by_day[item["observed_at"][:10]].append(item["price"])

        reductions = []
        increases = []
        price_changes = []
        for key, price_records in grouped_history.items():
            if len(price_records) < 2:
                continue
            delta = price_records[0]["price"] - price_records[1]["price"]
            row = next(
                record["row"] for record in records
                if (record["row"]["source"], record["row"]["external_id"])
                == key
            )
            previous = price_records[1]["price"]
            price_changes.append(
                {
                    "source": key[0],
                    "external_id": key[1],
                    "title": _repair_legacy_text(row["title"]),
                    "url": row["url"],
                    "previous": previous,
                    "current": price_records[0]["price"],
                    "delta": delta,
                    "delta_percent": (
                        round(delta / previous * 100, 1) if previous else None
                    ),
                    "observed_at": price_records[0]["observed_at"],
                }
            )
            if delta < 0:
                reductions.append(delta)
            elif delta > 0:
                increases.append(delta)
        price_changes.sort(key=lambda item: item["observed_at"], reverse=True)

        sold_prices = [
            row["sold_price"] or row["price"]
            for row in sold_rows
            if (row["sold_price"] or row["price"]) is not None
        ]
        sold_days = []
        for row in sold_rows:
            try:
                started = datetime.fromisoformat(
                    row["first_seen_at"].replace("Z", "+00:00")
                )
                finished = datetime.fromisoformat(
                    (row["sold_at"] or row["last_seen_at"]).replace(
                        "Z", "+00:00"
                    )
                )
                sold_days.append(max(0, (finished - started).days))
            except (AttributeError, TypeError, ValueError):
                pass

        now = datetime.now(timezone.utc)

        def seen_since(value: str, delta: timedelta) -> bool:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed >= now - delta
            except (TypeError, ValueError):
                return False

        def age_in_days(value: str | None) -> int | None:
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0, (now - parsed).days)
            except (TypeError, ValueError):
                return None

        views = [
            record["row"]["views_count"]
            for record in records
            if record["row"]["views_count"] is not None
        ]
        listing_ages = [
            age
            for record in records
            if (age := age_in_days(record["row"]["published_at"])) is not None
        ]

        additions_by_day: Counter[str] = Counter(
            record["row"]["first_seen_at"][:10] for record in records
        )
        timeline_days = [
            (now - timedelta(days=offset)).date().isoformat()
            for offset in range(29, -1, -1)
        ]

        brand_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        model_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        year_groups: dict[int, list[int]] = defaultdict(list)
        for record in records:
            brand_groups[record["brand"]].append(record)
            model_groups[
                f"{record['brand']} {record['model']}".strip()
            ].append(record)
            price = record["row"]["price"]
            if record["year"] is not None and price is not None:
                year_groups[record["year"]].append(price)

        def group_summary(
            groups: dict[str, list[dict[str, Any]]],
            limit: int,
        ) -> list[dict[str, Any]]:
            result = []
            for label, group in groups.items():
                group_prices = [
                    item["row"]["price"]
                    for item in group
                    if item["row"]["price"] is not None
                ]
                group_mileages = [
                    item["row"]["mileage_km"]
                    for item in group
                    if item["row"]["mileage_km"] is not None
                ]
                result.append(
                    {
                        "label": label,
                        "brand": group[0]["brand"],
                        "model": group[0]["model"],
                        "count": len(group),
                        "median_price": (
                            round(statistics.median(group_prices))
                            if group_prices
                            else None
                        ),
                        "median_mileage": (
                            round(statistics.median(group_mileages))
                            if group_mileages
                            else None
                        ),
                    }
                )
            result.sort(key=lambda item: item["count"], reverse=True)
            return result[:limit]

        attribute_names = {
            "fuel": "Тип двигателя",
            "transmission": "Коробка передач",
            "drive": "Привод",
            "body": "Тип кузова",
            "color": "Цвет",
            "owners": "Владельцев по ПТС",
        }
        composition = {
            name: _top_distribution(
                [
                    record["attributes"].get(attribute_name, "")
                    for record in records
                ]
            )
            for name, attribute_name in attribute_names.items()
        }

        comparable_groups: dict[
            tuple[str, str, int | None], list[int]
        ] = defaultdict(list)
        for record in records:
            price = record["row"]["price"]
            if price is not None:
                comparable_groups[
                    (record["brand"], record["model"], record["year"])
                ].append(price)
        deals = []
        for record in records:
            price = record["row"]["price"]
            if price is None:
                continue
            group = comparable_groups[
                (record["brand"], record["model"], record["year"])
            ]
            if len(group) < 3:
                continue
            benchmark = round(statistics.median(group))
            if benchmark <= 0:
                continue
            discount = round((benchmark - price) / benchmark * 100, 1)
            if discount < 5:
                continue
            deals.append(
                {
                    "title": record["title"],
                    "url": record["row"]["url"],
                    "price": price,
                    "benchmark": benchmark,
                    "discount_percent": discount,
                    "mileage_km": record["row"]["mileage_km"],
                    "location": record["location"],
                }
            )
        deals.sort(key=lambda item: item["discount_percent"], reverse=True)

        completeness_fields = (
            "description",
            "mileage_km",
            "views_count",
            "published_at",
        )
        complete_values = sum(
            bool(record["row"][field])
            for record in records
            for field in completeness_fields
        ) + sum(
            bool(record["row"]["has_image"]) + bool(record["attributes"])
            for record in records
        )
        completeness_total = len(records) * 6
        data_quality = (
            round(complete_values / completeness_total * 100)
            if completeness_total
            else 0
        )

        correlation = _correlation(price_mileage_pairs)
        insights = []
        if prices:
            p25 = _percentile(prices, 0.25)
            p75 = _percentile(prices, 0.75)
            insights.append(
                f"Половина рынка находится между "
                f"{p25:,} и {p75:,} ₽.".replace(",", " ")
            )
        if correlation is not None:
            strength = (
                "сильная"
                if abs(correlation) >= 0.65
                else "умеренная"
                if abs(correlation) >= 0.35
                else "слабая"
            )
            direction = "обратная" if correlation < 0 else "прямая"
            insights.append(
                f"Связь цены и пробега {strength}, {direction}: "
                f"коэффициент {correlation:.2f}."
            )
        if brand_groups:
            leader, leader_records = max(
                brand_groups.items(), key=lambda item: len(item[1])
            )
            share = round(len(leader_records) / len(records) * 100)
            insights.append(
                f"Самая представленная марка — {leader}: "
                f"{share}% текущей выборки."
            )
        if reductions:
            insights.append(
                f"Цена снижена у {len(reductions)} объявлений; "
                f"медианное снижение "
                f"{round(abs(statistics.median(reductions))):,} ₽.".replace(
                    ",", " "
                )
            )

        scatter = [
            {
                "price": price,
                "mileage": mileage,
                "title": record["title"],
                "brand": record["brand"],
                "source": record["row"]["source"],
                "external_id": record["row"]["external_id"],
            }
            for record in records
            if (price := record["row"]["price"]) is not None
            and (mileage := record["row"]["mileage_km"]) is not None
        ]
        if len(scatter) > 300:
            step = len(scatter) / 300
            scatter = [scatter[math.floor(index * step)] for index in range(300)]

        self._json(
            {
                "summary": {
                    "count": len(records),
                    "active_count": sum(
                        record["row"]["status"] == "active"
                        for record in records
                    ),
                    "price_count": len(prices),
                    "avg_price": (
                        round(statistics.fmean(prices)) if prices else None
                    ),
                    "median_price": (
                        round(statistics.median(prices)) if prices else None
                    ),
                    "min_price": min(prices) if prices else None,
                    "max_price": max(prices) if prices else None,
                    "price_p25": _percentile(prices, 0.25),
                    "price_p75": _percentile(prices, 0.75),
                    "avg_mileage": (
                        round(statistics.fmean(mileages))
                        if mileages
                        else None
                    ),
                    "median_mileage": (
                        round(statistics.median(mileages))
                        if mileages
                        else None
                    ),
                    "median_views": (
                        round(statistics.median(views)) if views else None
                    ),
                    "average_views": (
                        round(statistics.fmean(views)) if views else None
                    ),
                    "median_listing_age_days": (
                        round(statistics.median(listing_ages))
                        if listing_ages
                        else None
                    ),
                    "correlation": correlation,
                    "added_24h": sum(
                        seen_since(
                            record["row"]["first_seen_at"],
                            timedelta(days=1),
                        )
                        for record in records
                    ),
                    "added_7d": sum(
                        seen_since(
                            record["row"]["first_seen_at"],
                            timedelta(days=7),
                        )
                        for record in records
                    ),
                    "reductions_count": len(reductions),
                    "increases_count": len(increases),
                    "median_reduction": (
                        round(abs(statistics.median(reductions)))
                        if reductions
                        else None
                    ),
                    "data_quality": data_quality,
                    "inventory_value": sum(prices),
                },
                "insights": insights,
                "brands": group_summary(brand_groups, 12),
                "models": group_summary(model_groups, 12),
                "locations": _top_distribution(
                    [record["location"] for record in records], limit=10
                ),
                "composition": composition,
                "year_prices": [
                    {
                        "year": year,
                        "count": len(year_prices),
                        "median_price": round(
                            statistics.median(year_prices)
                        ),
                    }
                    for year, year_prices in sorted(year_groups.items())
                    if len(year_prices) >= 2
                ],
                "scatter": scatter,
                "additions_timeline": [
                    {"date": day, "value": additions_by_day[day]}
                    for day in timeline_days
                ],
                "price_timeline": [
                    {
                        "date": day,
                        "median_price": round(
                            statistics.median(history_by_day[day])
                        ),
                    }
                    for day in sorted(history_by_day)
                ],
                "price_changes": price_changes[:50],
                "sold_summary": {
                    "count": len(sold_rows),
                    "median_price": (
                        round(statistics.median(sold_prices))
                        if sold_prices else None
                    ),
                    "average_price": (
                        round(statistics.fmean(sold_prices))
                        if sold_prices else None
                    ),
                    "median_days_on_market": (
                        round(statistics.median(sold_days))
                        if sold_days else None
                    ),
                },
                "price_histogram": _histogram(
                    prices,
                    [500_000, 1_000_000, 2_000_000, 3_000_000, 5_000_000],
                    [
                        "< 500 тыс.",
                        "0,5–1 млн",
                        "1–2 млн",
                        "2–3 млн",
                        "3–5 млн",
                        "> 5 млн",
                    ],
                ),
                "mileage_histogram": _histogram(
                    mileages,
                    [50_000, 100_000, 150_000, 200_000, 300_000],
                    [
                        "< 50 тыс.",
                        "50–100 тыс.",
                        "100–150 тыс.",
                        "150–200 тыс.",
                        "200–300 тыс.",
                        "> 300 тыс.",
                    ],
                ),
                "deals": deals[:10],
            }
        )

    def _sold(self, query: dict[str, list[str]]) -> None:
        sold_query = dict(query)
        sold_query.pop("status", None)
        where, values = _filters(sold_query, default_status="sold")
        with _open_database(self.database) as connection:
            rows = connection.execute(
                f"""
                SELECT l.* FROM listings l{where}
                ORDER BY COALESCE(l.sold_at, l.last_seen_at) DESC
                """,
                values,
            ).fetchall()
        items = []
        days = []
        for row in rows:
            item = dict(row)
            item["sold_price"] = item["sold_price"] or item["price"]
            try:
                started = datetime.fromisoformat(
                    item["first_seen_at"].replace("Z", "+00:00")
                )
                finished = datetime.fromisoformat(
                    (item["sold_at"] or item["last_seen_at"]).replace(
                        "Z", "+00:00"
                    )
                )
                item["days_on_market"] = max(0, (finished - started).days)
                days.append(item["days_on_market"])
            except (AttributeError, TypeError, ValueError):
                item["days_on_market"] = None
            item.pop("attributes_json", None)
            items.append(item)
        prices = [item["sold_price"] for item in items if item["sold_price"]]
        source_counts = Counter(item["source"] for item in items)
        self._json(
            {
                "items": items,
                "summary": {
                    "count": len(items),
                    "median_price": (
                        round(statistics.median(prices)) if prices else None
                    ),
                    "average_price": (
                        round(statistics.fmean(prices)) if prices else None
                    ),
                    "median_days_on_market": (
                        round(statistics.median(days)) if days else None
                    ),
                    "sources": [
                        {"label": source, "value": count}
                        for source, count in source_counts.most_common()
                    ],
                },
            }
        )

    def _export_analysis(self, query: dict[str, list[str]]) -> None:
        where, values = _filters(query)
        with _open_database(self.database) as connection:
            rows = connection.execute(
                f"""
                SELECT l.*,
                    (SELECT COUNT(*) FROM listing_images i
                     WHERE i.source = l.source
                       AND i.external_id = l.external_id) AS image_count
                FROM listings l{where}
                ORDER BY l.last_seen_at DESC
                """,
                values,
            ).fetchall()

        exported = []
        price_mileage_pairs = []
        price_description_pairs = []
        price_quality_pairs = []
        mileage_quality_pairs = []
        for row in rows:
            attributes = _row_attributes(row)
            description = _repair_legacy_text(row["description"])
            words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", description)
            signals = {
                "mentions_accident": bool(
                    re.search(r"\b(?:дтп|авари)", description, re.I)
                ),
                "mentions_service": bool(
                    re.search(r"сервис|обслуж", description, re.I)
                ),
                "mentions_owners": bool(
                    re.search(r"владел|хозя", description, re.I)
                ),
                "mentions_repairs": bool(
                    re.search(r"ремонт|вложен|замен", description, re.I)
                ),
                "mentions_bargain": bool(
                    re.search(r"торг", description, re.I)
                ),
            }
            description_quality = min(
                100,
                (30 if description else 0)
                + min(35, len(words) // 3)
                + min(25, len(attributes) * 3)
                + min(10, int(row["image_count"] or 0) * 2),
            )
            year = _listing_year(row, attributes)
            power_match = re.search(
                r"\d+",
                attributes.get("Мощность", ""),
            )
            item = {
                "source": row["source"],
                "external_id": row["external_id"],
                "url": row["url"],
                "title": _repair_legacy_text(row["title"]),
                "brand": _repair_legacy_text(row["brand"]),
                "model": _repair_legacy_text(row["model"]),
                "year": year,
                "price_rub": row["price"],
                "mileage_km": row["mileage_km"],
                "power_hp": int(power_match.group()) if power_match else None,
                "location": _repair_legacy_text(row["location"]),
                "published_at": row["published_at"],
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "views_count": row["views_count"],
                "status": row["status"],
                "hidden": bool(row["hidden"]),
                "image_count": int(row["image_count"] or 0),
                "description": description,
                "description_length": len(description),
                "description_word_count": len(words),
                "description_quality_score": description_quality,
                "description_signals": signals,
                "attributes": attributes,
            }
            exported.append(item)
            price = row["price"]
            mileage = row["mileage_km"]
            if price is not None and mileage is not None:
                price_mileage_pairs.append((price, mileage))
            if price is not None:
                price_description_pairs.append((price, len(words)))
                price_quality_pairs.append((price, description_quality))
            if mileage is not None:
                mileage_quality_pairs.append((mileage, description_quality))

        payload = {
            "export_version": 1,
            "generated_at": utc_now_iso(),
            "purpose": (
                "Набор для анализа автомобильных объявлений в ChatGPT: "
                "цена, пробег, качество и содержание описания."
            ),
            "analysis_prompt": (
                "Проанализируй этот JSON как выборку объявлений автомобилей. "
                "Рассчитай и интерпретируй корреляции цены с пробегом, длиной "
                "и quality score описания. Сравни Pearson и Spearman, выдели "
                "выбросы, ценовые сегменты и объявления заметно дешевле аналогов. "
                "По возможности контролируй марку, модель, год и мощность; не "
                "выдавай корреляцию за причинность. Отдельно оцени влияние "
                "сигналов ДТП, обслуживания, ремонта, владельцев и торга."
            ),
            "filters": query,
            "summary": {
                "listing_count": len(exported),
                "with_price": sum(
                    item["price_rub"] is not None for item in exported
                ),
                "with_mileage": sum(
                    item["mileage_km"] is not None for item in exported
                ),
                "with_description": sum(
                    bool(item["description"]) for item in exported
                ),
                "pearson_price_mileage": _correlation(
                    price_mileage_pairs
                ),
                "pearson_price_description_words": _correlation(
                    price_description_pairs
                ),
                "pearson_price_description_quality": _correlation(
                    price_quality_pairs
                ),
                "pearson_mileage_description_quality": _correlation(
                    mileage_quality_pairs
                ),
            },
            "data_dictionary": {
                "description_quality_score": (
                    "Эвристика 0–100: наличие и объём описания, число "
                    "характеристик и фотографий."
                ),
                "description_signals": (
                    "Поиск тематических слов; это признаки текста, а не "
                    "подтверждённые факты о состоянии автомобиля."
                ),
            },
            "listings": exported,
        }
        self._download_json(
            payload,
            f"auto-market-analysis-{datetime.now().date().isoformat()}.json",
        )

    def _meta(self) -> None:
        with _open_database(self.database) as connection:
            locations = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT location FROM listings
                    WHERE location IS NOT NULL AND location != ''
                    ORDER BY location
                    """
                )
            ]
            brands = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT DISTINCT brand FROM listings
                    WHERE brand IS NOT NULL AND brand != ''
                    ORDER BY brand COLLATE NOCASE
                    """
                )
            ]
            sources = [
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT source FROM listings ORDER BY source"
                )
            ]
            attribute_rows = connection.execute(
                """
                SELECT attributes_json FROM listings
                WHERE attributes_json IS NOT NULL
                  AND attributes_json != '{}'
                """
            ).fetchall()
        attribute_values: dict[str, set[str]] = {
            parameter: set() for parameter in ATTRIBUTE_FILTERS
        }
        attribute_parameters = {
            attribute_name: parameter
            for parameter, attribute_name in ATTRIBUTE_FILTERS.items()
        }
        for row in attribute_rows:
            try:
                attributes = json.loads(row["attributes_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for raw_name, raw_value in attributes.items():
                name = _repair_legacy_text(raw_name)
                parameter = attribute_parameters.get(name)
                value = _repair_legacy_text(raw_value)
                if parameter and value:
                    attribute_values[parameter].add(value)

        def option_sort(parameter: str, value: str) -> tuple[Any, ...]:
            number = re.search(r"\d+(?:[.,]\d+)?", value)
            numeric = (
                float(number.group().replace(",", "."))
                if number
                else float("inf")
            )
            if parameter == "year":
                numeric = -numeric
            return (numeric, value.casefold())

        self._json(
            {
                "locations": locations,
                "brands": brands,
                "sources": sources,
                "attribute_options": {
                    parameter: sorted(
                        options,
                        key=lambda value, name=parameter: option_sort(
                            name,
                            value,
                        ),
                    )
                    for parameter, options in attribute_values.items()
                },
            }
        )

    def _read_json(self) -> dict[str, Any] | None:
        length = _integer(self.headers.get("Content-Length"))
        if length is None or length > 64 * 1024:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return None
        return payload if isinstance(payload, dict) else None

    def _search_profiles(self) -> None:
        with ListingRepository(self.database) as repository:
            profiles = [dict(row) for row in repository.search_profiles()]
        now = datetime.now(timezone.utc)
        cooldown = RequestGovernor(self.database).cooldown_until
        if cooldown is not None and cooldown > now:
            for profile in profiles:
                if profile.get("source", "avito") != "avito":
                    continue
                next_run_at = profile.get("next_run_at")
                try:
                    next_run = datetime.fromisoformat(
                        str(next_run_at).replace("Z", "+00:00")
                    )
                    if next_run.tzinfo is None:
                        next_run = next_run.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    next_run = now
                if (
                    profile.get("enabled")
                    and profile.get("last_status") != "running"
                    and next_run <= now
                ):
                    profile["waiting_reason"] = (
                        "Ожидание снятия ограничения Avito"
                    )
                    profile["waiting_until"] = cooldown.isoformat()
        self._json({"items": profiles})

    def _create_search_profile(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        all_cars = payload.get("all_cars") is True
        query = (
            ALL_CARS_QUERY
            if all_cars
            else str(payload.get("query", "")).strip()
        )
        source_name = str(payload.get("source", "avito")).strip().lower()
        region = str(payload.get("region", "all")).strip().lower()
        radius = _integer(str(payload.get("radius", "")))
        min_price = _integer(str(payload.get("min_price", "")))
        max_price = _integer(str(payload.get("max_price", "")))
        interval = _integer(str(payload.get("interval_minutes", "60")), minimum=15)
        if not query or not region or interval is None:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        if source_name not in {"avito", "auto_ru", "drom"}:
            self._json(
                {"error": "Источник объявлений не поддерживается"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        if (
            min_price is not None
            and max_price is not None
            and min_price > max_price
        ):
            self._json(
                {
                    "error": (
                        "Минимальная цена не может быть больше максимальной"
                    )
                },
                HTTPStatus.BAD_REQUEST,
            )
            return
        if all_cars:
            try:
                source_from_name(
                    source_name,
                    region=region,
                    radius=radius,
                    min_price=min_price,
                    max_price=max_price,
                ).build_search_url("")
            except ValueError as error:
                self._json(
                    {"error": str(error)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
        if query.startswith(("https://", "http://")):
            try:
                source_name = source_name_from_url(query)
                source_from_name(
                    source_name,
                    region=region,
                    radius=radius,
                    min_price=min_price,
                    max_price=max_price,
                    search_url=query,
                ).build_search_url("")
            except ValueError as error:
                self._json(
                    {"error": str(error)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
        try:
            with ListingRepository(self.database) as repository:
                profile_id = repository.add_search_profile(
                    source=source_name,
                    query=query,
                    region=region,
                    radius=radius,
                    min_price=min_price,
                    max_price=max_price,
                    interval_minutes=interval,
                    created_at=utc_now_iso(),
                )
        except sqlite3.IntegrityError:
            self._json(
                {"error": "Такой профиль уже существует"},
                HTTPStatus.CONFLICT,
            )
            return
        self._json({"id": profile_id}, HTTPStatus.CREATED)

    def _update_search_profile(self, profile_id: int | None) -> None:
        if profile_id is None:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        payload = self._read_json()
        if payload is None:
            return
        if "interval_minutes" in payload:
            payload["interval_minutes"] = max(
                15, int(payload["interval_minutes"])
            )
        if "enabled" in payload:
            payload["enabled"] = 1 if payload["enabled"] else 0
        for field in ("min_price", "max_price"):
            if field in payload:
                payload[field] = _integer(str(payload[field]))
        if (
            payload.get("min_price") is not None
            and payload.get("max_price") is not None
            and payload["min_price"] > payload["max_price"]
        ):
            self._json(
                {
                    "error": (
                        "Минимальная цена не может быть больше максимальной"
                    )
                },
                HTTPStatus.BAD_REQUEST,
            )
            return
        with ListingRepository(self.database) as repository:
            updated = repository.update_search_profile(profile_id, payload)
        self._json({"updated": updated})

    def _run_search_profile(self, profile_id: int | None) -> None:
        if profile_id is None:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        with ListingRepository(self.database) as repository:
            updated = repository.update_search_profile(
                profile_id, {"next_run_at": utc_now_iso(), "enabled": 1}
            )
        self._json({"scheduled": updated})

    def _set_listing_visibility(
        self,
        source_name: str,
        external_id: str,
    ) -> None:
        payload = self._read_json()
        if payload is None or not isinstance(payload.get("hidden"), bool):
            self._json(
                {"error": "Ожидался логический параметр hidden"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        with ListingRepository(self.database) as repository:
            updated = repository.set_listing_hidden(
                source_name,
                external_id,
                payload["hidden"],
            )
        if not updated:
            self._json(
                {"error": "Объявление не найдено"},
                HTTPStatus.NOT_FOUND,
            )
            return
        self._json({"updated": True, "hidden": payload["hidden"]})

    def _set_listing_sale(
        self,
        source_name: str,
        external_id: str,
    ) -> None:
        payload = self._read_json()
        sold_price = _integer(str(payload.get("sold_price", ""))) if payload else None
        if sold_price is None:
            self._json(
                {"error": "Укажите цену продажи"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        with ListingRepository(self.database) as repository:
            updated = repository.set_sold_price(
                source_name,
                external_id,
                sold_price,
            )
        if not updated:
            self._json(
                {"error": "Объявление не найдено"},
                HTTPStatus.NOT_FOUND,
            )
            return
        self._json({"updated": True, "sold_price": sold_price})

    def _cache_listing_gallery(
        self,
        source_name: str,
        external_id: str,
        *,
        prepare: bool = False,
    ) -> None:
        if source_name not in {"avito", "auto_ru", "drom"} or not external_id:
            self._json(
                {"error": "Источник объявления не поддерживается"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        with ListingRepository(self.database) as repository:
            listing = repository.get_listing(source_name, external_id)
        if listing is None:
            self._json(
                {"error": "Объявление не найдено"},
                HTTPStatus.NOT_FOUND,
            )
            return

        service = SearchService(
            source_from_name(source_name),
            governor=RequestGovernor(
                self.database,
                namespace=source_name,
            ),
        )
        warning = None
        gallery_refreshed = False
        activity_started = False
        mail_imported = (
            listing.source == "auto_ru"
            and listing.attributes.get("Канал получения")
            == "Письмо сохранённого поиска Auto.ru"
        )
        if prepare and not mail_imported:
            set_listing_activity(
                listing.source,
                listing.external_id,
                "Приоритетно обновляем описание и характеристики…",
                priority="interactive",
            )
            activity_started = True
            try:
                service.enrich_listing_details(
                    listing,
                    request_kind="interactive_detail",
                )
                gallery_refreshed = True
            except (OSError, SourceError) as error:
                warning = str(error)
        try:
            listing.image_urls = deduplicate_image_urls(
                listing.image_urls
            )
            missing_urls = [
                image_url
                for image_url in listing.image_urls
                if image_url not in listing.cached_images
            ]
            downloaded = 0
            if missing_urls:
                set_listing_activity(
                    listing.source,
                    listing.external_id,
                    "Загружаем фотографию открытой карточки…",
                    priority="interactive",
                )
                activity_started = True
                all_image_urls = listing.image_urls
                listing.image_urls = [missing_urls[0]]
                try:
                    downloaded = service.cache_images(
                        [listing],
                        cache_dir=self.cache_dir,
                        listings_limit=1,
                        images_per_listing=1,
                        request_kind="interactive_image",
                    )
                except (OSError, SourceError) as error:
                    warning = (
                        f"{warning}. {error}" if warning else str(error)
                    )
                finally:
                    listing.image_urls = all_image_urls
            with ListingRepository(self.database) as repository:
                repository.upsert_many([listing])
                if gallery_refreshed:
                    repository.replace_listing_images(listing)
                refreshed = repository.get_listing(source_name, external_id)
                stored_row = repository.connection.execute(
                    """
                    SELECT first_seen_at FROM listings
                    WHERE source = ? AND external_id = ?
                    """,
                    (source_name, external_id),
                ).fetchone()
                refreshed_year = (
                    _listing_year(
                        {"title": refreshed.title},
                        refreshed.attributes,
                    )
                    if refreshed
                    else None
                )
                exact_trim = (
                    refreshed.attributes.get("Комплектация")
                    if refreshed
                    else None
                )
                trim_options = (
                    [
                        {
                            "name": exact_trim,
                            "attributes": refreshed.attributes,
                            "source_url": (
                                refreshed.url
                                if refreshed.source == "drom"
                                else None
                            ),
                        }
                    ]
                    if exact_trim and refreshed
                    else repository.matching_trims(
                        refreshed.brand if refreshed else None,
                        refreshed.model if refreshed else None,
                        refreshed_year,
                    )
                )
                assigned_trim = repository.listing_trim_assignment(
                    source_name,
                    external_id,
                )
                analysis_trim = exact_trim or assigned_trim
                vehicle_analysis = repository.vehicle_analysis(
                    refreshed.brand if refreshed else None,
                    refreshed.model if refreshed else None,
                    trim_name=analysis_trim,
                )
                listing_assessment = (
                    repository.listing_vehicle_assessment(
                        source_name,
                        external_id,
                    )
                )
        except OSError as error:
            if activity_started:
                clear_listing_activity(
                    listing.source,
                    listing.external_id,
                )
            self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
            return

        cached_count = len(refreshed.cached_images) if refreshed else 0
        remaining_count = (
            len(
                [
                    image_url
                    for image_url in refreshed.image_urls
                    if image_url not in refreshed.cached_images
                ]
            )
            if refreshed
            else 0
        )
        stalled = remaining_count > 0 and downloaded == 0
        if stalled and warning is None:
            warning = "Не удалось скачать следующее изображение"
        if activity_started:
            clear_listing_activity(
                listing.source,
                listing.external_id,
            )
        self._json(
            {
                "images": [
                    f"/media/{source_name}/{external_id}/{index}"
                    for index in range(cached_count)
                ],
                "cached_count": cached_count,
                "total_count": (
                    len(refreshed.image_urls) if refreshed else 0
                ),
                "complete": remaining_count == 0,
                "stalled": stalled,
                "details_refreshed": gallery_refreshed,
                "downloaded_count": downloaded,
                "remaining_count": remaining_count,
                "description": refreshed.description if refreshed else None,
                "mileage_km": refreshed.mileage_km if refreshed else None,
                "views_count": refreshed.views_count if refreshed else None,
                "published_at": (
                    refreshed.published_at
                    if refreshed and refreshed.published_at
                    else stored_row["first_seen_at"] if stored_row else None
                ),
                "published_at_inferred": bool(
                    refreshed
                    and not refreshed.published_at
                    and stored_row
                ),
                "attributes": refreshed.attributes if refreshed else {},
                "year": refreshed_year,
                "trim_exact": bool(exact_trim),
                "trim_options": trim_options,
                "analysis_trim_name": analysis_trim,
                "drive2_url": _drive2_url(
                    refreshed.brand if refreshed else None,
                    refreshed.model if refreshed else None,
                    refreshed_year,
                ),
                "vehicle_analysis": vehicle_analysis,
                "listing_assessment": listing_assessment,
                "warning": warning,
            }
        )

    def _media(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 4:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        _, source, external_id, index_text = parts
        index = _integer(index_text)
        if index is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with _open_database(self.database) as connection:
            row = connection.execute(
                """
                SELECT local_path FROM listing_images
                WHERE source = ? AND external_id = ?
                  AND local_path IS NOT NULL
                ORDER BY rowid
                LIMIT 1 OFFSET ?
                """,
                (source, external_id, index),
            ).fetchone()
        if not row:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        allowed_root = self.cache_dir.resolve()
        stored_path = str(row["local_path"])
        filename = stored_path.replace("\\", "/").rsplit("/", 1)[-1]
        candidates = [allowed_root / external_id / filename]
        stored_candidate = Path(stored_path)
        if not stored_candidate.is_absolute():
            stored_candidate = Path.cwd() / stored_candidate
        candidates.append(stored_candidate.resolve())
        candidate = None
        for possible in candidates:
            resolved = possible.resolve()
            try:
                resolved.relative_to(allowed_root)
            except ValueError:
                continue
            if resolved.is_file():
                candidate = resolved
                break
        if candidate is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "image/jpeg"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (WEB_ROOT / relative).resolve()
        try:
            candidate.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "text/plain"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(
    database: Path,
    *,
    cache_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> None:
    if not database.is_file():
        raise FileNotFoundError(f"База объявлений не найдена: {database}")
    with ListingRepository(database):
        pass
    handler = type(
        "ConfiguredViewerHandler",
        (ViewerHandler,),
        {"database": database.resolve(), "cache_dir": cache_dir.resolve()},
    )
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Панель объявлений: http://{host}:{port}")
    print("Для остановки нажмите Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Локальная панель объявлений")
    parser.add_argument("--database", type=Path, default=Path("listings.db"))
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache/auto_parser/images"),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--scheduler",
        dest="scheduler",
        action="store_true",
        default=True,
    )
    parser.add_argument("--no-scheduler", dest="scheduler", action="store_false")
    parser.add_argument("--validation-interval", type=int, default=60)
    args = parser.parse_args(argv)
    scheduler = None
    try:
        if args.scheduler:
            from auto_parser.scheduler import BackgroundScheduler

            # Complete schema migrations before concurrent workers open
            # their own SQLite connections.
            with ListingRepository(args.database):
                pass
            scheduler = BackgroundScheduler(
                database=args.database,
                cache_dir=args.cache_dir,
                validation_interval_minutes=args.validation_interval,
            )
            scheduler.start()
        serve(
            args.database,
            cache_dir=args.cache_dir,
            host=args.host,
            port=args.port,
        )
    finally:
        if scheduler:
            scheduler.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

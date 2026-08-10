from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote_plus

from auto_parser.parts_api import PART_RECOMMENDATIONS, PART_TERMS
from auto_parser.sqlite_support import configure_sqlite_connection
from auto_parser.spare_parts import listing_spare_parts_payload
from auto_parser.storage import ListingRepository


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _integer(value: Any, *, minimum: int = 0) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return None


@contextmanager
def _database(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    configure_sqlite_connection(connection)
    try:
        yield connection
    finally:
        connection.close()


def _attributes(value: str | None) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _offer_for_plan(
    name: str,
    category: str,
    offers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    ignored = {"для", "после", "комплект", "элементы"}
    wanted = {
        token for token in re.findall(r"[a-zа-яё0-9]+", name.casefold())
        if len(token) > 3 and token not in ignored
    }
    ranked = []
    for offer in offers:
        haystack = " ".join(
            str(offer.get(key) or "")
            for key in ("name", "description", "category", "subcategory")
        ).casefold()
        score = sum(token in haystack for token in wanted)
        if category.casefold() in haystack:
            score += 2
        if score:
            ranked.append((score, offer.get("price") is not None, -(offer.get("price") or 10**12), offer))
    return max(ranked, default=(0, False, 0, None), key=lambda item: item[:3])[3]


class GarageApiMixin:
    database: Path
    cache_dir: Path

    def _garage_get_route(self, path: str) -> bool:
        parts = path.strip("/").split("/")
        if path == "/api/garage":
            self._garage_list()
            return True
        if len(parts) == 3 and parts[:2] == ["api", "garage"]:
            self._garage_detail(_integer(parts[2], minimum=1))
            return True
        if (
            len(parts) == 4
            and parts[:2] == ["api", "garage"]
            and parts[3] == "photo"
        ):
            self._garage_photo(_integer(parts[2], minimum=1))
            return True
        return False

    def _garage_post_route(self, path: str) -> bool:
        parts = path.strip("/").split("/")
        if path == "/api/garage":
            self._garage_create()
            return True
        if len(parts) == 4 and parts[:2] == ["api", "garage"]:
            garage_id = _integer(parts[2], minimum=1)
            if parts[3] == "entries":
                self._garage_create_entry(garage_id)
                return True
            if parts[3] == "parts":
                self._garage_create_part(garage_id)
                return True
            if parts[3] == "photo":
                self._garage_upload_photo(garage_id)
                return True
        if (
            len(parts) == 5
            and parts[:2] == ["api", "garage"]
            and parts[3:] == ["parts", "seed"]
        ):
            self._garage_seed_parts(_integer(parts[2], minimum=1))
            return True
        return False

    def _garage_patch_route(self, path: str) -> bool:
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "garage"]:
            self._garage_update(_integer(parts[2], minimum=1))
            return True
        return False

    def _garage_delete_route(self, path: str) -> bool:
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "garage"]:
            self._garage_delete(_integer(parts[2], minimum=1))
            return True
        if len(parts) == 4 and parts[:2] == ["api", "garage"]:
            if parts[2] == "entries":
                self._garage_delete_entry(_integer(parts[3], minimum=1))
                return True
        return False

    def _garage_list(self) -> None:
        with _database(self.database) as connection:
            rows = connection.execute(
                """
                SELECT g.*,
                    COALESCE((SELECT SUM(cost) FROM garage_entries e
                              WHERE e.garage_id = g.id), 0) AS spent_total,
                    COALESCE((SELECT SUM(
                        COALESCE(p.price, 0) * MAX(1, p.quantity)
                        + COALESCE(p.labor_cost, 0)
                    ) FROM car_parts p
                    WHERE p.garage_id = g.id
                      AND p.selected_for_replacement = 1), 0) AS planned_total
                FROM garage_cars g
                ORDER BY g.updated_at DESC
                """
            ).fetchall()
        self._json({"items": [dict(row) for row in rows]})

    def _garage_detail(self, garage_id: int | None) -> None:
        if garage_id is None:
            self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
            return
        with _database(self.database) as connection:
            car = connection.execute(
                "SELECT * FROM garage_cars WHERE id = ?", (garage_id,)
            ).fetchone()
            if car is None:
                self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
                return
            entries = connection.execute(
                """
                SELECT * FROM garage_entries WHERE garage_id = ?
                ORDER BY occurred_at DESC, id DESC
                """,
                (garage_id,),
            ).fetchall()
            parts = connection.execute(
                """
                SELECT * FROM car_parts WHERE garage_id = ?
                ORDER BY selected_for_replacement DESC, replacement_term, name
                """,
                (garage_id,),
            ).fetchall()
            linked_image = None
            if car["listing_source"] and car["listing_external_id"]:
                linked_image = connection.execute(
                    """
                    SELECT 1 FROM listing_images
                    WHERE source = ? AND external_id = ?
                      AND local_path IS NOT NULL LIMIT 1
                    """,
                    (car["listing_source"], car["listing_external_id"]),
                ).fetchone()
        car_data = dict(car)
        car_data["attributes"] = _attributes(car_data.pop("attributes_json"))
        car_data["photo_url"] = (
            f"/api/garage/{garage_id}/photo?v={quote_plus(str(car['updated_at']))}"
            if car["photo_path"]
            else (
                f"/media/{car['listing_source']}/{car['listing_external_id']}/0"
                if linked_image
                else None
            )
        )
        vehicle_context = self._garage_vehicle_context(car_data)
        entry_items = [dict(row) for row in entries]
        part_items = [dict(row) for row in parts]
        selected_parts = [item for item in part_items if item["selected_for_replacement"]]
        parts_cost = sum(
            (item["price"] or 0) * max(1, item["quantity"])
            for item in selected_parts
        )
        planned_labor = sum(item["labor_cost"] or 0 for item in selected_parts)
        spent_by_category: dict[str, int] = defaultdict(int)
        for entry in entry_items:
            spent_by_category[entry["category"] or entry["entry_type"]] += entry["cost"] or 0
        spent_total = sum(entry["cost"] or 0 for entry in entry_items)
        service_total = sum(
            entry["cost"] or 0
            for entry in entry_items
            if entry["entry_type"] == "service"
        )
        unpriced = sum(item["price"] is None for item in selected_parts)
        self._json(
            {
                "car": car_data,
                "entries": entry_items,
                "parts": part_items,
                **vehicle_context,
                "analytics": {
                    "spent_total": spent_total,
                    "service_total": service_total,
                    "entries_count": len(entry_items),
                    "planned_parts_cost": parts_cost,
                    "planned_labor_cost": planned_labor,
                    "planned_total": parts_cost + planned_labor,
                    "ownership_total": (car["purchase_price"] or 0) + spent_total,
                    "future_total": (
                        (car["purchase_price"] or 0)
                        + spent_total + parts_cost + planned_labor
                    ),
                    "unpriced_parts": unpriced,
                    "spent_by_category": [
                        {"label": label, "value": value}
                        for label, value in sorted(
                            spent_by_category.items(),
                            key=lambda item: item[1],
                            reverse=True,
                        )
                    ],
                },
            }
        )

    def _garage_vehicle_context(self, car: dict[str, Any]) -> dict[str, Any]:
        attributes = car.get("attributes") or {}
        with ListingRepository(self.database) as repository:
            trim_name = None
            assessment = None
            if car.get("listing_source") and car.get("listing_external_id"):
                trim_name = repository.listing_trim_assignment(
                    car["listing_source"], car["listing_external_id"]
                )
                assessment = repository.listing_vehicle_assessment(
                    car["listing_source"], car["listing_external_id"]
                )
            analysis = repository.vehicle_analysis(
                car.get("brand"), car.get("model"), trim_name=trim_name
            )
            if analysis is None and trim_name:
                analysis = repository.vehicle_analysis(
                    car.get("brand"), car.get("model"), trim_name=None
                )
            spare_parts = listing_spare_parts_payload(
                repository,
                {
                    "brand": car.get("brand"),
                    "model": car.get("model"),
                    "year": car.get("year"),
                    "price": car.get("purchase_price"),
                    "attributes": attributes,
                    "vehicle_analysis": analysis,
                    "listing_assessment": assessment,
                },
            )
        return {
            "vehicle_analysis": analysis,
            "listing_assessment": assessment,
            "spare_parts": spare_parts,
        }

    def _garage_photo(self, garage_id: int | None) -> None:
        if garage_id is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with _database(self.database) as connection:
            row = connection.execute(
                "SELECT photo_path FROM garage_cars WHERE id = ?", (garage_id,)
            ).fetchone()
        if not row or not row["photo_path"]:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        candidate = (self.cache_dir / row["photo_path"]).resolve()
        try:
            candidate.relative_to(self.cache_dir.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "image/jpeg"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _garage_upload_photo(self, garage_id: int | None) -> None:
        if garage_id is None:
            self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
            return
        length = _integer(self.headers.get("Content-Length"))
        if length is None or length > 6 * 1024 * 1024:
            self._json({"error": "Фотография слишком большая"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            data_url = str(payload.get("data_url") or "")
            header, encoded = data_url.split(",", 1)
            mime = header.split(";", 1)[0].removeprefix("data:")
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]
            body = base64.b64decode(encoded, validate=True)
        except (
            AttributeError, binascii.Error, KeyError, TypeError, ValueError,
            json.JSONDecodeError,
        ):
            self._json({"error": "Поддерживаются JPG, PNG и WebP"}, HTTPStatus.BAD_REQUEST)
            return
        if not body or len(body) > 4 * 1024 * 1024:
            self._json({"error": "Фотография должна быть не больше 4 МБ"}, HTTPStatus.BAD_REQUEST)
            return
        with _database(self.database) as connection:
            exists = connection.execute(
                "SELECT 1 FROM garage_cars WHERE id = ?", (garage_id,)
            ).fetchone()
            if not exists:
                self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
                return
            relative_path = Path("garage") / str(garage_id) / f"cover{suffix}"
            destination = (self.cache_dir / relative_path).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            for previous in destination.parent.glob("cover.*"):
                if previous != destination:
                    previous.unlink(missing_ok=True)
            destination.write_bytes(body)
            connection.execute(
                "UPDATE garage_cars SET photo_path = ?, updated_at = ? WHERE id = ?",
                (relative_path.as_posix(), _now(), garage_id),
            )
            connection.commit()
        self._json({"photo_url": f"/api/garage/{garage_id}/photo"})

    def _garage_create(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        listing_source = str(payload.get("listing_source", "")).strip() or None
        listing_external_id = str(payload.get("listing_external_id", "")).strip() or None
        listing = None
        with _database(self.database) as connection:
            if listing_source and listing_external_id:
                listing = connection.execute(
                    "SELECT * FROM listings WHERE source = ? AND external_id = ?",
                    (listing_source, listing_external_id),
                ).fetchone()
                if listing is None:
                    self._json({"error": "Объявление не найдено"}, HTTPStatus.NOT_FOUND)
                    return
            attributes = _attributes(listing["attributes_json"] if listing else None)
            manual_attributes = {
                "Цвет": payload.get("color"),
                "Тип двигателя": payload.get("engine_type"),
                "Объём двигателя": payload.get("engine_volume"),
                "Мощность": payload.get("power"),
                "Коробка передач": payload.get("transmission"),
                "Привод": payload.get("drive"),
                "Тип кузова": payload.get("body"),
            }
            for attribute_name, value in manual_attributes.items():
                if value not in {None, ""}:
                    suffix = " л.с." if attribute_name == "Мощность" else ""
                    attributes[attribute_name] = f"{str(value).strip()}{suffix}"
            year_text = attributes.get("Год выпуска")
            year = _integer(payload.get("year") or year_text, minimum=1885)
            if year is not None:
                attributes["Год выпуска"] = str(year)
            name = str(
                payload.get("name")
                or (listing["title"] if listing else "")
            ).strip()
            if not name:
                self._json({"error": "Название автомобиля обязательно"}, HTTPStatus.BAD_REQUEST)
                return
            now = _now()
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO garage_cars (
                        listing_source, listing_external_id, name, brand, model,
                        year, mileage_km, vin, plate_number, purchase_date,
                        purchase_price, attributes_json, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        listing_source, listing_external_id, name,
                        str(payload.get("brand") or (listing["brand"] if listing else "")).strip() or None,
                        str(payload.get("model") or (listing["model"] if listing else "")).strip() or None,
                        year,
                        _integer(payload.get("mileage_km") or (listing["mileage_km"] if listing else None)),
                        str(payload.get("vin", "")).strip() or None,
                        str(payload.get("plate_number", "")).strip() or None,
                        str(payload.get("purchase_date", "")).strip() or None,
                        _integer(payload.get("purchase_price") or (listing["price"] if listing else None)),
                        json.dumps(attributes, ensure_ascii=False),
                        str(payload.get("notes", "")).strip() or None,
                        now, now,
                    ),
                )
                garage_id = cursor.lastrowid
                if listing_source and listing_external_id:
                    connection.execute(
                        """
                        UPDATE car_parts SET garage_id = ?
                        WHERE car_source = ? AND car_external_id = ?
                          AND garage_id IS NULL
                        """,
                        (garage_id, listing_source, listing_external_id),
                    )
                connection.commit()
            except sqlite3.IntegrityError:
                self._json({"error": "Этот автомобиль уже находится в гараже"}, HTTPStatus.CONFLICT)
                return
        self._json({"id": garage_id}, HTTPStatus.CREATED)

    def _garage_update(self, garage_id: int | None) -> None:
        if garage_id is None:
            self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
            return
        payload = self._read_json()
        if payload is None:
            return
        allowed = {
            "name", "brand", "model", "year", "mileage_km", "vin",
            "plate_number", "purchase_date", "purchase_price", "notes",
        }
        updates = []
        values: list[Any] = []
        attribute_inputs = {
            "color": "Цвет",
            "engine_type": "Тип двигателя",
            "engine_volume": "Объём двигателя",
            "power": "Мощность",
            "transmission": "Коробка передач",
            "drive": "Привод",
            "body": "Тип кузова",
        }
        for key, value in payload.items():
            if key not in allowed:
                continue
            if key in {"year", "mileage_km", "purchase_price"}:
                value = _integer(value)
            else:
                value = str(value).strip() or None
            updates.append(f"{key} = ?")
            values.append(value)
        if any(key in payload for key in attribute_inputs):
            with _database(self.database) as connection:
                row = connection.execute(
                    "SELECT attributes_json FROM garage_cars WHERE id = ?",
                    (garage_id,),
                ).fetchone()
            attributes = _attributes(row["attributes_json"] if row else None)
            for key, label in attribute_inputs.items():
                if key not in payload:
                    continue
                value = str(payload.get(key) or "").strip()
                if value:
                    attributes[label] = (
                        f"{value} л.с."
                        if key == "power" and "л.с" not in value
                        else value
                    )
                else:
                    attributes.pop(label, None)
            updates.append("attributes_json = ?")
            values.append(json.dumps(attributes, ensure_ascii=False))
        if not updates:
            self._json({"updated": False})
            return
        updates.append("updated_at = ?")
        values.extend((_now(), garage_id))
        with _database(self.database) as connection:
            cursor = connection.execute(
                f"UPDATE garage_cars SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            connection.commit()
        self._json({"updated": cursor.rowcount > 0})

    def _garage_delete(self, garage_id: int | None) -> None:
        if garage_id is None:
            self._json({"deleted": False})
            return
        with _database(self.database) as connection:
            car = connection.execute(
                "SELECT photo_path FROM garage_cars WHERE id = ?", (garage_id,)
            ).fetchone()
            connection.execute("DELETE FROM garage_entries WHERE garage_id = ?", (garage_id,))
            connection.execute("DELETE FROM car_parts WHERE garage_id = ?", (garage_id,))
            cursor = connection.execute("DELETE FROM garage_cars WHERE id = ?", (garage_id,))
            connection.commit()
        if car and car["photo_path"]:
            candidate = (self.cache_dir / car["photo_path"]).resolve()
            try:
                candidate.relative_to(self.cache_dir.resolve())
                candidate.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        self._json({"deleted": cursor.rowcount > 0})

    def _garage_create_entry(self, garage_id: int | None) -> None:
        if garage_id is None:
            self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
            return
        payload = self._read_json()
        if payload is None:
            return
        title = str(payload.get("title", "")).strip()
        entry_type = str(payload.get("entry_type", "journal")).strip()
        if entry_type not in {"journal", "service", "expense"}:
            entry_type = "journal"
        if not title:
            self._json({"error": "Заголовок записи обязателен"}, HTTPStatus.BAD_REQUEST)
            return
        occurred_at = str(payload.get("occurred_at", "")).strip() or _now()
        with _database(self.database) as connection:
            exists = connection.execute("SELECT 1 FROM garage_cars WHERE id = ?", (garage_id,)).fetchone()
            if not exists:
                self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
                return
            cursor = connection.execute(
                """
                INSERT INTO garage_entries (
                    garage_id, entry_type, title, description, category,
                    occurred_at, mileage_km, cost, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    garage_id, entry_type, title,
                    str(payload.get("description", "")).strip() or None,
                    str(payload.get("category", "")).strip() or None,
                    occurred_at, _integer(payload.get("mileage_km")),
                    _integer(payload.get("cost")) or 0, _now(),
                ),
            )
            mileage = _integer(payload.get("mileage_km"))
            if mileage is not None:
                connection.execute(
                    "UPDATE garage_cars SET mileage_km = MAX(COALESCE(mileage_km, 0), ?), updated_at = ? WHERE id = ?",
                    (mileage, _now(), garage_id),
                )
            connection.commit()
        self._json({"id": cursor.lastrowid}, HTTPStatus.CREATED)

    def _garage_delete_entry(self, entry_id: int | None) -> None:
        if entry_id is None:
            self._json({"deleted": False})
            return
        with _database(self.database) as connection:
            cursor = connection.execute("DELETE FROM garage_entries WHERE id = ?", (entry_id,))
            connection.commit()
        self._json({"deleted": cursor.rowcount > 0})

    def _garage_create_part(self, garage_id: int | None) -> None:
        if garage_id is None:
            self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
            return
        payload = self._read_json()
        if payload is None:
            return
        name = str(payload.get("name", "")).strip()
        if not name:
            self._json({"error": "Название детали обязательно"}, HTTPStatus.BAD_REQUEST)
            return
        term = str(payload.get("replacement_term", "Позже")).strip()
        if term not in PART_TERMS:
            term = "Позже"
        url = str(payload.get("purchase_url", "")).strip()
        if url and not url.startswith(("http://", "https://")):
            self._json({"error": "Некорректная ссылка продавца"}, HTTPStatus.BAD_REQUEST)
            return
        now = _now()
        with _database(self.database) as connection:
            car = connection.execute("SELECT id FROM garage_cars WHERE id = ?", (garage_id,)).fetchone()
            if not car:
                self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
                return
            cursor = connection.execute(
                """
                INSERT INTO car_parts (
                    garage_id, car_source, car_external_id, name, category,
                    part_number, price, quantity, labor_cost, seller,
                    purchase_url, description, replacement_term,
                    selected_for_replacement, estimated, created_at, updated_at
                ) VALUES (?, 'garage', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    garage_id, str(garage_id), name,
                    str(payload.get("category", "Прочее")).strip() or "Прочее",
                    str(payload.get("part_number", "")).strip() or None,
                    _integer(payload.get("price")),
                    _integer(payload.get("quantity"), minimum=1) or 1,
                    _integer(payload.get("labor_cost")) or 0,
                    str(payload.get("seller", "")).strip() or None,
                    url or None,
                    str(payload.get("description", "")).strip() or None,
                    term, 1 if payload.get("selected_for_replacement") else 0,
                    now, now,
                ),
            )
            connection.commit()
        self._json({"id": cursor.lastrowid}, HTTPStatus.CREATED)

    def _garage_seed_parts(self, garage_id: int | None) -> None:
        if garage_id is None:
            self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
            return
        with _database(self.database) as connection:
            row = connection.execute(
                "SELECT * FROM garage_cars WHERE id = ?", (garage_id,)
            ).fetchone()
        if not row:
            self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
            return
        car = dict(row)
        car["attributes"] = _attributes(car.pop("attributes_json"))
        context = self._garage_vehicle_context(car)
        compatible_offers = context["spare_parts"].get("compatible_offers") or []
        mileage = car.get("mileage_km") or 0
        car_query = " ".join(
            value for value in (car.get("brand"), car.get("model")) if value
        ) or car["name"]
        created = 0
        updated = 0
        analysis_created = 0
        now = _now()
        with _database(self.database) as connection:
            for name, category, estimate, labor, term, threshold in PART_RECOMMENDATIONS:
                selected = threshold == 0 or threshold > 0 and mileage >= threshold
                offer = _offer_for_plan(name, category, compatible_offers)
                price = offer.get("price") if offer and offer.get("price") is not None else estimate
                seller = offer.get("seller") if offer else "Оценка AutoScope"
                purchase_url = (
                    offer.get("source_url") if offer else
                    "https://www.avito.ru/rossiya/zapchasti_i_aksessuary?q="
                    + quote_plus(f"{car_query} {name}")
                )
                description = (
                    "Цена из найденного предложения. Проверьте совместимость перед покупкой."
                    if offer else
                    "Ориентировочная стоимость — актуальных предложений пока нет."
                )
                existing = connection.execute(
                    """
                    SELECT id, selected_for_replacement, price, seller,
                           purchase_url, description, estimated
                    FROM car_parts
                    WHERE garage_id = ? AND name = ? LIMIT 1
                    """,
                    (garage_id, name),
                ).fetchone()
                if existing:
                    if not offer:
                        price = existing["price"] if existing["price"] is not None else price
                        seller = existing["seller"] or seller
                        purchase_url = existing["purchase_url"] or purchase_url
                        description = existing["description"] or description
                    connection.execute(
                        """
                        UPDATE car_parts SET price = ?, labor_cost = ?, seller = ?,
                            purchase_url = ?, description = ?, replacement_term = ?,
                            selected_for_replacement = ?, estimated = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            price, labor, seller, purchase_url, description, term,
                            existing["selected_for_replacement"],
                            int(not bool(offer) and bool(existing["estimated"])),
                            now, existing["id"],
                        ),
                    )
                    updated += 1
                else:
                    connection.execute(
                        """
                        INSERT INTO car_parts (
                            garage_id, car_source, car_external_id, name, category,
                            price, quantity, labor_cost, seller, purchase_url,
                            description, replacement_term, selected_for_replacement,
                            estimated, created_at, updated_at
                        ) VALUES (?, 'garage', ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            garage_id, str(garage_id), name, category, price, labor,
                            seller, purchase_url, description, term, int(selected),
                            int(not bool(offer)), now, now,
                        ),
                    )
                    created += 1

            analysis = (context.get("vehicle_analysis") or {}).get("data") or {}
            matched_by_point = {
                match.get("weak_point_id"): match
                for match in context["spare_parts"].get("matches") or []
            }
            for point in analysis.get("weak_points") or []:
                point_match = matched_by_point.get(point.get("id"), {})
                matched_offers = point_match.get("offers") or []
                for index, repair_part in enumerate(point.get("repair_parts") or []):
                    part_name = str(repair_part.get("name") or "").strip()
                    if not part_name:
                        continue
                    candidates = [
                        offer for offer in matched_offers
                        if offer.get("matched_repair_part") == part_name
                    ]
                    offer = min(
                        (item for item in candidates if item.get("price") is not None),
                        key=lambda item: item["price"],
                        default=(candidates[0] if candidates else None),
                    )
                    price = (
                        offer.get("price") if offer and offer.get("price") is not None
                        else point.get("parts_cost_min")
                    )
                    existing = connection.execute(
                        "SELECT id FROM car_parts WHERE garage_id = ? AND name = ? LIMIT 1",
                        (garage_id, part_name),
                    ).fetchone()
                    values = (
                        str(point.get("fault_type") or "По анализу ChatGPT").title(),
                        price,
                        int(point.get("labor_cost_min") or 0) if index == 0 else 0,
                        offer.get("seller") if offer else "Анализ ChatGPT",
                        offer.get("source_url") if offer else None,
                        " — ".join(filter(None, (point.get("system"), point.get("issue")))),
                        "Сразу" if point.get("priority") == "high" else "До 6 месяцев",
                        int(not bool(offer)),
                    )
                    if existing:
                        connection.execute(
                            """
                            UPDATE car_parts SET category = ?, price = ?, labor_cost = ?,
                                seller = ?, purchase_url = ?, description = ?,
                                replacement_term = ?, selected_for_replacement = 1,
                                estimated = ?, updated_at = ? WHERE id = ?
                            """,
                            (*values, now, existing["id"]),
                        )
                        updated += 1
                    else:
                        connection.execute(
                            """
                            INSERT INTO car_parts (
                                garage_id, car_source, car_external_id, name, category,
                                price, quantity, labor_cost, seller, purchase_url,
                                description, replacement_term, selected_for_replacement,
                                estimated, created_at, updated_at
                            ) VALUES (?, 'garage', ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                            """,
                            (garage_id, str(garage_id), part_name, *values, now, now),
                        )
                        created += 1
                        analysis_created += 1
            connection.commit()
        self._json(
            {"created": created, "updated": updated, "analysis_created": analysis_created}
        )

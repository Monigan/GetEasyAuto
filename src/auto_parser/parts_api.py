from __future__ import annotations

import sqlite3
import statistics
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote_plus

from auto_parser.models import utc_now_iso


PART_TERMS = (
    "Сразу",
    "До 3 месяцев",
    "До 6 месяцев",
    "До 12 месяцев",
    "Позже",
)
PART_RECOMMENDATIONS = (
    ("Моторное масло и масляный фильтр", "ТО", 7000, 1800, "Сразу", 0),
    ("Воздушный и салонный фильтры", "ТО", 4500, 1200, "До 3 месяцев", 0),
    ("Передние тормозные колодки", "Тормоза", 12000, 4500, "До 6 месяцев", 60000),
    ("Масло и фильтр коробки передач", "Трансмиссия", 16000, 7000, "До 12 месяцев", 60000),
    ("Комплект ГРМ", "Двигатель", 35000, 18000, "До 12 месяцев", 100000),
    ("Элементы подвески после диагностики", "Подвеска", 40000, 20000, "До 12 месяцев", 120000),
    ("Аккумулятор", "Электрика", 15000, 1000, "Позже", -1),
)


def _integer(value: Any, *, minimum: int = 0) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return None


@contextmanager
def _database(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


class PartsApiMixin:
    database: Path

    def _parts_cars(self) -> None:
        with _database(self.database) as connection:
            rows = connection.execute(
                """
                SELECT source, external_id, title, price, mileage_km,
                       brand, model, location
                FROM listings
                WHERE status = 'active'
                ORDER BY hidden, last_seen_at DESC
                """
            ).fetchall()
        self._json({"items": [dict(row) for row in rows]})

    def _parts(self, query: dict[str, list[str]]) -> None:
        source = query.get("source", [""])[0].strip()
        external_id = query.get("external_id", [""])[0].strip()
        if not source or not external_id:
            self._json({"error": "Выберите автомобиль"}, HTTPStatus.BAD_REQUEST)
            return
        with _database(self.database) as connection:
            car = connection.execute(
                """
                SELECT source, external_id, title, price, mileage_km,
                       brand, model, location
                FROM listings WHERE source = ? AND external_id = ?
                """,
                (source, external_id),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT * FROM car_parts
                WHERE car_source = ? AND car_external_id = ?
                ORDER BY selected_for_replacement DESC, replacement_term, category, name
                """,
                (source, external_id),
            ).fetchall()
        if car is None:
            self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
            return
        items = [dict(row) for row in rows]
        selected = [item for item in items if item["selected_for_replacement"]]
        parts_cost = sum((item["price"] or 0) * max(1, item["quantity"]) for item in selected)
        labor_cost = sum(item["labor_cost"] or 0 for item in selected)
        category_totals: dict[str, int] = {}
        term_totals: dict[str, int] = {}
        for item in selected:
            total = (item["price"] or 0) * max(1, item["quantity"]) + (item["labor_cost"] or 0)
            category_totals[item["category"]] = category_totals.get(item["category"], 0) + total
            term_totals[item["replacement_term"]] = term_totals.get(item["replacement_term"], 0) + total
        known_prices = [item["price"] for item in items if item["price"] is not None]
        unpriced = sum(item["price"] is None for item in selected)
        planned_total = parts_cost + labor_cost
        search_query = quote_plus(" ".join(filter(None, (car["brand"], car["model"]))) or car["title"])
        self._json(
            {
                "car": dict(car),
                "items": items,
                "analytics": {
                    "offers_count": len(items),
                    "planned_count": len(selected),
                    "planned_parts_cost": parts_cost,
                    "planned_labor_cost": labor_cost,
                    "planned_total": planned_total,
                    "unpriced_planned_count": unpriced,
                    "price_coverage_percent": round((len(selected) - unpriced) / len(selected) * 100) if selected else 100,
                    "purchase_price": car["price"],
                    "total_entry_cost": (car["price"] or 0) + planned_total,
                    "investment_percent": round(planned_total / car["price"] * 100, 1) if car["price"] else None,
                    "median_part_price": round(statistics.median(known_prices)) if known_prices else None,
                    "category_totals": [
                        {"label": label, "value": value}
                        for label, value in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
                    ],
                    "term_totals": [{"label": label, "value": value} for label, value in term_totals.items()],
                },
                "market_links": {
                    "avito": f"https://www.avito.ru/rossiya/zapchasti_i_aksessuary?q={search_query}",
                    "drom": f"https://baza.drom.ru/sell_spare_parts/?query={search_query}",
                },
            }
        )

    def _create_part(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        source = str(payload.get("car_source", "")).strip()
        external_id = str(payload.get("car_external_id", "")).strip()
        name = str(payload.get("name", "")).strip()
        if not source or not external_id or not name:
            self._json({"error": "Автомобиль и название детали обязательны"}, HTTPStatus.BAD_REQUEST)
            return
        purchase_url = str(payload.get("purchase_url", "")).strip()
        if purchase_url and not purchase_url.startswith(("http://", "https://")):
            self._json({"error": "Некорректная ссылка продавца"}, HTTPStatus.BAD_REQUEST)
            return
        term = str(payload.get("replacement_term", "Позже")).strip()
        term = term if term in PART_TERMS else "Позже"
        now = utc_now_iso()
        try:
            with _database(self.database) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM listings WHERE source = ? AND external_id = ?",
                    (source, external_id),
                ).fetchone()
                if not exists:
                    self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
                    return
                cursor = connection.execute(
                    """
                    INSERT INTO car_parts (
                        car_source, car_external_id, name, category, part_number,
                        price, quantity, labor_cost, seller, purchase_url,
                        description, replacement_term, selected_for_replacement,
                        estimated, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        source, external_id, name,
                        str(payload.get("category", "Прочее")).strip() or "Прочее",
                        str(payload.get("part_number", "")).strip() or None,
                        _integer(payload.get("price")),
                        _integer(payload.get("quantity"), minimum=1) or 1,
                        _integer(payload.get("labor_cost")) or 0,
                        str(payload.get("seller", "")).strip() or None,
                        purchase_url or None,
                        str(payload.get("description", "")).strip() or None,
                        term, 1 if payload.get("selected_for_replacement") else 0,
                        now, now,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError:
            self._json({"error": "Такое предложение уже добавлено"}, HTTPStatus.CONFLICT)
            return
        self._json({"id": cursor.lastrowid}, HTTPStatus.CREATED)

    def _seed_parts(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        source = str(payload.get("car_source", "")).strip()
        external_id = str(payload.get("car_external_id", "")).strip()
        with _database(self.database) as connection:
            car = connection.execute(
                "SELECT title, brand, model, mileage_km FROM listings WHERE source = ? AND external_id = ?",
                (source, external_id),
            ).fetchone()
            if car is None:
                self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
                return
            mileage = car["mileage_km"] or 0
            query = " ".join(filter(None, (car["brand"], car["model"]))) or car["title"]
            created = 0
            now = utc_now_iso()
            for name, category, price, labor, term, threshold in PART_RECOMMENDATIONS:
                selected = threshold == 0 or threshold > 0 and mileage >= threshold
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO car_parts (
                        car_source, car_external_id, name, category, price,
                        quantity, labor_cost, seller, purchase_url, description,
                        replacement_term, selected_for_replacement, estimated,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        source, external_id, name, category, price, labor,
                        "Оценка AutoScope",
                        "https://www.avito.ru/rossiya/zapchasti_i_aksessuary?q=" + quote_plus(f"{query} {name}"),
                        "Ориентировочная стоимость. Проверьте совместимость и актуальную цену у продавца.",
                        term, 1 if selected else 0, now, now,
                    ),
                )
                created += cursor.rowcount
            connection.commit()
        self._json({"created": created})

    def _update_part(self, part_id: int | None) -> None:
        if part_id is None:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        payload = self._read_json()
        if payload is None:
            return
        allowed = {
            "name", "category", "part_number", "price", "quantity",
            "labor_cost", "seller", "purchase_url", "description",
            "replacement_term", "selected_for_replacement",
        }
        updates: list[str] = []
        values: list[Any] = []
        for key, value in payload.items():
            if key not in allowed:
                continue
            if key in {"price", "labor_cost"}:
                value = _integer(value) if value not in {None, ""} else None
            elif key == "quantity":
                value = _integer(value, minimum=1) or 1
            elif key == "selected_for_replacement":
                value = 1 if value else 0
            elif key == "replacement_term":
                value = str(value).strip()
                value = value if value in PART_TERMS else "Позже"
            elif key == "purchase_url":
                value = str(value).strip() or None
                if value and not value.startswith(("http://", "https://")):
                    self._json({"error": "Некорректная ссылка продавца"}, HTTPStatus.BAD_REQUEST)
                    return
            elif value is not None:
                value = str(value).strip() or None
            updates.append(f"{key} = ?")
            values.append(value)
        if not updates:
            self._json({"updated": False})
            return
        updates.append("updated_at = ?")
        values.extend((utc_now_iso(), part_id))
        with _database(self.database) as connection:
            cursor = connection.execute(
                f"UPDATE car_parts SET {', '.join(updates)} WHERE id = ?", values
            )
            connection.commit()
        self._json({"updated": cursor.rowcount > 0})

    def _delete_part(self, part_id: int | None) -> None:
        if part_id is None:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        with _database(self.database) as connection:
            cursor = connection.execute("DELETE FROM car_parts WHERE id = ?", (part_id,))
            connection.commit()
        self._json({"deleted": cursor.rowcount > 0})

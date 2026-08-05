from __future__ import annotations

import json
import sqlite3
import statistics
import re
import time
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote_plus, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from auto_parser.models import utc_now_iso
from auto_parser.spare_parts import (
    SparePartOffer,
    build_drom_parts_url,
    drom_generation,
    generation_for_year,
    is_drom_verification_page,
    known_drom_generation,
    listing_spare_parts_payload,
    nested_categories_for_analysis,
    parse_drom_part_description,
    parse_drom_parts_index,
    parse_drom_parts_page,
)
from auto_parser.storage import ListingRepository, configure_sqlite_connection


DEFAULT_SPARE_PART_SOURCES = (
    "https://baza.drom.ru/sell_spare_parts/model/opel+vectra/?autoPartsGeneration=2",
    "https://baza.drom.ru/sell_spare_parts/model/ford+mondeo/?autoPartsGeneration=2",
    "https://baza.drom.ru/sell_spare_parts/model/ford+mondeo/?autoPartsFuel=diesel&autoPartsGeneration=5&autoPartsVolume=1600",
)
MAX_DROM_HTML_BYTES = 20 * 1024 * 1024
DROM_PART_CATEGORIES = (
    "Выхлопная система",
    "Двигатель и элементы двигателя",
    "Детали кузова",
    "Дополнительное оборудование",
    "Запчасти для ТО",
    "Интерьер",
    "Оптика",
    "Расходники и комплектующие",
    "Система отопления и кондиционирования",
    "Система подачи воздуха",
    "Топливная система",
    "Тормозная система",
    "Трансмиссия",
    "Ходовая часть",
    "Электрика",
)


class DromVerificationRequired(RuntimeError):
    def __init__(self, url: str) -> None:
        super().__init__(
            "Drom запросил ручную проверку «не робот». Откройте ссылку, "
            "пройдите проверку самостоятельно и повторите импорт позже."
        )
        self.url = url


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


def _drom_parts_page_url(url: str, page: int) -> str:
    """Return Drom's canonical path for a result page, preserving filters."""
    parsed = urlparse(url)
    path = re.sub(r"/page\d+/?$", "/", parsed.path)
    if page > 1:
        path = path.rstrip("/") + f"/page{page}/"
    return parsed._replace(path=path).geturl()


@contextmanager
def _database(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    configure_sqlite_connection(connection)
    try:
        yield connection
    finally:
        connection.close()


class PartsApiMixin:
    database: Path

    @staticmethod
    def _download_spare_parts_html(url: str) -> str:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"baza.drom.ru", "www.baza.drom.ru"}
        ):
            raise ValueError("Поддерживаются только HTTPS-ссылки на baza.drom.ru")
        request = Request(
            url,
            headers={
                "User-Agent": "AutoScope/0.1 (personal vehicle research)",
                "Accept-Language": "ru-RU,ru;q=0.9",
            },
        )
        with urlopen(request, timeout=20) as response:
            final_url = urlparse(response.geturl())
            if final_url.hostname not in {"baza.drom.ru", "www.baza.drom.ru"}:
                raise ValueError("Drom перенаправил запрос на неподдерживаемый сайт")
            content_length = response.headers.get("Content-Length")
            try:
                declared_size = int(content_length) if content_length else None
            except ValueError as error:
                raise ValueError("Некорректный размер HTML-ответа Drom") from error
            if declared_size is not None and declared_size > MAX_DROM_HTML_BYTES:
                raise ValueError("HTML-ответ Drom превышает лимит 20 МБ")
            raw = response.read(MAX_DROM_HTML_BYTES + 1)
            if len(raw) > MAX_DROM_HTML_BYTES:
                raise ValueError("HTML-ответ Drom превышает лимит 20 МБ")
            charset = response.headers.get_content_charset() or "utf-8"
        html = raw.decode(charset, errors="replace")
        if is_drom_verification_page(html, response.geturl()):
            raise DromVerificationRequired(url)
        return html

    def _enrich_spare_part_descriptions(
        self,
        offers: list[SparePartOffer],
        *,
        limit: int = 3,
    ) -> None:
        unique = {offer.external_id: offer for offer in offers}
        selected = list(unique.values())[:max(0, min(limit, 3))]
        for index, offer in enumerate(selected):
            try:
                html = self._download_spare_parts_html(offer.source_url)
                offer.description = parse_drom_part_description(html) or offer.description
            except DromVerificationRequired:
                raise
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                pass
            if index + 1 < len(selected):
                time.sleep(1.5)

    def _spare_parts_catalog(self, query: dict[str, list[str]]) -> None:
        brand = query.get("brand", [""])[0].strip() or None
        model = query.get("model", [""])[0].strip() or None
        category = query.get("category", [""])[0].strip() or None
        search = query.get("search", [""])[0].strip() or None
        generation = _integer(query.get("generation", [None])[0], minimum=1)
        min_price = _integer(query.get("min_price", [None])[0], minimum=0)
        max_price = _integer(query.get("max_price", [None])[0], minimum=0)
        priced_only = query.get("priced", [""])[0].casefold() in {"1", "true", "yes"}
        page = _integer(query.get("page", [1])[0], minimum=1) or 1
        limit = min(_integer(query.get("limit", [60])[0], minimum=1) or 60, 200)
        sort = query.get("sort", ["price_asc"])[0]
        filters = {
            "brand": brand,
            "model": model,
            "category": category,
            "search": search,
            "priced_only": priced_only,
            "generation": generation,
            "min_price": min_price,
            "max_price": max_price,
        }
        with ListingRepository(self.database) as repository:
            items = repository.spare_part_offers(
                **filters,
                sort=sort,
                limit=limit,
                offset=(page - 1) * limit,
            )
            summary = repository.spare_part_offer_summary(**filters)
            facets = repository.spare_part_facets()
            vehicles = [
                dict(row)
                for row in repository.connection.execute(
                    """
                    SELECT brand, model, COUNT(*) AS offers_count,
                           MIN(price) AS min_price, MAX(observed_at) AS observed_at
                    FROM spare_part_offers
                    GROUP BY brand_key, model_key
                    ORDER BY brand, model
                    """
                ).fetchall()
            ]
        self._json({
            "items": items,
            "vehicles": vehicles,
            "facets": facets,
            "sources": list(DEFAULT_SPARE_PART_SOURCES),
            "summary": {**summary, "models_count": len(vehicles)},
            "page": page,
            "page_size": limit,
            "pages": max(1, (int(summary["offers_count"] or 0) + limit - 1) // limit),
        })

    def _import_spare_parts(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        url = str(payload.get("url") or "").strip()
        requested_pages = min(_integer(payload.get("pages"), minimum=1) or 1, 10)
        offers_by_id: dict[str, SparePartOffer] = {}
        imported_pages = 0
        verification_url: str | None = None
        try:
            for page_number in range(1, requested_pages + 1):
                page_url = _drom_parts_page_url(url, page_number)
                html = self._download_spare_parts_html(page_url)
                page_offers = parse_drom_parts_page(html, page_url)
                if not page_offers:
                    break
                imported_pages += 1
                for offer in page_offers:
                    offers_by_id[offer.external_id] = offer
                if page_number < requested_pages:
                    time.sleep(1.5)
        except DromVerificationRequired as error:
            verification_url = error.url
            if not offers_by_id:
                self._json(
                    {
                        "error": str(error),
                        "verification_required": True,
                        "verification_url": error.url,
                    },
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
                return
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as error:
            self._json(
                {"error": f"Не удалось загрузить список Drom: {error}"},
                HTTPStatus.BAD_GATEWAY,
            )
            return
        offers = list(offers_by_id.values())
        if not offers:
            self._json(
                {"error": "Drom вернул страницу без распознаваемых предложений"},
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return

        if payload.get("load_descriptions", False):
            try:
                self._enrich_spare_part_descriptions(offers, limit=3)
            except DromVerificationRequired as error:
                verification_url = error.url
        observed_at = utc_now_iso()
        with ListingRepository(self.database) as repository:
            saved = repository.upsert_spare_part_offers(
                offers,
                source_list_url=url,
                observed_at=observed_at,
            )
        self._json({
            "imported": saved,
            "brand": offers[0].brand,
            "model": offers[0].model,
            "generation": offers[0].generation,
            "category": offers[0].category,
            "subcategory": offers[0].subcategory,
            "pages_imported": imported_pages,
            "verification_required": bool(verification_url),
            "verification_url": verification_url,
            "observed_at": observed_at,
        }, HTTPStatus.CREATED)

    def _import_spare_parts_html(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length < 1 or length > 50 * 1024 * 1024:
            self._json(
                {"error": "Общий размер страниц должен быть не больше 50 МБ"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "Не удалось прочитать открытую страницу"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(payload, dict):
            self._json({"error": "Некорректные данные страницы"}, HTTPStatus.BAD_REQUEST)
            return
        url = str(payload.get("url") or "").strip()
        captured_pages = payload.get("pages")
        if not isinstance(captured_pages, list):
            captured_pages = [{"url": url, "html": str(payload.get("html") or "")}]
        captured_pages = [page for page in captured_pages[:10] if isinstance(page, dict)]
        if any(
            is_drom_verification_page(
                str(page.get("html") or ""),
                str(page.get("url") or url),
            )
            for page in captured_pages
        ):
            self._json(
                {"error": "Сначала завершите ручную проверку Drom, затем снова нажмите кнопку импорта"},
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        try:
            offers_by_id: dict[str, SparePartOffer] = {}
            for page in captured_pages:
                page_url = str(page.get("url") or url)
                for offer in parse_drom_parts_page(
                    str(page.get("html") or ""), page_url,
                ):
                    offers_by_id[offer.external_id] = offer
            offers = list(offers_by_id.values())
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if not offers:
            self._json(
                {"error": "На открытой странице не найдены карточки запчастей"},
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
            return
        observed_at = utc_now_iso()
        with ListingRepository(self.database) as repository:
            saved = repository.upsert_spare_part_offers(
                offers,
                source_list_url=url,
                observed_at=observed_at,
            )
        self._json({
            "imported": saved,
            "brand": offers[0].brand,
            "model": offers[0].model,
            "generation": offers[0].generation,
            "category": offers[0].category,
            "subcategory": offers[0].subcategory,
            "pages_imported": len(captured_pages),
            "observed_at": observed_at,
        }, HTTPStatus.CREATED)

    def _import_spare_parts_for_car(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        source = str(payload.get("source") or "").strip()
        external_id = str(payload.get("external_id") or "").strip()
        with ListingRepository(self.database) as repository:
            car = repository.get_listing(source, external_id)
            assigned_trim = repository.listing_trim_assignment(source, external_id)
            vehicle_analysis = (
                repository.vehicle_analysis(
                    car.brand,
                    car.model,
                    trim_name=(car.attributes.get("Комплектация") or assigned_trim),
                )
                if car and car.brand and car.model
                else None
            )
        if car is None:
            self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
            return
        if not car.brand or not car.model:
            self._json(
                {"error": "В карточке не определены марка и модель"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        attributes = car.attributes or {}
        year_match = re.search(
            r"\b((?:19|20)\d{2})\b",
            str(attributes.get("Год выпуска") or car.title),
        )
        year = int(year_match.group(1)) if year_match else None
        fuel = attributes.get("Тип двигателя") or attributes.get("Топливо")
        volume = attributes.get("Объём двигателя") or attributes.get("Объем двигателя")
        generation = drom_generation(
            attributes.get("Поколение") or attributes.get("Generation")
        ) or known_drom_generation(car.brand, car.model, year)
        discovery_url = build_drom_parts_url(
            car.brand,
            car.model,
            fuel=fuel,
            engine_volume=volume,
        )
        categories = list(DROM_PART_CATEGORIES)
        warning: str | None = None
        try:
            discovery_html = self._download_spare_parts_html(discovery_url)
            index = parse_drom_parts_index(discovery_html)
            generation = generation or generation_for_year(index, year)
            categories = index.categories or categories
        except DromVerificationRequired as error:
            self._json(
                {
                    "error": str(error),
                    "verification_required": True,
                    "verification_url": error.url,
                },
                HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as error:
            warning = str(error)

        base_url = build_drom_parts_url(
            car.brand,
            car.model,
            fuel=fuel,
            generation=generation,
            engine_volume=volume,
        )
        nested_categories = nested_categories_for_analysis(
            (vehicle_analysis or {}).get("data")
        )
        category_names = [
            *nested_categories,
            *(category for category in categories if category not in nested_categories),
        ]
        category_links = [
            {
                "category": category,
                "url": build_drom_parts_url(
                    car.brand,
                    car.model,
                    category=category,
                    fuel=fuel,
                    generation=generation,
                    engine_volume=volume,
                ),
            }
            for category in category_names
        ]
        links = [{"category": "Все запчасти", "url": base_url}, *category_links]

        # Only directly relevant nested tags are fetched automatically. The
        # remaining links are returned to the UI for manual navigation. This
        # avoids the burst of dozens of requests that triggers Drom verification.
        fetch_links = category_links[:3] if nested_categories else []
        fetch_links.append(links[0])

        collected: list[tuple[dict[str, str], list[SparePartOffer]]] = []
        failed_categories = 0
        verification_url: str | None = None
        for index, link in enumerate(fetch_links):
            try:
                html = self._download_spare_parts_html(link["url"])
                offers = parse_drom_parts_page(html, link["url"])
            except DromVerificationRequired as error:
                verification_url = error.url
                failed_categories += 1
                break
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError):
                offers = []
                failed_categories += 1
            collected.append((link, offers))
            if index + 1 < len(fetch_links):
                time.sleep(1.5)

        unique_offers: dict[str, SparePartOffer] = {}
        for _, offers in collected:
            for offer in offers:
                unique_offers[offer.external_id] = offer
        observed_at = utc_now_iso()
        with ListingRepository(self.database) as repository:
            repository.upsert_spare_part_offers(
                unique_offers.values(),
                source_list_url=base_url,
                observed_at=observed_at,
            )
            refreshed = repository.get_listing(source, external_id)
            if refreshed is None:
                self._json({"error": "Автомобиль не найден"}, HTTPStatus.NOT_FOUND)
                return
            item = {
                "brand": refreshed.brand,
                "model": refreshed.model,
                "year": year,
                "price": refreshed.price,
                "attributes": refreshed.attributes,
                "vehicle_analysis": repository.vehicle_analysis(
                    refreshed.brand,
                    refreshed.model,
                    trim_name=(
                        refreshed.attributes.get("Комплектация")
                        or repository.listing_trim_assignment(source, external_id)
                    ),
                ),
                "listing_assessment": repository.listing_vehicle_assessment(
                    source,
                    external_id,
                ),
            }
            spare_parts = listing_spare_parts_payload(repository, item)
        successful_categories = [
            {"category": link["category"], "url": link["url"], "offers": len(offers)}
            for link, offers in collected
            if offers
        ]
        self._json({
            "imported": len(unique_offers),
            "generation": generation,
            "year": year,
            "links": links,
            "successful_categories": successful_categories,
            "failed_categories": failed_categories,
            "warning": warning,
            "verification_required": bool(verification_url),
            "verification_url": verification_url,
            "spare_parts": spare_parts,
            "observed_at": observed_at,
        }, HTTPStatus.CREATED)

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

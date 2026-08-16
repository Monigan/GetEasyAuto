from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from auto_parser.models import Listing
from auto_parser.parts_api import DromVerificationRequired
from auto_parser.spare_parts import (
    SparePartOffer,
    build_drom_parts_url,
    drom_parts_url_with_filters,
    generation_for_year,
    is_drom_verification_page,
    known_drom_generation,
    listing_spare_parts_payload,
    nested_categories_for_analysis,
    normalize_listing_assessment,
    normalize_vehicle_analysis_parts,
    offer_matches_repair_part,
    parse_drom_part_description,
    parse_drom_parts_index,
    parse_drom_parts_page,
)
from auto_parser.storage import ListingRepository
from auto_parser.viewer import ViewerHandler


SEARCH_HTML = """
<html><body>
<h1>Запчасти Ford Mondeo 5-ое поколение</h1>
<p>Объявления о продаже запчастей Форд Мондео 5 2014 – 2019.</p>
<div data-bulletin-id="17868339054" class="bull-item bull-item_inline">
  <div class="bull-item__content-wrapper">
    <div class="bull-item__cell bull-item__image-cell">
      <img src="https://static.baza.drom.ru/thermostat_block" alt="Термостат фото">
    </div>
    <div class="descriptionCell bull-item__description-cell">
      <div class="price-block__price" data-role="price">4 900 ₽</div>
      <span class="bull-delivery__city">Рязань</span>
      <a class="bulletinLink bull-item__self-link" href="/ryazan/sell_spare_parts/termostat-g17868339054.html">Термостат Ford</a>
      <div class="searchSnippet">Ford Mondeo 5 поколение, дизель 1.6</div>
      <div class="ellipsis-text__left-side">Магазин деталей</div>
    </div>
  </div>
</div>
</body></html>
"""


class SparePartsTest(unittest.TestCase):
    def test_listing_statuses_are_the_single_source_for_legacy_filters(self) -> None:
        assessment = normalize_listing_assessment({
            "weak_point_statuses": [
                {"weak_point_id": " pump ", "status": "confirmed", "evidence": " Течь "},
                {"weak_point_id": "thermostat", "status": "reported_fixed", "evidence": "Заменён"},
                {"weak_point_id": "unknown", "status": "invented"},
            ],
            "excluded_weak_point_ids": ["pump"],
            "relevant_weak_point_ids": ["thermostat"],
        })

        self.assertEqual(assessment["relevant_weak_point_ids"], ["pump"])
        self.assertEqual(assessment["excluded_weak_point_ids"], ["thermostat"])
        self.assertEqual(len(assessment["weak_point_statuses"]), 2)
        self.assertEqual(
            assessment["weak_point_statuses"][0]["evidence"],
            "Течь",
        )

    def test_car_import_reports_drom_verification_to_the_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "test.db"
            cache = root / "images"
            cache.mkdir()
            with ListingRepository(database) as repository:
                repository.upsert_many([
                    Listing(
                        source="avito",
                        external_id="verification-car",
                        url="https://www.avito.ru/verification-car",
                        title="Ford Mondeo, 2016",
                        brand="Ford",
                        model="Mondeo",
                        attributes={"Год выпуска": "2016"},
                    )
                ])
            handler = type(
                "DromVerificationViewerHandler",
                (ViewerHandler,),
                {"database": database, "cache_dir": cache, "_is_authenticated": lambda self: True},
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            verification_url = "https://baza.drom.ru/verify?u=parts"
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/spare-parts/import-for-car",
                data=json.dumps({
                    "source": "avito",
                    "external_id": "verification-car",
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with patch.object(
                    handler,
                    "_download_spare_parts_html",
                    side_effect=DromVerificationRequired(verification_url),
                ):
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(request, timeout=5)
                    payload = json.load(raised.exception)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(raised.exception.code, 429)
        self.assertTrue(payload["verification_required"])
        self.assertEqual(payload["verification_url"], verification_url)

    def test_links_cooling_fault_to_parts_without_matching_heater_actuator(self) -> None:
        analysis = normalize_vehicle_analysis_parts({"weak_points": [{
            "id": "coolant-leaks",
            "system": "Система охлаждения",
            "issue": "Течи из-за водяного насоса, термостата и патрубков",
            "replacement_parts": ["водяная помпа", "термостат", "патрубки охлаждения"],
        }]})
        parts = analysis["weak_points"][0]["repair_parts"]
        actuator = {
            "category": "Система отопления и кондиционирования",
            "subcategory": "заслонки печки",
            "name": "Сервопривод заслонок печки BMW 7-Series 6911819 E65",
            "description": "Привод климатической установки",
        }
        pump = {
            "category": "Двигатель и элементы двигателя",
            "subcategory": "система охлаждения",
            "name": "Водяная помпа BMW 7-Series E65 N62",
            "description": "Насос охлаждающей жидкости",
        }

        self.assertEqual(parts[0]["part_type"], "water_pump")
        self.assertTrue(offer_matches_repair_part(pump, parts[0]))
        self.assertFalse(any(offer_matches_repair_part(actuator, part) for part in parts))

    def test_builds_automatic_tag_links_and_discovers_generation(self) -> None:
        index = parse_drom_parts_index("""
        <a href="/sell_spare_parts/model/ford+mondeo/?autoPartsGeneration=4">4 поколение 2007 – 2015</a>
        <a href="/sell_spare_parts/model/ford+mondeo/?autoPartsGeneration=5">5 поколение 2014 – 2019</a>
        <a href="/sell_spare_parts/+/интерьер/model/ford+mondeo/">Интерьер</a>
        <a href="/sell_spare_parts/+/оптика/model/ford+mondeo/">Оптика</a>
        <a href="/sell_spare_parts/+/детали+кузова/model/ford+mondeo/">Детали кузова</a>
        """)

        self.assertEqual(index.categories, ["Интерьер", "Оптика", "Детали кузова"])
        self.assertEqual(
            index.category_links[1]["url"],
            "https://baza.drom.ru/sell_spare_parts/+/оптика/model/ford+mondeo/",
        )
        self.assertEqual(generation_for_year(index, 2014), 5)
        self.assertEqual(known_drom_generation("Ford", "Mondeo", 2016), 5)
        self.assertEqual(
            build_drom_parts_url(
                "Ford",
                "Mondeo",
                category="Детали кузова",
                fuel="Дизель",
                generation=5,
                engine_volume="1.6 л",
            ),
            "https://baza.drom.ru/sell_spare_parts/+/%E4%E5%F2%E0%EB%E8+%EA%F3%E7%EE%E2%E0/model/ford+mondeo/?autoPartsFuel=diesel&autoPartsGeneration=5&autoPartsVolume=1600",
        )

    def test_preserves_nested_category_from_drom_url(self) -> None:
        url = (
            "https://baza.drom.ru/sell_spare_parts/+/оптика+задний+фонарь/"
            "model/bmw+5-series/?autoPartsFuel=gasoline&"
            "autoPartsGeneration=4&autoPartsVolume=2500"
        )
        offers = parse_drom_parts_page(SEARCH_HTML, url)

        self.assertEqual(offers[0].category, "Оптика")
        self.assertEqual(offers[0].subcategory, "задний фонарь")
        self.assertEqual(
            nested_categories_for_analysis({"weak_points": [{
                "replacement_parts": ["задний фонарь"],
            }]}),
            ["задний фонарь"],
        )

    def test_preserves_discovered_drom_path_when_applying_vehicle_filters(self) -> None:
        self.assertEqual(
            drom_parts_url_with_filters(
                "https://baza.drom.ru/sell_spare_parts/+/оптика+задний+фонарь/model/bmw+5-series/?sort=price",
                fuel="Бензин",
                generation=4,
                engine_volume="2.5 л",
            ),
            "https://baza.drom.ru/sell_spare_parts/+/оптика+задний+фонарь/model/bmw+5-series/?sort=price&autoPartsFuel=gasoline&autoPartsGeneration=4&autoPartsVolume=2500",
        )

    def test_normalizes_structured_part_tags_without_guessing_parent_category(self) -> None:
        analysis = normalize_vehicle_analysis_parts({"weak_points": [{
            "system": "Система смазки",
            "replacement_parts": ["маслянный фильтр"],
        }, {
            "system": "Система охлаждения",
            "replacement_parts": ["водяная помпа"],
        }]})

        oil_filter = analysis["weak_points"][0]["repair_parts"][0]
        water_pump = analysis["weak_points"][1]["repair_parts"][0]
        self.assertEqual(oil_filter["name"], "масляный фильтр")
        self.assertEqual(oil_filter["part_type"], "oil_filter")
        self.assertEqual(oil_filter["system_tag"], "система смазки")
        self.assertEqual(water_pump["system_tag"], "система охлаждения")
        self.assertEqual(
            nested_categories_for_analysis(analysis),
            ["масляный фильтр", "водяная помпа"],
        )

    def test_rejects_neighboring_radiator_parts(self) -> None:
        part = normalize_vehicle_analysis_parts({"weak_points": [{
            "system": "Система охлаждения",
            "replacement_parts": ["радиатор охлаждения"],
        }]})["weak_points"][0]["repair_parts"][0]
        engine_radiator = {"name": "Радиатор ДВС BMW E39", "description": "Основной радиатор охлаждения"}
        heater_radiator = {"name": "Радиатор отопителя салона BMW E39", "description": "Радиатор печки"}
        radiator_fan = {"name": "Вентилятор радиатора BMW E39", "description": "Система охлаждения"}

        self.assertTrue(offer_matches_repair_part(engine_radiator, part))
        self.assertFalse(offer_matches_repair_part(heater_radiator, part))
        self.assertFalse(offer_matches_repair_part(radiator_fan, part))

    def test_merges_new_safety_exclusions_into_saved_repair_part(self) -> None:
        saved = normalize_vehicle_analysis_parts({"weak_points": [{
            "system": "Система охлаждения",
            "repair_parts": [{
                "name": "радиатор охлаждения",
                "part_type": "radiator",
                "search_terms": ["радиатор"],
                "exclude_terms": ["интеркулер"],
            }],
        }]})["weak_points"][0]["repair_parts"][0]

        self.assertIn("интеркулер", saved["exclude_terms"])
        self.assertIn("радиатор отопителя", saved["exclude_terms"])
        self.assertIn("вентилятор радиатора", saved["exclude_terms"])

    def test_listing_ranks_relevance_before_price_and_rejects_other_bmw_model(self) -> None:
        def offer(external_id: str, name: str, price: int) -> SparePartOffer:
            return SparePartOffer(
                source="drom", external_id=external_id,
                source_url=f"https://baza.drom.ru/g{external_id}.html",
                brand="BMW", model="5-Series", generation=4,
                year_from=1995, year_to=2004, fuel="gasoline",
                engine_volume_cc=2500, category=None, subcategory=None,
                name=name, description=None, price=price, image_url=None,
                seller=None, location=None,
            )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_spare_part_offers(
                    [
                        offer("1", "Насос (помпа) электрический BMW X5 E53", 1000),
                        offer("2", "Водяная помпа BMW 5-Series E39", 5000),
                        offer("3", "Радиатор отопителя салона BMW E39", 1200),
                        offer("4", "Радиатор ДВС BMW 5-Series E39", 7000),
                    ],
                    source_list_url="https://baza.drom.ru/sell_spare_parts/model/bmw+5-series/",
                    observed_at="2026-08-12T10:00:00+00:00",
                )
                payload = listing_spare_parts_payload(repository, {
                    "brand": "BMW", "model": "5-Series", "year": 2001,
                    "price": 500_000,
                    "attributes": {"Поколение": "4", "Тип двигателя": "Бензин", "Объём двигателя": "2.5 л"},
                    "vehicle_analysis": {"data": {"weak_points": [{
                        "id": "cooling", "system": "Система охлаждения",
                        "replacement_parts": ["водяная помпа", "радиатор охлаждения"],
                    }]}},
                    "listing_assessment": {"data": {}},
                })

        names = [offer["name"] for offer in payload["matches"][0]["offers"]]
        self.assertEqual(names, ["Водяная помпа BMW 5-Series E39", "Радиатор ДВС BMW 5-Series E39"])
        self.assertEqual(payload["costs"]["parts"], 12_000)

    def test_detects_drom_manual_verification_page(self) -> None:
        self.assertTrue(is_drom_verification_page(
            "<h2>Вы не робот?</h2><p>Мы зарегистрировали подозрительный трафик</p>",
            "https://baza.drom.ru/verify?u=parts",
        ))
        self.assertFalse(is_drom_verification_page(SEARCH_HTML))

    def test_imports_html_captured_from_verified_browser_tab(self) -> None:
        url = (
            "https://baza.drom.ru/sell_spare_parts/+/оптика+задний+фонарь/"
            "model/bmw+5-series/?autoPartsGeneration=4"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "test.db"
            cache = root / "images"
            cache.mkdir()
            with ListingRepository(database):
                pass
            handler = type(
                "CapturedDromPageHandler",
                (ViewerHandler,),
                {"database": database, "cache_dir": cache, "_is_authenticated": lambda self: True},
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/spare-parts/import-html",
                data=json.dumps({
                    "url": url,
                    "html": SEARCH_HTML,
                    "pages": [
                        {"url": url, "html": SEARCH_HTML},
                        {
                            "url": url.replace("/?", "/page2/?"),
                            "html": SEARCH_HTML.replace(
                                "17868339054", "17868339055"
                            ).replace("Термостат Ford", "Помпа Ford"),
                        },
                    ],
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=5) as response:
                    payload = json.load(response)
                with ListingRepository(database) as repository:
                    saved = repository.spare_part_offers()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(payload["imported"], 2)
        self.assertEqual(payload["pages_imported"], 2)
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["category"], "Оптика")
        self.assertEqual(saved[0]["subcategory"], "задний фонарь")

    def test_parses_drom_model_filters_and_offer_fields(self) -> None:
        url = (
            "https://baza.drom.ru/sell_spare_parts/model/ford+mondeo/"
            "?autoPartsFuel=diesel&autoPartsGeneration=5&autoPartsVolume=1600"
        )
        offers = parse_drom_parts_page(SEARCH_HTML, url)

        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual((offer.brand, offer.model), ("ford", "mondeo"))
        self.assertEqual(offer.generation, 5)
        self.assertEqual((offer.year_from, offer.year_to), (2014, 2019))
        self.assertEqual(offer.fuel, "diesel")
        self.assertEqual(offer.engine_volume_cc, 1600)
        self.assertIsNone(offer.category)
        self.assertEqual(offer.name, "Термостат Ford")
        self.assertEqual(offer.price, 4900)
        self.assertEqual(offer.seller, "Магазин деталей")
        self.assertEqual(offer.location, "Рязань")
        self.assertEqual(offer.image_url, "https://static.baza.drom.ru/thermostat_block")

    def test_reads_description_from_product_page(self) -> None:
        html = '<meta name="description" content="Запасной термостат"><div data-ftid="bulletin-description"><div data-ftid="info-full">Новый, в наличии. Подходит для дизеля.</div></div>'
        self.assertEqual(
            parse_drom_part_description(html),
            "Новый, в наличии. Подходит для дизеля.",
        )

    def test_matches_offer_to_vehicle_and_weak_point_cost(self) -> None:
        url = (
            "https://baza.drom.ru/sell_spare_parts/model/ford+mondeo/"
            "?autoPartsFuel=diesel&autoPartsGeneration=5&autoPartsVolume=1600"
        )
        offers = parse_drom_parts_page(SEARCH_HTML, url)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many([Listing(
                    source="drom",
                    external_id="car-1",
                    url="https://auto.drom.ru/1.html",
                    title="Ford Mondeo, 2016",
                    price=1_000_000,
                    brand="Ford",
                    model="Mondeo",
                    attributes={
                        "Год выпуска": "2016",
                        "Тип двигателя": "Дизель",
                        "Объём двигателя": "1.6 л",
                    },
                )])
                repository.upsert_spare_part_offers(
                    offers,
                    source_list_url=url,
                    observed_at="2026-08-05T10:00:00+00:00",
                )
                item = {
                    "brand": "Ford", "model": "Mondeo", "year": 2016,
                    "price": 1_000_000,
                    "attributes": {
                        "Тип двигателя": "Дизель",
                        "Объём двигателя": "1.6 л",
                    },
                    "vehicle_analysis": {"data": {"weak_points": [{
                        "id": "cooling-thermostat",
                        "system": "Охлаждение",
                        "issue": "Отказ термостата",
                        "replacement_parts": ["термостат"],
                        "labor_cost_min": 2500,
                        "labor_cost_max": 4000,
                    }]}},
                    "listing_assessment": {"data": {}},
                }
                payload = listing_spare_parts_payload(repository, item)

        self.assertEqual(payload["matches"][0]["offers"][0]["name"], "Термостат Ford")
        self.assertEqual(payload["compatible_offers"][0]["name"], "Термостат Ford")
        self.assertEqual(payload["costs"]["parts"], 4900)
        self.assertEqual(payload["costs"]["service_min"], 7400)
        self.assertEqual(payload["costs"]["total_entry_max"], 1_008_900)
        self.assertEqual(
            payload["search_url"],
            "https://baza.drom.ru/sell_spare_parts/model/ford+mondeo/?autoPartsFuel=diesel&autoPartsGeneration=5&autoPartsVolume=1600",
        )

    def test_uses_catalog_ids_and_separates_current_cost_from_risk_reserve(self) -> None:
        def offer(external_id: str, name: str, price: int) -> SparePartOffer:
            return SparePartOffer(
                source="drom",
                external_id=external_id,
                source_url=f"https://baza.drom.ru/g{external_id}.html",
                brand="Ford",
                model="Mondeo",
                generation=5,
                year_from=2014,
                year_to=2019,
                fuel="diesel",
                engine_volume_cc=1600,
                category="Двигатель и элементы двигателя",
                subcategory="система охлаждения",
                name=name,
                description=None,
                price=price,
                image_url=None,
                seller="Магазин",
                location="Москва",
            )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_spare_part_offers(
                    [
                        offer("pump", "Водяная помпа Ford Mondeo", 8_000),
                        offer("thermostat", "Термостат Ford Mondeo", 5_000),
                    ],
                    source_list_url="https://baza.drom.ru/sell_spare_parts/model/ford+mondeo/",
                    observed_at="2026-08-16T10:00:00+00:00",
                )
                saved_offers = repository.spare_part_offers(
                    brand="Ford",
                    model="Mondeo",
                    sort="name",
                )
                offer_ids = {item["name"]: item["id"] for item in saved_offers}
                payload = listing_spare_parts_payload(
                    repository,
                    {
                        "brand": "Ford",
                        "model": "Mondeo",
                        "year": 2016,
                        "price": 1_000_000,
                        "attributes": {
                            "Поколение": "5",
                            "Тип двигателя": "Дизель",
                            "Объём двигателя": "1.6 л",
                        },
                        "vehicle_analysis": {"data": {"weak_points": [
                            {
                                "id": "pump",
                                "system": "Охлаждение",
                                "issue": "Течь помпы",
                                "repair_parts": [{
                                    "name": "Насос контура ОЖ",
                                    "catalog_offer_ids": [
                                        offer_ids["Водяная помпа Ford Mondeo"]
                                    ],
                                }],
                                "labor_cost_min": 3_000,
                                "labor_cost_max": 4_000,
                            },
                            {
                                "id": "thermostat",
                                "system": "Охлаждение",
                                "issue": "Термостат",
                                "repair_parts": [{
                                    "name": "Термостат",
                                    "catalog_offer_ids": [
                                        offer_ids["Термостат Ford Mondeo"]
                                    ],
                                }],
                                "labor_cost_min": 2_000,
                                "labor_cost_max": 3_000,
                            },
                        ]}},
                        "listing_assessment": {"data": {
                            "weak_point_statuses": [
                                {
                                    "weak_point_id": "pump",
                                    "status": "confirmed",
                                    "evidence": "Подтекает помпа",
                                },
                                {
                                    "weak_point_id": "thermostat",
                                    "status": "not_mentioned",
                                    "evidence": None,
                                },
                            ],
                            "investment_budget": {
                                "immediate": {
                                    "parts_min": 8_000,
                                    "parts_max": 8_000,
                                    "labor_min": 3_000,
                                    "labor_max": 4_000,
                                    "total_min": 11_000,
                                    "total_max": 12_000,
                                },
                                "preventive": {
                                    "parts_min": 2_000,
                                    "parts_max": 3_000,
                                    "labor_min": 1_000,
                                    "labor_max": 1_000,
                                    "total_min": 3_000,
                                    "total_max": 4_000,
                                },
                                "risk_reserve": {
                                    "parts_min": 4_000,
                                    "parts_max": 6_000,
                                    "labor_min": 2_000,
                                    "labor_max": 3_000,
                                    "total_min": 6_000,
                                    "total_max": 9_000,
                                },
                            },
                        }},
                    },
                    include_analysis_catalog=True,
                )

        pump_match = next(
            match for match in payload["matches"]
            if match["weak_point_id"] == "pump"
        )
        self.assertEqual(pump_match["offers"][0]["matched_by"], "catalog_offer_id")
        self.assertEqual(pump_match["status_evidence"], "Подтекает помпа")
        self.assertEqual(payload["costs"]["parts"], 8_000)
        self.assertEqual(payload["costs"]["service_min"], 11_000)
        self.assertEqual(payload["costs"]["preventive_min"], 3_000)
        self.assertEqual(payload["costs"]["investment_min"], 14_000)
        self.assertEqual(payload["costs"]["risk_reserve_min"], 6_000)
        self.assertEqual(payload["costs"]["risk_reserve_max"], 9_000)
        self.assertEqual(payload["analysis_catalog"]["total_offers"], 2)
        self.assertEqual(payload["analysis_catalog"]["priced_offers"], 2)

    def test_analysis_context_endpoint_returns_imported_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "test.db"
            cache = root / "images"
            cache.mkdir()
            with ListingRepository(database) as repository:
                repository.upsert_many([Listing(
                    source="avito",
                    external_id="analysis-car",
                    url="https://www.avito.ru/analysis-car",
                    title="Ford Mondeo, 2016",
                    price=1_000_000,
                    mileage_km=150_000,
                    brand="Ford",
                    model="Mondeo",
                    attributes={"Год выпуска": "2016", "Поколение": "5"},
                )])
                repository.upsert_spare_part_offers(
                    [SparePartOffer(
                        source="drom",
                        external_id="context-part",
                        source_url="https://baza.drom.ru/g1.html",
                        brand="Ford",
                        model="Mondeo",
                        generation=5,
                        year_from=2014,
                        year_to=2019,
                        fuel=None,
                        engine_volume_cc=None,
                        category="Запчасти для ТО",
                        subcategory="фильтры",
                        name="Масляный фильтр Ford Mondeo",
                        description="Новый фильтр",
                        price=900,
                        image_url=None,
                        seller=None,
                        location=None,
                    )],
                    source_list_url="https://baza.drom.ru/sell_spare_parts/model/ford+mondeo/",
                    observed_at="2026-08-16T10:00:00+00:00",
                )
            handler = type(
                "AnalysisContextViewerHandler",
                (ViewerHandler,),
                {"database": database, "cache_dir": cache, "_is_authenticated": lambda self: True},
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/vehicle-analysis/context?source=avito&external_id=analysis-car",
                    timeout=5,
                ) as response:
                    payload = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(payload["vehicle"]["year"], 2016)
        self.assertEqual(payload["vehicle"]["mileage_km"], 150_000)
        self.assertEqual(payload["parts_catalog"]["total_offers"], 1)
        self.assertEqual(
            payload["parts_catalog"]["items"][0]["name"],
            "Масляный фильтр Ford Mondeo",
        )


if __name__ == "__main__":
    unittest.main()

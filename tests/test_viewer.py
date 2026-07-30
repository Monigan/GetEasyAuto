import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from auto_parser.activity import (
    clear_listing_activity,
    set_listing_activity,
)
from auto_parser.models import Listing
from auto_parser.request_governor import (
    RequestDeferredError,
    RequestGovernor,
)
from auto_parser.storage import ListingRepository
from auto_parser.viewer import ViewerHandler


class FakeSearchService:
    cache_request_kinds: list[str | None] = []
    detail_request_kinds: list[str | None] = []

    def __init__(self, source: object, **_: object) -> None:
        self.source = source

    def cache_images(self, listings: list[Listing], **kwargs: object) -> int:
        self.cache_request_kinds.append(kwargs.get("request_kind"))
        listing = listings[0]
        image_url = listing.image_urls[0]
        cache_dir = Path(kwargs["cache_dir"])
        destination = cache_dir / listing.external_id / "lazy.jpg"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"lazy")
        listing.cached_images[image_url] = str(destination)
        return 1

    def enrich_listing_details(
        self,
        listing: Listing,
        *,
        request_kind: str = "detail",
    ) -> Listing:
        self.detail_request_kinds.append(request_kind)
        listing.description = "Описание загружено при открытии"
        listing.attributes = {"Цвет": "Красный"}
        return listing


class DeferredSearchService:
    def __init__(self, source: object, **_: object) -> None:
        self.source = source

    def cache_images(self, *_: object, **__: object) -> int:
        raise RequestDeferredError(
            datetime.now(timezone.utc) + timedelta(minutes=15),
            "image",
        )


class ViewerTests(unittest.TestCase):
    def test_listing_uses_drom_trim_fallback_date_and_saved_analysis(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "listings.db"
            cache = root / "images"
            cache.mkdir()
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="drom",
                            external_id="trim-source",
                            url="https://auto.drom.ru/bmw/trim-source.html",
                            title="BMW 3-Series, 1995",
                            brand="BMW",
                            model="3-Series",
                            attributes={
                                "Год выпуска": "1995",
                                "Комплектация": "316i MT",
                            },
                        ),
                        Listing(
                            source="avito",
                            external_id="target",
                            url="https://www.avito.ru/bmw-target",
                            title="BMW 3 серия, 1995",
                            brand="BMW",
                            model="3 серия",
                            description="Помпа заменена",
                            attributes={"Год выпуска": "1995"},
                            collected_at="2026-07-30T08:00:00+00:00",
                        ),
                    ]
                )
            handler = type(
                "TestVehicleKnowledgeViewerHandler",
                (ViewerHandler,),
                {"database": database, "cache_dir": cache},
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(
                    f"{base_url}/api/listings?source=avito",
                    timeout=5,
                ) as response:
                    before = json.load(response)["items"][0]
                save_request = Request(
                    f"{base_url}/api/vehicle-analysis",
                    data=json.dumps(
                        {
                            "source": "avito",
                            "external_id": "target",
                            "analysis": {
                                "model_analysis": {
                                    "summary": "Проверить охлаждение",
                                    "weak_points": [
                                        {
                                            "id": "cooling",
                                            "issue": "Помпа",
                                            "parts_cost_min": 10000,
                                            "parts_cost_max": 20000,
                                        }
                                    ],
                                },
                                "listing_assessment": {
                                    "confirmed_maintenance": [
                                        {
                                            "item": "Помпа",
                                            "evidence": "Помпа заменена",
                                        }
                                    ],
                                    "excluded_weak_point_ids": ["cooling"],
                                    "relevant_weak_point_ids": [],
                                    "parts_investment_total": {
                                        "min": 0,
                                        "max": 0,
                                    },
                                },
                            },
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(save_request, timeout=5) as response:
                    saved = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings?source=avito",
                    timeout=5,
                ) as response:
                    after = json.load(response)["items"][0]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertTrue(before["published_at_inferred"])
        self.assertEqual(
            before["published_at"],
            "2026-07-30T08:00:00+00:00",
        )
        self.assertFalse(before["trim_exact"])
        self.assertEqual(before["trim_options"][0]["name"], "316i MT")
        self.assertIn("drive2.ru/search", before["drive2_url"])
        self.assertTrue(saved["saved"])
        self.assertEqual(
            after["vehicle_analysis"]["data"]["summary"],
            "Проверить охлаждение",
        )
        self.assertEqual(
            after["listing_assessment"]["data"][
                "excluded_weak_point_ids"
            ],
            ["cooling"],
        )
        self.assertEqual(
            after["listing_assessment"]["description_snapshot"],
            "Помпа заменена",
        )
        self.assertEqual(
            after["vehicle_analysis"]["data"]["weak_points"][0][
                "parts_cost_min"
            ],
            10000,
        )

    def test_displayed_listings_endpoint_tracks_current_catalog_page(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "listings.db"
            cache = root / "images"
            cache.mkdir()
            with ListingRepository(database):
                pass
            handler = type(
                "TestDisplayedViewerHandler",
                (ViewerHandler,),
                {"database": database, "cache_dir": cache},
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            request = Request(
                (
                    f"http://127.0.0.1:{server.server_port}"
                    "/api/displayed-listings"
                ),
                data=json.dumps(
                    {
                        "client_id": "browser-test",
                        "items": [
                            {
                                "source": "drom",
                                "external_id": "visible-car",
                            }
                        ],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urlopen(request, timeout=5) as response:
                    result = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            with ListingRepository(database) as repository:
                tracked = repository.connection.execute(
                    """
                    SELECT source, external_id FROM displayed_listings
                    WHERE client_id = 'browser-test'
                    """
                ).fetchall()

        self.assertEqual(result["tracked"], 1)
        self.assertEqual(
            [(row["source"], row["external_id"]) for row in tracked],
            [("drom", "visible-car")],
        )

    def test_source_filter_price_changes_and_sold_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "listings.db"
            cache = root / "images"
            cache.mkdir()
            with ListingRepository(database) as repository:
                avito = Listing(
                    source="avito",
                    external_id="active",
                    url="https://www.avito.ru/item_active",
                    title="BMW 520i",
                    price=600_000,
                    brand="BMW",
                )
                repository.upsert_many([avito])
                avito.price = 550_000
                avito.collected_at = "2026-07-30T18:00:00+00:00"
                repository.upsert_many([avito])
                repository.upsert_many(
                    [
                        Listing(
                            source="drom",
                            external_id="sold",
                            url="https://auto.drom.ru/sold.html",
                            title="BMW 525i",
                            price=500_000,
                            brand="BMW",
                        )
                    ]
                )
                repository.mark_sold(
                    "drom",
                    "sold",
                    "2026-07-29T19:00:00+00:00",
                )
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id="hidden-by-source",
                            url="https://www.avito.ru/item_hidden",
                            title="BMW 528i",
                            price=580_000,
                            brand="BMW",
                            status="hidden",
                        )
                    ]
                )

            handler = type(
                "TestSoldViewerHandler",
                (ViewerHandler,),
                {"database": database, "cache_dir": cache},
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(f"{base_url}/api/listings?source=avito", timeout=5) as response:
                    active = json.load(response)
                with urlopen(f"{base_url}/api/listings", timeout=5) as response:
                    catalog = json.load(response)
                with urlopen(f"{base_url}/api/market?source=avito", timeout=5) as response:
                    market = json.load(response)
                with urlopen(f"{base_url}/api/sold?source=drom", timeout=5) as response:
                    sold = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings?visibility=source_hidden",
                    timeout=5,
                ) as response:
                    hidden_by_source = json.load(response)
                sale_request = Request(
                    f"{base_url}/api/listings/drom/sold/sale",
                    data=json.dumps({"sold_price": 470_000}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="PATCH",
                )
                with urlopen(sale_request, timeout=5) as response:
                    updated = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(active["total"], 1)
        self.assertEqual(catalog["total"], 1)
        self.assertEqual(market["price_changes"][0]["delta"], -50_000)
        self.assertEqual(sold["summary"]["count"], 1)
        self.assertEqual(sold["items"][0]["sold_price"], 500_000)
        self.assertEqual(hidden_by_source["total"], 1)
        self.assertEqual(
            hidden_by_source["items"][0]["external_id"],
            "hidden-by-source",
        )
        self.assertEqual(updated["sold_price"], 470_000)

    def test_client_disconnect_does_not_escape_request_handler(self) -> None:
        handler = object.__new__(ViewerHandler)
        handler.client_address = ("127.0.0.1", 12345)

        with patch.object(
            BaseHTTPRequestHandler,
            "handle",
            side_effect=ConnectionAbortedError,
        ):
            handler.handle()

    def test_api_filters_and_calculates_stats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "listings.db"
            cache = root / "images"
            cache.mkdir()
            listing_cache = cache / "1"
            listing_cache.mkdir()
            first_image = listing_cache / "first.jpg"
            first_image.write_bytes(b"first")
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id="1",
                            url="https://www.avito.ru/item_1",
                            title="Volvo XC90",
                            price=1_500_000,
                            mileage_km=180_000,
                            location="Тверь",
                            description="Полное описание",
                            brand="Volvo",
                            model="XC90",
                            views_count=500,
                            attributes={
                                "Цвет": "Синий",
                                "Тип двигателя": "Бензин",
                                "Коробка передач": "Автомат",
                                "Год выпуска": "2012",
                                "Мощность": "192 л.с.",
                            },
                            image_urls=[
                                "https://10.img.avito.st/image/first.jpg",
                                "https://10.img.avito.st/image/second.jpg",
                            ],
                            cached_images={
                                "https://10.img.avito.st/image/first.jpg":
                                    "/data/images/1/first.jpg",
                            },
                        ),
                        Listing(
                            source="avito",
                            external_id="2",
                            url="https://www.avito.ru/item_2",
                            title="BMW X5",
                            price=3_500_000,
                            mileage_km=90_000,
                            location="Москва",
                            attributes={
                                "Цвет": "Красный",
                                "Тип двигателя": "Дизель",
                                "Коробка передач": "Механика",
                                "Год выпуска": "2010",
                                "Мощность": "286 л.с.",
                            },
                        ),
                    ]
                )

            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """
                    UPDATE listings
                    SET first_seen_at = '2025-01-01T00:00:00+00:00',
                        last_seen_at = '2026-02-01T00:00:00+00:00'
                    WHERE external_id = '1'
                    """
                )
                connection.execute(
                    """
                    UPDATE listings
                    SET first_seen_at = '2026-03-01T00:00:00+00:00',
                        last_seen_at = '2026-01-01T00:00:00+00:00'
                    WHERE external_id = '2'
                    """
                )
                connection.commit()
            finally:
                connection.close()

            handler = type(
                "TestViewerHandler",
                (ViewerHandler,),
                {"database": database, "cache_dir": cache},
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            set_listing_activity(
                "avito",
                "1",
                "В фоне обновляются недостающие данные…",
                priority="incomplete",
            )
            FakeSearchService.cache_request_kinds.clear()
            FakeSearchService.detail_request_kinds.clear()
            try:
                with urlopen(
                    f"{base_url}/api/listings?max_price=2000000",
                    timeout=5,
                ) as response:
                    listings = json.load(response)
                with urlopen(
                    f"{base_url}/api/stats?max_price=2000000",
                    timeout=5,
                ) as response:
                    stats = json.load(response)
                with urlopen(
                    f"{base_url}/api/refresh-activity",
                    timeout=5,
                ) as response:
                    refresh_activity = json.load(response)
                with urlopen(
                    f"{base_url}/api/market?max_price=2000000",
                    timeout=5,
                ) as response:
                    market = json.load(response)
                with urlopen(
                    f"{base_url}/api/export-analysis?visibility=all",
                    timeout=5,
                ) as response:
                    export_disposition = response.headers.get(
                        "Content-Disposition"
                    )
                    analysis_export = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings"
                    "?source=avito&external_id=1&visibility=all",
                    timeout=5,
                ) as response:
                    exact_listing = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings"
                    "?brand=Volvo&model=XC90&visibility=all",
                    timeout=5,
                ) as response:
                    model_listing = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings"
                    "?sort=power_desc&visibility=all",
                    timeout=5,
                ) as response:
                    power_sorted = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings"
                    "?sort=recently_updated&visibility=all",
                    timeout=5,
                ) as response:
                    recently_updated = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings"
                    "?sort=recently_added&visibility=all",
                    timeout=5,
                ) as response:
                    recently_added = json.load(response)
                with urlopen(f"{base_url}/api/meta", timeout=5) as response:
                    meta = json.load(response)
                with urlopen(
                    f"{base_url}/api/parts/cars", timeout=5
                ) as response:
                    parts_cars = json.load(response)
                seed_parts_request = Request(
                    f"{base_url}/api/parts/seed",
                    data=json.dumps(
                        {"car_source": "avito", "car_external_id": "1"}
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(seed_parts_request, timeout=5) as response:
                    seeded_parts = json.load(response)
                with urlopen(
                    f"{base_url}/api/parts?source=avito&external_id=1",
                    timeout=5,
                ) as response:
                    parts_payload = json.load(response)
                create_part_request = Request(
                    f"{base_url}/api/parts",
                    data=json.dumps(
                        {
                            "car_source": "avito",
                            "car_external_id": "1",
                            "name": "Тормозные диски",
                            "category": "Тормоза",
                            "price": 18000,
                            "quantity": 1,
                            "labor_cost": 5000,
                            "seller": "Магазин",
                            "purchase_url": "https://example.com/discs",
                            "replacement_term": "До 6 месяцев",
                            "selected_for_replacement": True,
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(create_part_request, timeout=5) as response:
                    created_part = json.load(response)
                update_part_request = Request(
                    f"{base_url}/api/parts/{created_part['id']}",
                    data=json.dumps({"price": 17000}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="PATCH",
                )
                with urlopen(update_part_request, timeout=5) as response:
                    updated_part = json.load(response)
                with urlopen(
                    f"{base_url}/api/parts?source=avito&external_id=1",
                    timeout=5,
                ) as response:
                    parts_after_create = json.load(response)
                create_garage_request = Request(
                    f"{base_url}/api/garage",
                    data=json.dumps(
                        {
                            "listing_source": "avito",
                            "listing_external_id": "1",
                            "purchase_date": "2026-07-20",
                            "purchase_price": 1_450_000,
                            "vin": "YV1TESTGARAGE0001",
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(create_garage_request, timeout=5) as response:
                    created_garage = json.load(response)
                garage_id = created_garage["id"]
                create_entry_request = Request(
                    f"{base_url}/api/garage/{garage_id}/entries",
                    data=json.dumps(
                        {
                            "entry_type": "service",
                            "title": "Замена масла",
                            "description": "Плановое обслуживание",
                            "category": "ТО",
                            "occurred_at": "2026-07-21",
                            "mileage_km": 181000,
                            "cost": 12500,
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(create_entry_request, timeout=5) as response:
                    created_entry = json.load(response)
                create_garage_part_request = Request(
                    f"{base_url}/api/garage/{garage_id}/parts",
                    data=json.dumps(
                        {
                            "name": "Комплект свечей",
                            "category": "Двигатель",
                            "price": 8000,
                            "quantity": 1,
                            "labor_cost": 2500,
                            "replacement_term": "До 3 месяцев",
                            "selected_for_replacement": True,
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(create_garage_part_request, timeout=5) as response:
                    created_garage_part = json.load(response)
                with urlopen(f"{base_url}/api/garage", timeout=5) as response:
                    garage_list = json.load(response)
                with urlopen(
                    f"{base_url}/api/garage/{garage_id}", timeout=5
                ) as response:
                    garage_detail = json.load(response)
                create_manual_garage_request = Request(
                    f"{base_url}/api/garage",
                    data=json.dumps(
                        {
                            "name": "Проектный автомобиль",
                            "brand": "BMW",
                            "model": "E39",
                            "year": 2001,
                            "color": "Чёрный",
                            "engine_type": "Бензин",
                            "power": 231,
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(
                    create_manual_garage_request, timeout=5
                ) as response:
                    manual_garage = json.load(response)
                with urlopen(
                    f"{base_url}/api/garage/{manual_garage['id']}",
                    timeout=5,
                ) as response:
                    manual_garage_detail = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings"
                    "?source=avito&external_id=1&visibility=all",
                    timeout=5,
                ) as response:
                    listing_after_garage = json.load(response)
                multi_query = urlencode(
                    [
                        ("color", "Синий"),
                        ("color", "Красный"),
                    ]
                )
                with urlopen(
                    f"{base_url}/api/listings?{multi_query}",
                    timeout=5,
                ) as response:
                    multi_filtered = json.load(response)
                combined_query = urlencode(
                    {
                        "color": "Красный",
                        "fuel": "Дизель",
                    }
                )
                with urlopen(
                    f"{base_url}/api/listings?{combined_query}",
                    timeout=5,
                ) as response:
                    combined_filtered = json.load(response)
                with urlopen(
                    f"{base_url}/api/stats?{combined_query}",
                    timeout=5,
                ) as response:
                    combined_stats = json.load(response)
                create_request = Request(
                    f"{base_url}/api/search-profiles",
                    data=json.dumps(
                        {
                            "query": "Volvo XC90",
                            "region": "tver",
                            "radius": 200,
                            "interval_minutes": 60,
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(create_request, timeout=5) as response:
                    created = json.load(response)
                with ListingRepository(database) as repository:
                    profile_listing = repository.get_listing("avito", "1")
                    repository.remember_search_profile_listings(
                        created["id"],
                        [profile_listing],
                    )
                with urlopen(
                    f"{base_url}/api/listings"
                    f"?profile_id={created['id']}&visibility=all",
                    timeout=5,
                ) as response:
                    profile_filtered = json.load(response)
                all_cars_request = Request(
                    f"{base_url}/api/search-profiles",
                    data=json.dumps(
                        {
                            "source": "drom",
                            "all_cars": True,
                            "region": "moskva",
                            "radius": 100,
                            "min_price": 100000,
                            "max_price": 150000,
                            "interval_minutes": 30,
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(all_cars_request, timeout=5) as response:
                    all_cars_created = json.load(response)
                RequestGovernor(database).record_rate_limit(
                    retry_after_seconds=60
                )
                with urlopen(
                    f"{base_url}/api/search-profiles", timeout=5
                ) as response:
                    profiles = json.load(response)
                hide_request = Request(
                    f"{base_url}/api/listings/avito/1/visibility",
                    data=json.dumps({"hidden": True}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="PATCH",
                )
                with urlopen(hide_request, timeout=5) as response:
                    hidden_result = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings?max_price=2000000",
                    timeout=5,
                ) as response:
                    visible_after_hide = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings"
                    "?max_price=2000000&visibility=hidden",
                    timeout=5,
                ) as response:
                    hidden_after_hide = json.load(response)
                with urlopen(
                    f"{base_url}/api/stats?max_price=2000000",
                    timeout=5,
                ) as response:
                    stats_after_hide = json.load(response)
                with urlopen(
                    f"{base_url}/media/avito/1/0", timeout=5
                ) as response:
                    first_photo_body = response.read()
                gallery_request = Request(
                    f"{base_url}/api/listings/avito/1/images",
                    data=b"",
                    method="POST",
                )
                with patch(
                    "auto_parser.viewer.SearchService",
                    DeferredSearchService,
                ):
                    with urlopen(gallery_request, timeout=5) as response:
                        deferred_gallery = json.load(response)
                with patch(
                    "auto_parser.viewer.SearchService",
                    FakeSearchService,
                ):
                    with urlopen(gallery_request, timeout=5) as response:
                        gallery = json.load(response)
                    priority_request = Request(
                        f"{base_url}/api/listings/avito/2/images?prepare=1",
                        data=b"",
                        method="POST",
                    )
                    with urlopen(priority_request, timeout=5) as response:
                        priority_gallery = json.load(response)
            finally:
                clear_listing_activity("avito", "1")
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(listings["total"], 1)
            self.assertEqual(listings["items"][0]["title"], "Volvo XC90")
            self.assertTrue(listings["items"][0]["image_pending"])
            self.assertEqual(
                listings["items"][0]["update_stage"],
                "В фоне обновляются недостающие данные…",
            )
            self.assertEqual(
                listings["items"][0]["update_priority"],
                "incomplete",
            )
            self.assertEqual(
                listings["items"][0]["update_state"],
                "running",
            )
            self.assertEqual(refresh_activity["running_count"], 1)
            self.assertEqual(
                refresh_activity["items"][0]["title"],
                "Volvo XC90",
            )
            self.assertEqual(stats["count"], 1)
            self.assertEqual(stats["median_price"], 1_500_000)
            self.assertEqual(stats["median_mileage"], 180_000)
            self.assertEqual(market["summary"]["count"], 1)
            self.assertEqual(market["summary"]["median_price"], 1_500_000)
            self.assertEqual(
                analysis_export["summary"]["listing_count"],
                2,
            )
            self.assertIn("analysis_prompt", analysis_export)
            self.assertIn(
                "description_quality_score",
                analysis_export["listings"][0],
            )
            self.assertIn("attachment", export_disposition)
            self.assertIn("median_listing_age_days", market["summary"])
            self.assertIn("median_views", market["summary"])
            self.assertEqual(market["brands"][0]["label"], "Volvo")
            self.assertIn("composition", market)
            self.assertIn("scatter", market)
            self.assertIn("min_value", market["price_histogram"][0])
            self.assertIn("max_value", market["price_histogram"][0])
            self.assertEqual(market["scatter"][0]["source"], "avito")
            self.assertEqual(market["scatter"][0]["external_id"], "1")
            self.assertEqual(exact_listing["total"], 1)
            self.assertEqual(model_listing["total"], 1)
            self.assertEqual(power_sorted["items"][0]["title"], "BMW X5")
            self.assertEqual(power_sorted["items"][1]["title"], "Volvo XC90")
            self.assertEqual(
                recently_updated["items"][0]["title"],
                "Volvo XC90",
            )
            self.assertEqual(
                recently_added["items"][0]["title"],
                "BMW X5",
            )
            self.assertIn("Volvo", meta["brands"])
            self.assertEqual(parts_cars["items"][0]["title"], "Volvo XC90")
            self.assertGreater(seeded_parts["created"], 0)
            self.assertGreater(parts_payload["analytics"]["offers_count"], 0)
            self.assertIn("planned_total", parts_payload["analytics"])
            self.assertIn(
                "price_coverage_percent",
                parts_payload["analytics"],
            )
            self.assertIn("total_entry_cost", parts_payload["analytics"])
            self.assertTrue(updated_part["updated"])
            self.assertTrue(
                any(
                    item["name"] == "Тормозные диски"
                    and item["price"] == 17000
                    for item in parts_after_create["items"]
                )
            )
            self.assertGreater(created_garage["id"], 0)
            self.assertGreater(created_entry["id"], 0)
            self.assertGreater(created_garage_part["id"], 0)
            self.assertEqual(garage_list["items"][0]["name"], "Volvo XC90")
            self.assertEqual(garage_detail["car"]["vin"], "YV1TESTGARAGE0001")
            self.assertEqual(garage_detail["car"]["mileage_km"], 181000)
            self.assertEqual(garage_detail["analytics"]["spent_total"], 12500)
            self.assertEqual(garage_detail["analytics"]["service_total"], 12500)
            self.assertGreaterEqual(garage_detail["analytics"]["planned_total"], 10500)
            self.assertTrue(
                any(
                    item["name"] == "Комплект свечей"
                    for item in garage_detail["parts"]
                )
            )
            self.assertEqual(
                manual_garage_detail["car"]["attributes"]["Цвет"],
                "Чёрный",
            )
            self.assertEqual(
                manual_garage_detail["car"]["attributes"]["Мощность"],
                "231 л.с.",
            )
            self.assertEqual(
                listing_after_garage["items"][0]["garage_id"],
                garage_id,
            )
            self.assertIn("Синий", meta["attribute_options"]["color"])
            self.assertIn("Дизель", meta["attribute_options"]["fuel"])
            self.assertEqual(multi_filtered["total"], 2)
            self.assertEqual(combined_filtered["total"], 1)
            self.assertEqual(
                combined_filtered["items"][0]["title"],
                "BMW X5",
            )
            self.assertEqual(combined_stats["count"], 1)
            self.assertGreater(created["id"], 0)
            self.assertEqual(profile_filtered["total"], 1)
            self.assertEqual(
                profile_filtered["items"][0]["external_id"],
                "1",
            )
            self.assertGreater(all_cars_created["id"], 0)
            self.assertEqual(profiles["items"][0]["query"], "Volvo XC90")
            self.assertTrue(
                any(
                    item["source"] == "drom"
                    and item["query"] == "__all_cars__"
                    and item["region"] == "moskva"
                    and item["radius"] == 100
                    and item["min_price"] == 100000
                    and item["max_price"] == 150000
                    and item["interval_minutes"] == 30
                    for item in profiles["items"]
                )
            )
            self.assertEqual(
                profiles["items"][0]["waiting_reason"],
                "Ожидание снятия ограничения Avito",
            )
            self.assertIn("waiting_until", profiles["items"][0])
            self.assertTrue(hidden_result["hidden"])
            self.assertEqual(visible_after_hide["total"], 0)
            self.assertEqual(hidden_after_hide["total"], 1)
            self.assertTrue(hidden_after_hide["items"][0]["hidden"])
            self.assertEqual(stats_after_hide["count"], 1)
            self.assertEqual(gallery["cached_count"], 2)
            self.assertEqual(len(gallery["images"]), 2)
            self.assertTrue(gallery["complete"])
            self.assertFalse(gallery["stalled"])
            self.assertEqual(gallery["downloaded_count"], 1)
            self.assertEqual(gallery["remaining_count"], 0)
            self.assertIn(
                "interactive_image",
                FakeSearchService.cache_request_kinds,
            )
            self.assertTrue(priority_gallery["details_refreshed"])
            self.assertEqual(
                priority_gallery["description"],
                "Описание загружено при открытии",
            )
            self.assertIn(
                "interactive_detail",
                FakeSearchService.detail_request_kinds,
            )
            self.assertEqual(gallery["description"], "Полное описание")
            self.assertEqual(first_photo_body, b"first")
            self.assertEqual(deferred_gallery["cached_count"], 1)
            self.assertTrue(deferred_gallery["warning"])


if __name__ == "__main__":
    unittest.main()

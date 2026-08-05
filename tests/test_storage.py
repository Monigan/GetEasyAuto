import tempfile
import unittest
import sqlite3
from pathlib import Path

from auto_parser.models import Listing
from auto_parser.storage import ListingRepository


class ListingRepositoryTests(unittest.TestCase):
    def test_search_profile_remembers_only_its_listings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            listing = Listing(
                source="drom",
                external_id="profile-match",
                url="https://auto.drom.ru/profile-match.html",
                title="BMW 5-Series",
            )
            with ListingRepository(database) as repository:
                profile_id = repository.add_search_profile(
                    source="drom",
                    query="BMW 5-Series",
                    region="tver",
                    radius=200,
                    interval_minutes=60,
                    created_at="2026-07-30T12:00:00+00:00",
                )
                repository.upsert_many([listing])
                remembered = repository.remember_search_profile_listings(
                    profile_id,
                    [listing],
                )
                row = repository.connection.execute(
                    "SELECT * FROM search_profile_listings WHERE profile_id = ?",
                    (profile_id,),
                ).fetchone()
                repository.delete_search_profile(profile_id)
                remaining = repository.connection.execute(
                    "SELECT COUNT(*) FROM search_profile_listings"
                ).fetchone()[0]

        self.assertEqual(remembered, 1)
        self.assertEqual(row["external_id"], "profile-match")
        self.assertEqual(remaining, 0)

    def test_hidden_source_status_is_not_counted_as_sold_and_is_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                listing = Listing(
                    source="avito",
                    external_id="hidden-by-source",
                    url="https://www.avito.ru/item_hidden",
                    title="BMW 5 серия",
                    price=500_000,
                    collected_at="2026-07-29T12:00:00+00:00",
                    status="hidden",
                    last_validated_at="2026-07-29T18:00:00+00:00",
                )
                repository.upsert_many([listing])
                stored = repository.get_listing("avito", "hidden-by-source")
                due = repository.list_for_validation(
                    due_before="2026-07-30T18:00:00+00:00"
                )

        self.assertEqual(stored.status, "hidden")
        self.assertIsNone(stored.sold_price)
        self.assertIsNone(stored.sold_at)
        self.assertEqual(
            [item.external_id for item in due],
            ["hidden-by-source"],
        )

    def test_sold_listing_keeps_final_price_and_relisting_resets_sale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                listing = Listing(
                    source="avito",
                    external_id="sold-car",
                    url="https://www.avito.ru/item_sold",
                    title="BMW 5 серия",
                    price=500_000,
                    collected_at="2026-07-29T12:00:00+00:00",
                )
                repository.upsert_many([listing])
                repository.mark_sold(
                    "avito",
                    "sold-car",
                    "2026-07-29T18:00:00+00:00",
                )
                repository.set_sold_price("avito", "sold-car", 480_000)
                sold = repository.get_listing("avito", "sold-car")

                listing.price = 490_000
                listing.status = "active"
                listing.collected_at = "2026-07-30T18:00:00+00:00"
                repository.upsert_many([listing])
                relisted = repository.get_listing("avito", "sold-car")
                listing.price = 500_000
                listing.collected_at = "2026-07-31T18:00:00+00:00"
                repository.upsert_many([listing])
                history = repository.connection.execute(
                    """
                    SELECT price FROM price_history
                    WHERE source = 'avito' AND external_id = 'sold-car'
                    ORDER BY observed_at
                    """
                ).fetchall()

        self.assertEqual(sold.status, "sold")
        self.assertEqual(sold.sold_price, 480_000)
        self.assertEqual(sold.sold_at, "2026-07-29T18:00:00+00:00")
        self.assertEqual(relisted.status, "active")
        self.assertIsNone(relisted.sold_price)
        self.assertIsNone(relisted.sold_at)
        self.assertEqual([row["price"] for row in history], [500_000, 490_000, 500_000])

    def test_detail_refresh_prioritizes_incomplete_due_listing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id="incomplete",
                            url="https://www.avito.ru/item_incomplete",
                            title="Incomplete",
                        ),
                        Listing(
                            source="avito",
                            external_id="complete",
                            url="https://www.avito.ru/item_complete",
                            title="Complete",
                            description="Полное описание",
                            attributes={"Цвет": "Белый"},
                            image_urls=[
                                "https://10.img.avito.st/image/car.jpg"
                            ],
                        ),
                    ]
                )
                repository.mark_detail_attempted(
                    "avito",
                    "incomplete",
                    "2026-07-25T08:00:00+00:00",
                )
                repository.mark_detail_attempted(
                    "avito",
                    "complete",
                    "2026-07-26T08:00:00+00:00",
                )
                selected = repository.list_for_detail_refresh(
                    "avito",
                    incomplete_due_before="2026-07-25T09:00:00+00:00",
                    stale_due_before="2026-07-25T00:00:00+00:00",
                )

        self.assertEqual(
            [listing.external_id for listing in selected],
            ["incomplete"],
        )

    def test_validation_returns_only_due_listings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id="old",
                            url="https://www.avito.ru/item_old",
                            title="Old",
                            last_validated_at="2026-07-25T08:00:00+00:00",
                        ),
                        Listing(
                            source="avito",
                            external_id="fresh",
                            url="https://www.avito.ru/item_fresh",
                            title="Fresh",
                            last_validated_at="2026-07-25T12:00:00+00:00",
                        ),
                    ]
                )
                due = repository.list_for_validation(
                    due_before="2026-07-25T10:00:00+00:00",
                )

        self.assertEqual(
            [listing.external_id for listing in due],
            ["old"],
        )

    def test_displayed_listing_has_detail_refresh_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="drom",
                            external_id="background",
                            url="https://auto.drom.ru/background.html",
                            title="Background",
                        ),
                        Listing(
                            source="drom",
                            external_id="displayed",
                            url="https://auto.drom.ru/displayed.html",
                            title="Displayed",
                        ),
                    ]
                )
                repository.mark_detail_attempted(
                    "drom",
                    "background",
                    "2026-07-20T08:00:00+00:00",
                )
                repository.mark_detail_attempted(
                    "drom",
                    "displayed",
                    "2026-07-21T08:00:00+00:00",
                )
                repository.set_displayed_listings(
                    "browser-test",
                    [("drom", "displayed")],
                    seen_at="2026-07-30T08:00:00+00:00",
                )
                selected = repository.list_for_detail_refresh(
                    "drom",
                    incomplete_due_before="2026-07-29T08:00:00+00:00",
                    stale_due_before="2026-07-29T08:00:00+00:00",
                    displayed_since="2026-07-30T07:59:00+00:00",
                )
                validation = repository.list_for_validation(
                    source="drom",
                    displayed_since="2026-07-30T07:59:00+00:00",
                )

        self.assertEqual(selected[0].external_id, "displayed")
        self.assertEqual(validation[0].external_id, "displayed")

    def test_drom_trim_is_reused_for_matching_model_and_year(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="drom",
                            external_id="drom-trim",
                            url="https://auto.drom.ru/bmw/1.html",
                            title="BMW 3-Series, 1995",
                            brand="BMW",
                            model="3-Series",
                            attributes={
                                "Год выпуска": "1995",
                                "Комплектация": "316i MT",
                                "Мощность": "102 л.с.",
                            },
                        )
                    ]
                )
                trims = repository.matching_trims(
                    "BMW",
                    "3 серия",
                    1995,
                )
                repository.save_vehicle_analysis(
                    "BMW",
                    "3-Series",
                    "316i MT",
                    {"summary": "Проверить систему охлаждения"},
                    updated_at="2026-07-30T08:00:00+00:00",
                )
                analysis = repository.vehicle_analysis(
                    "BMW",
                    "3 серия",
                    trim_name="316i MT",
                )
                repository.save_listing_vehicle_assessment(
                    "avito",
                    "specific-car",
                    {
                        "excluded_weak_point_ids": ["cooling"],
                        "parts_investment_total": {
                            "min": 10000,
                            "max": 20000,
                        },
                    },
                    description_snapshot="Помпа заменена",
                    updated_at="2026-07-30T08:00:00+00:00",
                )
                assessment = repository.listing_vehicle_assessment(
                    "avito",
                    "specific-car",
                )
                repository.save_vehicle_analysis(
                    "BMW",
                    "5-Series",
                    "523i AT",
                    {
                        "summary": "Типовые проблемы E39",
                        "weak_points": [{"id": "cooling"}],
                    },
                    updated_at="2026-07-30T09:00:00+00:00",
                )
                same_trim_analysis = repository.vehicle_analysis(
                    "BMW",
                    "5 серия",
                    trim_name="523i AT",
                )
                wrong_trim_analysis = repository.vehicle_analysis(
                    "BMW",
                    "5 серия",
                    trim_name="525i AT",
                )
                repository.set_listing_trim_assignment(
                    "avito",
                    "specific-car",
                    "523i AT",
                    assigned_at="2026-07-30T09:00:00+00:00",
                )
                assigned_trim = repository.listing_trim_assignment(
                    "avito",
                    "specific-car",
                )
                analyses = repository.vehicle_analyses()

        self.assertEqual([trim["name"] for trim in trims], ["316i MT"])
        self.assertEqual(trims[0]["attributes"]["Мощность"], "102 л.с.")
        self.assertEqual(
            analysis["data"]["summary"],
            "Проверить систему охлаждения",
        )
        self.assertEqual(
            assessment["data"]["excluded_weak_point_ids"],
            ["cooling"],
        )
        self.assertEqual(
            assessment["description_snapshot"],
            "Помпа заменена",
        )
        self.assertEqual(same_trim_analysis["match_kind"], "exact_trim")
        self.assertEqual(
            same_trim_analysis["data"]["summary"],
            "Типовые проблемы E39",
        )
        self.assertIsNone(wrong_trim_analysis)
        self.assertEqual(assigned_trim, "523i AT")
        self.assertEqual(len(analyses), 2)

    def test_hidden_state_survives_repository_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id="hidden-car",
                            url="https://www.avito.ru/item_hidden-car",
                            title="BMW E39",
                        )
                    ]
                )
                self.assertTrue(
                    repository.set_listing_hidden(
                        "avito",
                        "hidden-car",
                        True,
                    )
                )
            with ListingRepository(database) as repository:
                row = repository.connection.execute(
                    """
                    SELECT hidden FROM listings
                    WHERE source = 'avito' AND external_id = 'hidden-car'
                    """
                ).fetchone()

        self.assertEqual(row["hidden"], 1)

    def test_migrates_new_avito_variants_without_losing_cached_file(self) -> None:
        variants = [
            "https://40.img.avito.st/image/1/1."
            "yIUjpLaAZGw1BMZtP6P9.first",
            "https://40.img.avito.st/image/1/1."
            "yIUjpLaAZGxNBL5tP6P9.second",
            "https://40.img.avito.st/image/1/1."
            "yIUjpLaAZGxVAaZoP6P9.third",
            "https://40.img.avito.st/image/1/1."
            "yIUjpLaAZGwlANZpP6P9.fourth",
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id="duplicate-photo",
                            url="https://www.avito.ru/item_duplicate-photo",
                            title="Volvo XC90",
                        )
                    ]
                )
                for index, url in enumerate(variants):
                    repository.connection.execute(
                        """
                        INSERT INTO listing_images (
                            source, external_id, image_url, local_path, cached_at
                        ) VALUES ('avito', 'duplicate-photo', ?, ?, ?)
                        """,
                        (
                            url,
                            "cache/photo.webp" if index == 3 else None,
                            "2026-07-25T10:00:00+00:00",
                        ),
                    )
                repository.connection.execute(
                    "DELETE FROM app_metadata "
                    "WHERE key = 'deduplicate_avito_image_variants_v2'"
                )
                repository.connection.commit()

            with ListingRepository(database) as repository:
                rows = repository.connection.execute(
                    """
                    SELECT image_url, local_path FROM listing_images
                    WHERE source = 'avito'
                      AND external_id = 'duplicate-photo'
                    """
                ).fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["image_url"], variants[0])
        self.assertEqual(rows[0]["local_path"], "cache/photo.webp")

    def test_search_profile_reports_running_and_finished_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                profile_id = repository.add_search_profile(
                    query="Volvo XC90",
                    region="tver",
                    radius=200,
                    min_price=500_000,
                    max_price=1_500_000,
                    interval_minutes=60,
                    created_at="2026-07-25T10:00:00+00:00",
                )
                repository.begin_search_profile(
                    profile_id,
                    started_at="2026-07-25T10:01:00+00:00",
                )
                running = dict(repository.search_profiles()[0])
                repository.finish_search_profile(
                    profile_id,
                    last_run_at="2026-07-25T10:01:00+00:00",
                    next_run_at="2026-07-25T11:01:00+00:00",
                    status="ok",
                    result_count=12,
                )
                finished = dict(repository.search_profiles()[0])

            self.assertEqual(running["last_status"], "running")
            self.assertEqual(running["min_price"], 500_000)
            self.assertEqual(running["max_price"], 1_500_000)
            self.assertEqual(finished["last_status"], "ok")
            self.assertEqual(finished["last_result_count"], 12)

    def test_same_search_profile_can_exist_for_different_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "listings.db"
            with ListingRepository(database) as repository:
                created_at = "2026-07-26T12:00:00+00:00"
                repository.add_search_profile(
                    source="avito",
                    query="BMW E39",
                    region="tver",
                    radius=200,
                    interval_minutes=60,
                    created_at=created_at,
                )
                repository.add_search_profile(
                    source="auto_ru",
                    query="BMW E39",
                    region="tver",
                    radius=200,
                    interval_minutes=60,
                    created_at=created_at,
                )

                profiles = [dict(row) for row in repository.search_profiles()]

            self.assertEqual({item["source"] for item in profiles}, {"avito", "auto_ru"})

    def test_migrates_legacy_search_profile_unique_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "listings.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE search_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    region TEXT NOT NULL DEFAULT 'all',
                    radius INTEGER,
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at TEXT,
                    next_run_at TEXT,
                    last_status TEXT,
                    last_result_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(query, region, radius)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO search_profiles (
                    query, region, radius, interval_minutes, created_at
                ) VALUES ('BMW E39', 'tver', 200, 60, '2026-07-26T12:00:00+00:00')
                """
            )
            connection.commit()
            connection.close()

            with ListingRepository(database) as repository:
                repository.add_search_profile(
                    source="auto_ru",
                    query="BMW E39",
                    region="tver",
                    radius=200,
                    interval_minutes=60,
                    created_at="2026-07-26T12:00:00+00:00",
                )
                profiles = [dict(row) for row in repository.search_profiles()]

            self.assertEqual(len(profiles), 2)
            self.assertEqual(profiles[0]["source"], "avito")

    def test_upsert_and_price_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id="42",
                            url="https://www.avito.ru/item_42",
                            title="Toyota Camry",
                            price=2_000_000,
                            currency="RUB",
                            description="Первое описание",
                            mileage_km=210_000,
                        )
                    ]
                )

                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id="42",
                            url="https://www.avito.ru/item_42",
                            title="Toyota Camry",
                            price=1_900_000,
                            currency="RUB",
                            description="Обновлённое описание",
                            mileage_km=205_000,
                            brand="Toyota",
                            model="Camry",
                            views_count=1_200,
                            attributes={"Цвет": "Белый"},
                            image_urls=[
                                "https://10.img.avito.st/image/car.jpg"
                            ],
                            cached_images={
                                "https://10.img.avito.st/image/car.jpg":
                                ".cache/42/car.jpg"
                            },
                        )
                    ]
                )
                row = repository.connection.execute(
                    "SELECT price FROM listings WHERE external_id = '42'"
                ).fetchone()
                history_count = repository.connection.execute(
                    "SELECT COUNT(*) FROM price_history WHERE external_id = '42'"
                ).fetchone()[0]
                description = repository.connection.execute(
                    "SELECT description FROM listings WHERE external_id = '42'"
                ).fetchone()[0]
                mileage = repository.connection.execute(
                    "SELECT mileage_km FROM listings WHERE external_id = '42'"
                ).fetchone()[0]
                cached_image = repository.connection.execute(
                    "SELECT local_path FROM listing_images "
                    "WHERE external_id = '42'"
                ).fetchone()[0]
                extended = repository.connection.execute(
                    "SELECT brand, model, views_count, attributes_json "
                    "FROM listings WHERE external_id = '42'"
                ).fetchone()
                restored = repository.get_listing("avito", "42")
                enrichment = repository.list_for_enrichment(
                    "avito", ["42"]
                )

            self.assertEqual(row["price"], 1_900_000)
            self.assertEqual(history_count, 2)
            self.assertEqual(description, "Обновлённое описание")
            self.assertEqual(mileage, 205_000)
            self.assertEqual(cached_image, ".cache/42/car.jpg")
            self.assertEqual(extended["brand"], "Toyota")
            self.assertEqual(extended["model"], "Camry")
            self.assertEqual(extended["views_count"], 1_200)
            self.assertIn('"Цвет": "Белый"', extended["attributes_json"])
            self.assertEqual(
                restored.image_urls,
                ["https://10.img.avito.st/image/car.jpg"],
            )
            self.assertEqual(
                restored.cached_images[
                    "https://10.img.avito.st/image/car.jpg"
                ],
                ".cache/42/car.jpg",
            )
            self.assertEqual(enrichment, [])

    def test_notification_rule_matches_new_listing_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            listing = Listing(
                source="auto_ru",
                external_id="mail-1",
                url="https://auto.ru/cars/used/sale/bmw/5er/1/",
                title="BMW 5 серии E39",
                price=900_000,
                mileage_km=210_000,
                brand="BMW",
                model="5 серия",
                attributes={
                    "Год выпуска": "2001",
                    "Мощность": "193 л.с.",
                    "Цвет": "Чёрный",
                    "Тип двигателя": "Бензин",
                },
            )
            with ListingRepository(database) as repository:
                repository.add_notification_rule(
                    {
                        "name": "E39 до миллиона",
                        "query": "BMW E39",
                        "max_price": 1_000_000,
                        "max_mileage": 250_000,
                        "min_year": 1998,
                        "min_power": 180,
                        "colors": ["Чёрный", "Серый"],
                        "engines": ["Бензин"],
                    },
                    now="2026-07-26T12:00:00+00:00",
                )
                repository.upsert_many([listing])
                repository.upsert_many([listing])
                notifications = repository.notifications()

            self.assertEqual(len(notifications), 1)
            self.assertEqual(notifications[0]["external_id"], "mail-1")

    def test_sqlite_safety_settings_and_profile_cascade(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                self.assertEqual(
                    repository.connection.execute(
                        "PRAGMA foreign_keys"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    repository.connection.execute(
                        "PRAGMA journal_mode"
                    ).fetchone()[0],
                    "wal",
                )
                profile_id = repository.add_search_profile(
                    query="Volvo XC90",
                    region="all",
                    radius=None,
                    interval_minutes=60,
                    created_at="2026-08-05T10:00:00+00:00",
                )
                repository.remember_search_profile_listings(
                    profile_id,
                    [
                        Listing(
                            source="avito",
                            external_id="cascade",
                            url="https://www.avito.ru/cascade",
                            title="Volvo XC90",
                        )
                    ],
                )
                repository.connection.execute(
                    "DELETE FROM search_profiles WHERE id = ?",
                    (profile_id,),
                )
                repository.connection.commit()
                remaining = repository.connection.execute(
                    "SELECT COUNT(*) FROM search_profile_listings "
                    "WHERE profile_id = ?",
                    (profile_id,),
                ).fetchone()[0]
            self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()

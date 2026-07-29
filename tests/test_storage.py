import tempfile
import unittest
import sqlite3
from pathlib import Path

from auto_parser.models import Listing
from auto_parser.storage import ListingRepository


class ListingRepositoryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

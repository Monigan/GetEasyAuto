import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from auto_parser.activity import clear_listing_activity, listing_activity
from auto_parser.models import Listing
from auto_parser.service import SearchService
from auto_parser.scheduler import (
    DETAIL_MAX_CONSECUTIVE_RATE_LIMITS,
    BackgroundScheduler,
)
from auto_parser.sources.base import HttpSourceError, SourceError
from auto_parser.sources import ALL_CARS_QUERY
from auto_parser.storage import ListingRepository


class SchedulerRateLimitTests(unittest.TestCase):
    def test_all_cars_profile_searches_with_empty_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.add_search_profile(
                    source="drom",
                    query=ALL_CARS_QUERY,
                    region="tver",
                    radius=100,
                    interval_minutes=30,
                    created_at="2026-07-29T08:00:00+00:00",
                )
                profile = dict(repository.search_profiles()[0])
            scheduler = BackgroundScheduler(
                database=database,
                cache_dir=Path(directory) / "cache",
            )
            with patch(
                "auto_parser.scheduler.SearchService.search",
                autospec=True,
                return_value=[],
            ) as search:
                scheduler._run_profile(profile)

        service = search.call_args.args[0]
        self.assertEqual(search.call_args.args[1], "")
        self.assertEqual(service.source.name, "drom")
        self.assertEqual(service.source.region, "tver")
        self.assertEqual(service.source.radius, 100)

    def test_card_refresh_stops_after_three_consecutive_429_errors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id=str(index),
                            url=f"https://www.avito.ru/{index}",
                            title=f"Автомобиль {index}",
                            collected_at="2025-01-01T00:00:00+00:00",
                        )
                        for index in range(1, 5)
                    ]
                )
            scheduler = BackgroundScheduler(
                database=database,
                cache_dir=Path(directory) / "cache",
            )
            with patch(
                "auto_parser.scheduler.SearchService.enrich_listing_details",
                autospec=True,
                side_effect=HttpSourceError("avito", 429),
            ) as enrich:
                scheduler._refresh_listing_details()
            third_activity = listing_activity("avito", "3")
            for index in range(1, 5):
                clear_listing_activity("avito", str(index))

        self.assertEqual(
            enrich.call_count,
            DETAIL_MAX_CONSECUTIVE_RATE_LIMITS,
        )
        self.assertIsNotNone(third_activity)
        self.assertIn("3/3", third_activity.stage)

    def test_card_refresh_stops_after_three_consecutive_403_errors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id=str(index),
                            url=f"https://www.avito.ru/{index}",
                            title=f"Автомобиль {index}",
                            collected_at="2025-01-01T00:00:00+00:00",
                        )
                        for index in range(1, 5)
                    ]
                )
            scheduler = BackgroundScheduler(
                database=database,
                cache_dir=Path(directory) / "cache",
            )
            with patch(
                "auto_parser.scheduler.SearchService.enrich_listing_details",
                autospec=True,
                side_effect=HttpSourceError("avito", 403),
            ) as enrich:
                scheduler._refresh_listing_details()
            third_activity = listing_activity("avito", "3")
            for index in range(1, 5):
                clear_listing_activity("avito", str(index))

        self.assertEqual(
            enrich.call_count,
            DETAIL_MAX_CONSECUTIVE_RATE_LIMITS,
        )
        self.assertIsNotNone(third_activity)
        self.assertIn("HTTP 403", third_activity.stage)
        self.assertIn("3/3", third_activity.stage)

    def test_card_refresh_stops_on_first_source_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id="1",
                            url="https://www.avito.ru/1",
                            title="Первая",
                            collected_at="2025-01-01T00:00:00+00:00",
                        ),
                        Listing(
                            source="avito",
                            external_id="2",
                            url="https://www.avito.ru/2",
                            title="Вторая",
                            collected_at="2025-01-01T00:00:00+00:00",
                        ),
                    ]
                )
            scheduler = BackgroundScheduler(
                database=database,
                cache_dir=Path(directory) / "cache",
            )
            with patch(
                "auto_parser.scheduler.SearchService.enrich_listing_details",
                autospec=True,
                side_effect=SourceError("temporary source failure"),
            ) as enrich:
                scheduler._refresh_listing_details()
            activity = listing_activity("avito", "1")
            clear_listing_activity("avito", "1")

        self.assertEqual(enrich.call_count, 1)
        self.assertEqual(
            enrich.call_args.kwargs["request_kind"],
            "refresh_incomplete_detail",
        )
        self.assertIsNotNone(activity)
        self.assertEqual(activity.state, "error")
        self.assertIn("temporary source failure", activity.stage)

    def test_url_profile_uses_copied_avito_search_url(self) -> None:
        copied_url = (
            "https://www.avito.ru/tver/avtomobili/"
            "s_probegom-ASgBAgICAUSGFMjmAQ?radius=200"
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.add_search_profile(
                    query=copied_url,
                    region="tver",
                    radius=200,
                    interval_minutes=60,
                    created_at="2026-07-26T08:00:00+00:00",
                )
                profile = dict(repository.search_profiles()[0])
            scheduler = BackgroundScheduler(
                database=database,
                cache_dir=Path(directory) / "cache",
            )
            with patch(
                "auto_parser.scheduler.SearchService.search",
                autospec=True,
                return_value=[],
            ) as search, patch(
                "auto_parser.scheduler.MailImportConfig.from_env",
                return_value=None,
            ):
                scheduler._run_profile(profile)

        service = search.call_args.args[0]
        self.assertEqual(search.call_args.args[1], "")
        self.assertEqual(service.source.search_url, copied_url)

    def test_profile_saves_each_search_page_before_search_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.add_search_profile(
                    query="BMW E39",
                    region="tver",
                    radius=200,
                    interval_minutes=60,
                    created_at="2026-07-26T08:00:00+00:00",
                )
                profile = dict(repository.search_profiles()[0])
            scheduler = BackgroundScheduler(
                database=database,
                cache_dir=Path(directory) / "cache",
            )
            listing = Listing(
                source="avito",
                external_id="streamed",
                url="https://www.avito.ru/item_streamed",
                title="BMW 525i",
                price=500_000,
                description="Полная карточка",
                image_urls=[
                    "https://10.img.avito.st/image/1/streamed"
                ],
            )
            visible_before_return = False

            def streamed_search(
                _service: SearchService,
                _query: str,
                *,
                on_page: object,
            ) -> list[Listing]:
                nonlocal visible_before_return
                on_page([listing], 1, 1)
                with ListingRepository(database) as repository:
                    visible_before_return = (
                        repository.get_listing("avito", "streamed")
                        is not None
                    )
                return [listing]

            with patch(
                "auto_parser.scheduler.SearchService.search",
                autospec=True,
                side_effect=streamed_search,
            ), patch(
                "auto_parser.scheduler.SearchService.cache_images",
                autospec=True,
                return_value=0,
            ):
                scheduler._run_profile(profile)
            with ListingRepository(database) as repository:
                saved_profile = dict(repository.search_profiles()[0])

        self.assertTrue(visible_before_return)
        self.assertEqual(saved_profile["last_result_count"], 1)

    def test_url_profile_uses_auto_ru_source(self) -> None:
        copied_url = "https://auto.ru/tver/cars/bmw/5er/3473283/all/?query=bmw+e39"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with ListingRepository(database) as repository:
                repository.add_search_profile(
                    source="auto_ru",
                    query=copied_url,
                    region="tver",
                    radius=200,
                    interval_minutes=60,
                    created_at="2026-07-26T08:00:00+00:00",
                )
                profile = dict(repository.search_profiles()[0])
            scheduler = BackgroundScheduler(
                database=database,
                cache_dir=Path(directory) / "cache",
            )
            with patch(
                "auto_parser.scheduler.SearchService.search",
                autospec=True,
                return_value=[],
            ) as search:
                scheduler._run_profile(profile)
            with ListingRepository(database) as repository:
                saved = dict(repository.search_profiles()[0])

        search.assert_not_called()
        self.assertIn("AUTO_RU_IMAP", saved["last_status"])

    def test_default_poll_checks_new_profiles_quickly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scheduler = BackgroundScheduler(
                database=Path(directory) / "test.db",
                cache_dir=Path(directory) / "cache",
            )

        self.assertEqual(scheduler.poll_seconds, 5)

    def test_concurrent_tick_keeps_other_sources_running_after_avito_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scheduler = BackgroundScheduler(
                database=Path(directory) / "test.db",
                cache_dir=Path(directory) / "cache",
            )
            completed_sources: list[str] = []

            def tick_source(source_name: str) -> None:
                if source_name == "avito":
                    raise RuntimeError("Avito unavailable")
                completed_sources.append(source_name)

            with patch.object(
                scheduler,
                "_tick_source",
                side_effect=tick_source,
            ):
                scheduler._tick()

        self.assertCountEqual(completed_sources, ["drom", "auto_ru"])

    def test_search_cooldown_is_isolated_by_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scheduler = BackgroundScheduler(
                database=Path(directory) / "test.db",
                cache_dir=Path(directory) / "cache",
            )
            scheduler._register_rate_limit(
                HttpSourceError("avito", 429),
                source_name="avito",
            )

        self.assertIsNotNone(scheduler._search_cooldowns["avito"])
        self.assertIsNone(scheduler._search_cooldowns.get("drom"))

    def test_rate_limit_uses_exponential_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scheduler = BackgroundScheduler(
                database=Path(directory) / "test.db",
                cache_dir=Path(directory) / "cache",
            )
            first = scheduler._register_rate_limit(
                HttpSourceError("avito", 429)
            )
            second = scheduler._register_rate_limit(
                HttpSourceError("avito", 429)
            )

        self.assertGreaterEqual((second - first).total_seconds(), 14 * 60)

    def test_rate_limit_respects_retry_after(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scheduler = BackgroundScheduler(
                database=Path(directory) / "test.db",
                cache_dir=Path(directory) / "cache",
            )
            started = datetime.now(timezone.utc)
            cooldown = scheduler._register_rate_limit(
                HttpSourceError(
                    "avito",
                    429,
                    retry_after_seconds=7200,
                )
            )

        self.assertGreaterEqual(
            (cooldown - started).total_seconds(),
            7199,
        )
        self.assertGreaterEqual(scheduler._rate_limit_strikes, 1)

    def test_profile_is_rescheduled_without_crashing_on_429(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            cache = Path(directory) / "cache"
            with ListingRepository(database) as repository:
                profile_id = repository.add_search_profile(
                    query="Volvo XC90",
                    region="tver",
                    radius=200,
                    interval_minutes=15,
                    created_at="2026-07-25T10:00:00+00:00",
                )
                profile = dict(repository.search_profiles()[0])
            scheduler = BackgroundScheduler(
                database=database,
                cache_dir=cache,
            )
            with patch(
                "auto_parser.scheduler.SearchService.search",
                side_effect=HttpSourceError(
                    "avito",
                    429,
                    retry_after_seconds=3600,
                ),
            ):
                scheduler._run_profile(profile)
            with ListingRepository(database) as repository:
                saved = dict(repository.search_profiles()[0])

        self.assertEqual(saved["id"], profile_id)
        self.assertIn("Ограничение Avito HTTP 429", saved["last_status"])
        self.assertGreater(saved["next_run_at"], saved["last_run_at"])


if __name__ == "__main__":
    unittest.main()

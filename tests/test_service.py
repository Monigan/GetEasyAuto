import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from auto_parser.models import Listing
from auto_parser.images import avito_image_identity
from auto_parser.service import SearchService
from auto_parser.sources.avito import AvitoSource
from auto_parser.sources.base import HttpSourceError


class SearchPaginationTests(unittest.TestCase):
    def test_validation_preserves_sold_status_from_detail_page(self) -> None:
        service = SearchService(
            AvitoSource(region="tver"),
            check_robots=False,
        )
        listing = Listing(
            source="avito",
            external_id="sold",
            url="https://www.avito.ru/item_sold",
            title="Проданный автомобиль",
            price=900_000,
        )

        def mark_sold(item: Listing, _html: str) -> Listing:
            item.status = "sold"
            return item

        with patch.object(SearchService, "_fetch_html", return_value="sold"), patch.object(
            AvitoSource,
            "enrich_listing",
            side_effect=mark_sold,
        ):
            status = service.validate_listing(listing)

        self.assertEqual(status, "sold")
        self.assertEqual(listing.sold_price, 900_000)
        self.assertIsNotNone(listing.sold_at)

    def test_validation_returns_hidden_status_from_detail_page(self) -> None:
        service = SearchService(
            AvitoSource(region="tver"),
            check_robots=False,
        )
        listing = Listing(
            source="avito",
            external_id="hidden",
            url="https://www.avito.ru/item_hidden",
            title="Скрытый автомобиль",
            price=900_000,
        )

        def mark_hidden(item: Listing, _html: str) -> Listing:
            item.status = "hidden"
            item.last_validated_at = "2026-07-29T18:00:00+00:00"
            return item

        with patch.object(SearchService, "_fetch_html", return_value="hidden"), patch.object(
            AvitoSource,
            "enrich_listing",
            side_effect=mark_hidden,
        ):
            status = service.validate_listing(listing)

        self.assertEqual(status, "hidden")
        self.assertIsNone(listing.sold_price)
        self.assertIsNone(listing.sold_at)

    def test_cache_reuses_file_for_another_variant_of_same_photo(self) -> None:
        first = (
            "https://40.img.avito.st/image/1/1."
            "yIUjpLaAZGw1BMZtP6P9.first"
        )
        another = (
            "https://40.img.avito.st/image/1/1."
            "yIUjpLaAZGxNBL5tP6P9.second"
        )
        service = SearchService(
            AvitoSource(region="tver"),
            check_robots=False,
        )
        listing = Listing(
            source="avito",
            external_id="photo-cache",
            url="https://www.avito.ru/item_photo-cache",
            title="Volvo XC90",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            digest = hashlib.sha256(
                avito_image_identity(first).encode("utf-8")
            ).hexdigest()[:20]
            expected = cache / listing.external_id / f"{digest}.webp"
            expected.parent.mkdir()
            expected.write_bytes(b"cached")

            result = service._cache_image(
                another,
                listing=listing,
                cache_dir=cache,
            )

        self.assertEqual(result, expected)

    def test_collects_more_than_one_search_page(self) -> None:
        service = SearchService(
            AvitoSource(region="tver"),
            check_robots=False,
        )

        def page_result(_service: SearchService, url: str) -> list[Listing]:
            page = int(parse_qs(urlsplit(url).query).get("p", ["1"])[0])
            start = (page - 1) * 50
            return [
                Listing(
                    source="avito",
                    external_id=str(index),
                    url=f"https://www.avito.ru/item_{index}",
                    title=f"Автомобиль {index}",
                    price=1_000_000 + index,
                    currency="RUB",
                )
                for index in range(start, start + 50)
            ]

        with patch.object(
            SearchService,
            "_search_url",
            autospec=True,
            side_effect=page_result,
        ) as fetch:
            listings = service.search(
                "Volvo XC90",
                max_results=120,
                max_pages=5,
            )

        self.assertEqual(len(listings), 120)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(listings[-1].external_id, "119")

    def test_default_search_has_no_five_page_cap(self) -> None:
        service = SearchService(
            AvitoSource(region="tver"),
            check_robots=False,
        )

        def page_result(_service: SearchService, url: str) -> list[Listing]:
            page = int(parse_qs(urlsplit(url).query).get("p", ["1"])[0])
            start = (page - 1) * 50
            return [
                Listing(
                    source="avito",
                    external_id=str(index),
                    url=f"https://www.avito.ru/item_{index}",
                    title=f"Автомобиль {index}",
                )
                for index in range(start, start + 50)
            ]

        saved_pages: list[tuple[int, int, int]] = []

        with patch.object(
            SearchService,
            "_search_url",
            autospec=True,
            side_effect=page_result,
        ) as fetch:
            listings = service.search(
                "Volvo XC90",
                max_results=320,
                on_page=lambda items, page, total: saved_pages.append(
                    (page, len(items), total)
                ),
            )

        self.assertEqual(len(listings), 320)
        self.assertEqual(fetch.call_count, 7)
        self.assertEqual(listings[-1].external_id, "319")
        self.assertEqual(saved_pages[0], (1, 50, 50))
        self.assertEqual(saved_pages[-1], (7, 20, 320))

    def test_keeps_completed_pages_when_next_page_is_rate_limited(self) -> None:
        service = SearchService(
            AvitoSource(region="tver"),
            check_robots=False,
        )
        first_page = [
            Listing(
                source="avito",
                external_id=str(index),
                url=f"https://www.avito.ru/item_{index}",
                title=f"Автомобиль {index}",
            )
            for index in range(50)
        ]

        with patch.object(
            SearchService,
            "_search_url",
            autospec=True,
            side_effect=[
                first_page,
                HttpSourceError("avito", 429, retry_after_seconds=900),
            ],
        ):
            listings = service.search("Volvo XC90")

        self.assertEqual(len(listings), 50)
        self.assertEqual(service.rate_limit_error.status_code, 429)


if __name__ == "__main__":
    unittest.main()

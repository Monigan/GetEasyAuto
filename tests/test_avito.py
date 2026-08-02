import json
import unittest
from pathlib import Path

from auto_parser.sources.avito import AvitoSource


FIXTURES = Path(__file__).parent / "fixtures"


class AvitoSourceTests(unittest.TestCase):
    def test_applies_price_range_locally_without_disallowed_url_params(self) -> None:
        url = AvitoSource(
            region="tver",
            min_price=100_000,
            max_price=150_000,
        ).build_search_url("")

        self.assertNotIn("pmin=", url)
        self.assertNotIn("pmax=", url)

    def test_builds_all_cars_url_with_region_and_radius(self) -> None:
        url = AvitoSource(region="tver", radius=150).build_search_url("")

        self.assertEqual(
            url,
            "https://www.avito.ru/tver/avtomobili"
            "?cd=1",
        )

    def test_build_search_url(self) -> None:
        url = AvitoSource().build_search_url("  Toyota   Camry ")
        self.assertEqual(
            url,
            "https://www.avito.ru/all/avtomobili/"
            "toyota/camry-ASgBAgICAkTgtg20mSjitg3UoCg?cd=1",
        )

    def test_build_known_generation_url_with_region_and_radius(self) -> None:
        source = AvitoSource(region="tver", radius=200)
        url = source.build_search_url("BMW E39")

        self.assertEqual(
            url,
            "https://www.avito.ru/tver/avtomobili/"
            "bmw/5_seriya/e39-ASgBAgICA0Tgtg3klyjitg3UnCjqtg3yhCk"
            "?cd=1",
        )
        self.assertEqual(
            source.build_search_urls("BMW E39")[1],
            "https://www.avito.ru/tver/avtomobili"
            "?q=BMW+E39&cd=1",
        )

    def test_unknown_query_uses_text_search(self) -> None:
        url = AvitoSource(region="tver", radius=50).build_search_url("Volvo XC90")

        self.assertEqual(
            url,
            "https://www.avito.ru/tver/avtomobili"
            "?q=Volvo+XC90&cd=1",
        )

    def test_builds_next_page_without_losing_search_filters(self) -> None:
        source = AvitoSource(region="tver", radius=200)
        first = source.build_search_url("Volvo XC90")
        second = source.build_page_url(first, 2)

        self.assertIn("q=Volvo+XC90", second)
        self.assertNotIn("radius=", second)
        self.assertIn("p=2", second)

    def test_accepts_copied_avito_search_url(self) -> None:
        copied = (
            "https://www.avito.ru/tver/avtomobili/toyota/"
            "camry-ASgBAgICAkTgtg20mSjitg3UoCg?cd=1&radius=200"
        )

        self.assertEqual(
            AvitoSource(search_url=copied).build_search_url(""),
            "https://www.avito.ru/tver/avtomobili/toyota/"
            "camry-ASgBAgICAkTgtg20mSjitg3UoCg?cd=1",
        )

    def test_rejects_external_search_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "avito.ru"):
            AvitoSource(search_url="https://example.com/avtomobili").build_search_url(
                ""
            )

    def test_parse_semantic_html_cards(self) -> None:
        html = """
        <html><head><title>BMW E39 в Твери</title></head><body>
          <div data-marker="item">
            <a data-marker="item-title"
               href="/tver/avtomobili/bmw_5_serii_2001_1234567890">
              BMW 5 серия, 2001, 200 100 км
            </a>
            <meta itemprop="price" content="850000">
            <img src="https://10.img.avito.st/image/1/vehicle.jpg">
            <p data-marker="item-description">Хорошее состояние. Без ДТП.</p>
            <div data-marker="item-location">Тверь</div>
          </div>
        </body></html>
        """

        listings = AvitoSource().parse_search_page(html)

        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].external_id, "1234567890")
        self.assertEqual(listings[0].price, 850_000)
        self.assertEqual(listings[0].mileage_km, 200_100)
        self.assertEqual(listings[0].description, "Хорошее состояние. Без ДТП.")
        self.assertEqual(
            listings[0].image_urls,
            ["https://10.img.avito.st/image/1/vehicle.jpg"],
        )

    def test_reports_access_block_instead_of_empty_result(self) -> None:
        with self.assertRaisesRegex(Exception, "CAPTCHA"):
            AvitoSource().parse_search_page(
                "<html><title>Доступ ограничен</title></html>"
            )

    def test_parse_json_ld_listings(self) -> None:
        html = (FIXTURES / "avito_search.html").read_text(encoding="utf-8")
        listings = AvitoSource().parse_search_page(html)

        self.assertEqual(len(listings), 2)
        self.assertEqual(listings[0].external_id, "1234567890")
        self.assertEqual(listings[0].price, 2_750_000)
        self.assertEqual(listings[0].location, "Москва")
        self.assertEqual(
            listings[0].description,
            "Один владелец. Кузов без ДТП. Сервисная книжка в наличии.",
        )
        self.assertEqual(listings[0].mileage_km, 142_000)
        self.assertEqual(listings[1].external_id, "9876543210")

    def test_enriches_listing_from_detail_page(self) -> None:
        listing = AvitoSource().parse_search_page(
            """
            <div data-marker="item">
              <a data-marker="item-title"
                 href="/tver/avtomobili/bmw_2001_1234567890">
                BMW 5 серия, 2001
              </a>
            </div>
            """
        )[0]
        hydration = json.dumps(
            {
                "loaderData": {
                    "item": {
                        "id": 1234567890,
                        "imageUrls": [
                            {
                                "1280x960": (
                                    "https://10.img.avito.st/image/1/1."
                                    "caronea4full.high"
                                ),
                                "75x55": (
                                    "https://10.img.avito.st/image/1/1."
                                    "caronea3thumb.low"
                                ),
                            },
                            {
                                "1280x960": (
                                    "https://20.img.avito.st/image/1/1."
                                    "cartwoa4full.high"
                                ),
                                "75x55": (
                                    "https://20.img.avito.st/image/1/1."
                                    "cartwoa3thumb.low"
                                ),
                            },
                        ],
                    }
                }
            },
            ensure_ascii=False,
        )
        hydration_argument = json.dumps(hydration, ensure_ascii=False)
        detail_html = f"""
        <html><head>
          <meta property="og:description" content="BMW в отличном состоянии">
          <meta property="og:image"
                content="https://m.avito.ru/icons/touch-icon-512x512.png">
        </head><body>
          <script>
            window.__staticRouterHydrationData =
              JSON.parse({hydration_argument})
          </script>
          <img data-marker="image-frame/image"
               src="https://10.img.avito.st/image/car-main.jpg">
          <div data-marker="item-view/item-description">
            Два владельца. Оригинальный ПТС.
          </div>
          <div data-marker="item-view/item-date">25 июля в 12:30</div>
          <span data-marker="item-view/total-views">1 234 просмотра</span>
          <ul data-marker="item-view/item-params">
            <li>Пробег: 215 400 км</li>
            <li><span>Цвет</span><span>Чёрный</span></li>
            <li><span>Мощность</span><span>192 л.с.</span></li>
          </ul>
        </body></html>
        """

        AvitoSource().enrich_listing(listing, detail_html)

        self.assertEqual(
            listing.description,
            "Два владельца. Оригинальный ПТС.",
        )
        self.assertEqual(listing.mileage_km, 215_400)
        self.assertEqual(listing.published_at, "25 июля в 12:30")
        self.assertEqual(
            listing.image_urls,
            [
                "https://10.img.avito.st/image/1/1.caronea4full.high",
                "https://20.img.avito.st/image/1/1.cartwoa4full.high",
            ],
        )
        self.assertEqual(listing.views_count, 1_234)
        self.assertEqual(listing.attributes["Цвет"], "Чёрный")
        self.assertEqual(listing.attributes["Мощность"], "192 л.с.")

    def test_detects_sold_listing_from_avito_state(self) -> None:
        listing = AvitoSource().parse_search_page(
            """
            <div data-marker="item">
              <a data-marker="item-title"
                 href="/klin/avtomobili/bmw_1997_8051475686">
                BMW 5 серия, 1997
              </a>
            </div>
            """
        )[0]
        detail_html = """
        <html><body>
          <script>
            window.__staticRouterHydrationData = {
              "loaderData": {"item": {"id": 8051475686, "isItemSold": true}}
            };
          </script>
          <div data-marker="item-view/item-status">Автомобиль продан</div>
        </body></html>
        """

        AvitoSource().enrich_listing(listing, detail_html)

        self.assertEqual(listing.status, "sold")
        self.assertEqual(listing.attributes["Статус на площадке"], "Продано")
        self.assertIsNotNone(listing.sold_at)

    def test_detects_listing_hidden_by_avito(self) -> None:
        listing = AvitoSource().parse_search_page(
            """
            <div data-marker="item">
              <a data-marker="item-title"
                 href="/pushkino/avtomobili/bmw_1997_8199311527">
                BMW 5 серия, 1997
              </a>
            </div>
            """
        )[0]
        detail_html = """
        <html><body>
          <script>
            window.__staticRouterHydrationData = {
              "loaderData": {
                "item": {"id": 8199311527, "itemStatus": "hidden"}
              }
            };
          </script>
          <div data-marker="item-view/item-status">Объявление скрыто</div>
        </body></html>
        """

        AvitoSource().enrich_listing(listing, detail_html)

        self.assertEqual(listing.status, "hidden")
        self.assertEqual(listing.attributes["Статус на площадке"], "Скрыто")
        self.assertIsNone(listing.sold_price)
        self.assertIsNone(listing.sold_at)

    def test_empty_query_builds_all_russia_catalog(self) -> None:
        self.assertEqual(
            AvitoSource().build_search_url(" "),
            "https://www.avito.ru/all/avtomobili?cd=1",
        )


if __name__ == "__main__":
    unittest.main()

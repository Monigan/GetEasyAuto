import unittest

from auto_parser.models import Listing
from auto_parser.sources import source_name_from_url
from auto_parser.sources.drom import DromSource


SEARCH_HTML = """
<html><body>
  <div data-ftid="bulls-list_bull">
    <div data-ftid="bull_image">
      <a href="https://auto.drom.ru/tver/bmw/3-series/793878963.html">
        <img alt="Седан BMW 3-Series 1995 года, 267666 рублей, Тверь"
             src="https://s12.auto.drom.ru/photo/v2/photo-one/gen272wb.jpg"
             srcset="https://s12.auto.drom.ru/photo/v2/photo-one/gen272wb.jpg 1x,
                     https://s12.auto.drom.ru/photo/v2/photo-one/gen544wb.jpg 2x">
      </a>
    </div>
    <a data-ftid="bull_title"
       href="https://auto.drom.ru/tver/bmw/3-series/793878963.html">
      <h3><style>.generated-title { color: red; }</style>BMW 3-Series, 1995</h3>
    </a>
    <div data-ftid="bull_subtitle">316i MT</div>
    <div data-ftid="component_inline-bull-description">
      <span data-ftid="bull_description-item">1.6 л (102 л.с.),</span>
      <span data-ftid="bull_description-item">бензин,</span>
      <span data-ftid="bull_description-item">механика,</span>
      <span data-ftid="bull_description-item">задний,</span>
      <span data-ftid="bull_description-item">299 666 км</span>
    </div>
    <span data-ftid="bull_price">267&nbsp;666</span> ₽
    <span data-ftid="bull_location">Тверь</span>
    <div data-ftid="bull_date">23 июля</div>
  </div>
  <div data-ftid="bulls-list_bull">
    <a data-ftid="bull_title"
       href="https://auto.drom.ru/tver/bmw/5-series/493713066.html">
      BMW 5-Series, 1979
    </a>
    <span data-ftid="bull_price">350 000</span> ₽
    <div data-ftid="bull_sold">снят с продажи</div>
  </div>
</body></html>
"""


DETAIL_HTML = """
<html><head>
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "Car",
    "name": "BMW 3-Series",
    "brand": {"@type": "Brand", "name": "BMW"},
    "model": "3-Series",
    "vehicleModelDate": "1995",
    "description": "Краткое описание",
    "image": {"@type": "ImageObject", "url": "https://s12.auto.drom.ru/photo/v2/photo-one/gen600.jpg"},
    "offers": {"@type": "Offer", "price": 267666, "priceCurrency": "RUB", "availability": "https://schema.org/InStock"},
    "mileageFromOdometer": {"@type": "QuantitativeValue", "value": "299666", "unitCode": "KMT"}
  }
  </script>
</head><body>
  <h1 data-ftid="page-title">Продажа BMW 3-Series, 1995 год в Твери</h1>
  <div data-ftid="bull-page_bull-gallery_thumbnails">
    <a href="https://s12.auto.drom.ru/photo/v2/photo-one/gen1200.jpg"><img src="https://s12.auto.drom.ru/photo/v2/photo-one/gen115.jpg"></a>
    <a href="https://s12.auto.drom.ru/photo/v2/photo-two/gen1200.jpg"><img src="https://s12.auto.drom.ru/photo/v2/photo-two/gen115.jpg"></a>
  </div>
  <div data-ftid="bull-page_similar-car-offers">
    <img src="https://s12.auto.drom.ru/photo/v2/unrelated-car/gen1200.jpg">
  </div>
  <div data-ftid="bull-page_bull-views">
    <div>Объявление 793878963 от 23.07.2026</div><div>92</div>
  </div>
  <div data-ftid="bulletin-price">267 666 ₽</div>
  <table data-ftid="bulletin-specifications">
    <tr><th data-ftid="property">Год выпуска</th><td data-ftid="value">1995</td></tr>
    <tr><th data-ftid="property">Двигатель</th><td data-ftid="value">бензин, 1.6 л</td></tr>
    <tr><th data-ftid="property">Мощность</th><td data-ftid="value">102 л.с., <button>налог</button></td></tr>
    <tr><th data-ftid="property">Коробка передач</th><td data-ftid="value">механика</td></tr>
    <tr><th data-ftid="property">Пробег</th><td data-ftid="value">299 666 км</td></tr>
  </table>
  <div data-ftid="bulletin-description">
    <div data-ftid="info-full">
      <span data-ftid="property">Дополнительно:</span>
      <span data-ftid="value">Машина на полном ходу. Документы чистые.</span>
    </div>
    <div data-ftid="city">
      <span data-ftid="property">Город:</span><span data-ftid="value">Тверь</span>
    </div>
  </div>
</body></html>
"""


class DromSourceTests(unittest.TestCase):
    def test_builds_all_cars_url_with_price_limit(self) -> None:
        self.assertEqual(
            DromSource(max_price=150_000).build_search_url(""),
            "https://auto.drom.ru/?maxprice=150000",
        )

    def test_price_settings_override_copied_url_values(self) -> None:
        source = DromSource(
            min_price=100_000,
            max_price=150_000,
            search_url="https://auto.drom.ru/all/?maxprice=900000",
        )

        self.assertEqual(
            source.build_search_url(""),
            "https://auto.drom.ru/all/?minprice=100000&maxprice=150000",
        )

    def test_builds_all_cars_url_with_region_and_radius(self) -> None:
        source = DromSource(region="tver", radius=100)

        self.assertEqual(
            source.build_search_url(""),
            "https://auto.drom.ru/tver/?distance=100",
        )
        self.assertEqual(
            source.build_page_url(
                "https://auto.drom.ru/tver/?distance=100",
                2,
            ),
            "https://auto.drom.ru/tver/page2/?distance=100",
        )

    def test_builds_search_url_and_pagination(self) -> None:
        source = DromSource(region="tver", radius=200)
        first = source.build_search_url("BMW E39")
        self.assertEqual(
            first,
            "https://auto.drom.ru/tver/bmw/5-series/?distance=200",
        )
        self.assertEqual(
            source.build_page_url(first, 2),
            "https://auto.drom.ru/tver/bmw/5-series/page2/?distance=200",
        )

    def test_brand_search_keeps_all_path_segment(self) -> None:
        source = DromSource(region="tver")
        first = source.build_search_url("BMW")
        self.assertEqual(first, "https://auto.drom.ru/tver/bmw/all/")
        self.assertEqual(
            source.build_page_url(first, 2),
            "https://auto.drom.ru/tver/bmw/all/page2/",
        )

    def test_accepts_copied_drom_url(self) -> None:
        copied = "https://auto.drom.ru/tver/bmw/all/?order=price#tabs"
        source = DromSource(search_url=copied)
        self.assertEqual(
            source.build_search_url(""),
            "https://auto.drom.ru/tver/bmw/all/?order=price",
        )
        self.assertEqual(source_name_from_url(copied), "drom")

        model_url = "https://auto.drom.ru/tver/bmw/5-series/?distance=200"
        self.assertEqual(
            DromSource(search_url=model_url).build_search_url(""),
            model_url,
        )

        all_cars_url = "https://auto.drom.ru/tver/"
        self.assertEqual(
            DromSource(search_url=all_cars_url).build_search_url(""),
            all_cars_url,
        )

    def test_parses_search_cards(self) -> None:
        listings = DromSource(region="tver").parse_search_page(SEARCH_HTML)
        self.assertEqual(len(listings), 2)
        listing = listings[0]
        self.assertEqual(listing.source, "drom")
        self.assertEqual(listing.external_id, "793878963")
        self.assertEqual(listing.price, 267666)
        self.assertEqual(listing.mileage_km, 299666)
        self.assertEqual(listing.brand, "BMW")
        self.assertEqual(listing.model, "3-Series")
        self.assertEqual(listing.location, "Тверь")
        self.assertEqual(listing.attributes["Комплектация"], "316i MT")
        self.assertEqual(listing.attributes["Мощность"], "102 л.с.")
        self.assertEqual(listing.attributes["Коробка передач"], "механика")
        self.assertEqual(
            listing.image_urls,
            ["https://s12.auto.drom.ru/photo/v2/photo-one/gen544wb.jpg"],
        )
        self.assertEqual(listings[1].status, "sold")

    def test_enriches_listing_from_detail_page(self) -> None:
        listing = Listing(
            source="drom",
            external_id="793878963",
            url="https://auto.drom.ru/tver/bmw/3-series/793878963.html",
            title="BMW 3-Series, 1995",
        )

        DromSource().enrich_listing(listing, DETAIL_HTML)

        self.assertEqual(listing.title, "BMW 3-Series, 1995")
        self.assertEqual(
            listing.description,
            "Машина на полном ходу. Документы чистые.",
        )
        self.assertEqual(listing.price, 267666)
        self.assertEqual(listing.mileage_km, 299666)
        self.assertEqual(listing.views_count, 92)
        self.assertEqual(listing.location, "Тверь")
        self.assertEqual(listing.published_at, "2026-07-23T00:00:00+00:00")
        self.assertEqual(listing.attributes["Мощность"], "102 л.с.")
        self.assertEqual(len(listing.image_urls), 2)

    def test_reports_access_block(self) -> None:
        with self.assertRaisesRegex(Exception, "CAPTCHA"):
            DromSource().parse_search_page("<html>Подтвердите, что вы не робот</html>")


if __name__ == "__main__":
    unittest.main()

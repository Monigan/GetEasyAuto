import unittest

from auto_parser.models import Listing
from auto_parser.sources.auto_ru import AutoRuSource


SEARCH_HTML = """
<html><body><div data-testid="listing-infinite-container">
  <div class="ListingItemUniversal-BAZaq ListingItemUniversal-CEMgQ">
    <a class="Link ListingItemTitle__link" href="/cars/used/sale/ford/focus/1132136561-f2008079/">
      Ford Focus I Рестайлинг
    </a>
    <div class="ListingItemUniversalSpecs__subtitle-TVmJO">Чёрный</div>
    <div class="ListingItemUniversalSpecs__spec-S5lzA">2.0 л, 136 л.с., бензин</div>
    <div class="ListingItemUniversalSpecs__spec-S5lzA">Седан</div>
    <div class="ListingItemUniversalSpecs__spec-S5lzA">Передний привод</div>
    <div class="ListingItemUniversalSpecs__spec-S5lzA">Автомат</div>
    <div>2004</div><div>208 400 км</div><div>300 000 ₽</div>
    <span class="MetroListPlace__regionName">Тверь</span>
    <img src="https://avatars.avto.ru/get-autoru-vos/1/photo/320x240"
         srcset="//avatars.avto.ru/get-autoru-vos/1/photo/320x240 320w, //avatars.avto.ru/get-autoru-vos/1/photo/1200x900 1200w">
  </div>
</div></body></html>
"""

DETAIL_HTML = """
<html><head>
  <meta property="og:description" content="Седан Ford Focus 2004 года, пробег 208 400 км, двигатель 2.0 AT (136 л.с.), цвет чёрный за 300 000 рублей.">
  <meta property="og:image" content="https://avatars.avto.ru/get-autoru-vos/1/photo/1200x900">
</head><body>
  <h1 class="CardHead__title">Ford Focus I Рестайлинг, 2004</h1>
  <div class="CardHead__infoItem CardHead__creationDate">10 апреля</div>
  <div class="CardHead__infoItem CardHead__views">293 (2 сегодня)</div>
  <div class="CardInfoSummarySimpleRow__label-a">Пробег</div>
  <div class="CardInfoSummarySimpleRow__content-b">208 400 км</div>
  <div class="CardInfoSummaryComplexRow__cellTitle-a">Коробка</div>
  <div class="CardInfoSummaryComplexRow__cellValue-b">автоматическая</div>
  <div class="CardInfoSummaryComplexRow__cellTitle-a">Цвет</div>
  <a class="Link Link_color_black CardInfoSummaryComplexRow__cellValue-b">чёрный</a>
  <div class="CardDescription__textInner">Автомобиль в хорошем состоянии. Обслужен.</div>
  <img class="ImageGalleryDesktop__image"
       src="https://avatars.avto.ru/get-autoru-vos/2/photo/584x438"
       srcset="//avatars.avto.ru/get-autoru-vos/2/photo/584x438 584w, //avatars.avto.ru/get-autoru-vos/2/photo/1200x900 1200w">
</body></html>
"""


class AutoRuSourceTests(unittest.TestCase):
    def test_builds_url_with_price_range(self) -> None:
        self.assertEqual(
            AutoRuSource(
                region="tver",
                min_price=100_000,
                max_price=150_000,
            ).build_search_url(""),
            (
                "https://auto.ru/tver/cars/all/"
                "?price_from=100000&price_to=150000"
            ),
        )

    def test_builds_all_cars_urls(self) -> None:
        self.assertEqual(
            AutoRuSource(region="tver").build_search_url(""),
            "https://auto.ru/tver/cars/all/",
        )
        self.assertEqual(
            AutoRuSource(region="all").build_search_url(""),
            "https://auto.ru/cars/all/",
        )

    def test_builds_search_url_and_preserves_catalog_filters(self) -> None:
        source = AutoRuSource(region="tver")
        self.assertEqual(
            source.build_search_url("BMW E39"),
            "https://auto.ru/tver/cars/all/?query=BMW+E39&from=searchline",
        )
        filtered = (
            "https://auto.ru/tver/cars/all/"
            "?catalog_filter=mark%3DBMW%2Cmodel%3D7ER"
            "&catalog_filter=mark%3DBMW%2Cmodel%3D8ER"
        )
        self.assertEqual(AutoRuSource(search_url=filtered).build_search_url(""), filtered)

    def test_parses_search_cards(self) -> None:
        listings = AutoRuSource(region="tver").parse_search_page(SEARCH_HTML)
        self.assertEqual(len(listings), 1)
        listing = listings[0]
        self.assertEqual(listing.source, "auto_ru")
        self.assertEqual(listing.external_id, "1132136561")
        self.assertEqual(listing.price, 300000)
        self.assertEqual(listing.mileage_km, 208400)
        self.assertEqual(listing.location, "Тверь")
        self.assertEqual(listing.attributes["Мощность"], "136 л.с.")
        self.assertEqual(listing.attributes["Коробка передач"], "Автомат")
        self.assertEqual(
            listing.image_urls,
            ["https://avatars.avto.ru/get-autoru-vos/1/photo/1200x900"],
        )

    def test_enriches_listing_from_detail_page(self) -> None:
        listing = Listing(
            source="auto_ru",
            external_id="1132136561",
            url="https://auto.ru/cars/used/sale/ford/focus/1132136561-f2008079/",
            title="Ford Focus",
        )
        AutoRuSource().enrich_listing(listing, DETAIL_HTML)
        self.assertEqual(listing.title, "Ford Focus I Рестайлинг")
        self.assertEqual(listing.description, "Автомобиль в хорошем состоянии. Обслужен.")
        self.assertEqual(listing.views_count, 293)
        self.assertEqual(listing.mileage_km, 208400)
        self.assertEqual(listing.price, 300000)
        self.assertEqual(listing.attributes["Коробка"], "автоматическая")
        self.assertEqual(listing.attributes["Цвет"], "чёрный")
        self.assertEqual(listing.attributes["Мощность"], "136 л.с.")
        self.assertIsNotNone(listing.published_at)
        self.assertEqual(len(listing.image_urls), 2)


if __name__ == "__main__":
    unittest.main()

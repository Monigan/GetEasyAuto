import unittest

from auto_parser.formatting import format_listing, format_price
from auto_parser.models import Listing


class FormattingTests(unittest.TestCase):
    def test_formats_ruble_price(self) -> None:
        self.assertEqual(format_price(2_750_000, "RUB"), "2 750 000 руб.")

    def test_formats_listing_card(self) -> None:
        listing = Listing(
            source="avito",
            external_id="42",
            url="https://www.avito.ru/item_42",
            title="  Toyota   Camry ",
            price=2_750_000,
            currency="RUB",
            description=" Один владелец.   Без ДТП. ",
            mileage_km=142_000,
            image_urls=[
                "https://10.img.avito.st/image/1.jpg",
                "https://10.img.avito.st/image/2.jpg",
            ],
            cached_images={
                "https://10.img.avito.st/image/1.jpg": ".cache/1.jpg"
            },
        )

        card = format_listing(listing)

        self.assertIn("Toyota Camry", card)
        self.assertIn("Цена:     2 750 000 руб.", card)
        self.assertIn("Пробег:   142 000 км", card)
        self.assertIn("Описание: Один владелец. Без ДТП.", card)
        self.assertIn("Фото:     1/2 в кэше", card)


if __name__ == "__main__":
    unittest.main()

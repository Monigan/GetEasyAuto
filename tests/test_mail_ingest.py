import unittest
from unittest.mock import patch

from auto_parser.mail_ingest import MailImportConfig, parse_auto_ru_message


MESSAGE = """From: Auto.ru <notifications@auto.ru>
To: user@example.ru
Subject: =?utf-8?b?0J3QvtCy0YvQtSDQvtCx0YrRj9Cy0LvQtdC90LjRjw==?=
Date: Sun, 26 Jul 2026 18:30:00 +0000
Message-ID: <auto-ru-search-1@example.ru>
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body>
  <div>
    <img src="https://avatars.avto.ru/get-autoru-vos/123/1200x900">
    <a href="https://auto.ru/cars/used/sale/bmw/5er/1122334455-aabbcc/">BMW 5 серии 2001</a>
    <span>950 000 ₽</span><span>245 000 км</span>
  </div>
</body></html>
""".encode("utf-8")


class AutoRuMailIngestTests(unittest.TestCase):
    def test_parses_listing_from_auto_ru_notification(self) -> None:
        parsed = parse_auto_ru_message(MESSAGE)

        self.assertEqual(parsed.message_id, "<auto-ru-search-1@example.ru>")
        self.assertEqual(len(parsed.listings), 1)
        listing = parsed.listings[0]
        self.assertEqual(listing.source, "auto_ru")
        self.assertEqual(listing.external_id, "1122334455")
        self.assertEqual(listing.price, 950_000)
        self.assertEqual(listing.mileage_km, 245_000)
        self.assertEqual(listing.attributes["Год выпуска"], "2001")
        self.assertEqual(len(listing.image_urls), 1)

    def test_mail_import_is_disabled_without_credentials(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AUTO_RU_IMAP_HOST": "",
                "AUTO_RU_IMAP_USERNAME": "",
                "AUTO_RU_IMAP_PASSWORD": "",
            },
        ):
            self.assertIsNone(MailImportConfig.from_env())


if __name__ == "__main__":
    unittest.main()

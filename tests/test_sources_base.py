import unittest
from unittest.mock import patch

from auto_parser.sources.base import Source, _ROBOTS_CACHE


class _Headers:
    def get_content_charset(self) -> str:
        return "utf-8"


class _Response:
    headers = _Headers()

    def __init__(self, body: bytes = b"User-agent: *\nDisallow: /private/\n") -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self, amount: int | None = None) -> bytes:
        return self.body if amount is None else self.body[:amount]


class _Source(Source):
    name = "test"
    base_url = "https://robots-test.invalid"

    def build_search_url(self, query: str) -> str:
        return self.base_url

    def parse_search_page(self, html: str):
        return []


class SourceRobotsTests(unittest.TestCase):
    def setUp(self) -> None:
        _ROBOTS_CACHE.pop("https://robots-test.invalid/robots.txt", None)

    def tearDown(self) -> None:
        _ROBOTS_CACHE.pop("https://robots-test.invalid/robots.txt", None)

    def test_fetches_robots_with_application_user_agent(self) -> None:
        source = _Source()
        user_agent = "AutoListingsResearchBot/0.1"

        with patch(
            "auto_parser.sources.base.urlopen",
            return_value=_Response(),
        ) as open_url:
            self.assertTrue(source.is_allowed(user_agent, source.base_url + "/cars/"))
            self.assertFalse(source.is_allowed(user_agent, source.base_url + "/private/item"))

        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), user_agent)
        self.assertEqual(open_url.call_count, 1)

    def test_more_specific_allow_overrides_wildcard_disallow(self) -> None:
        source = _Source()
        robots = _Response(
            b"User-agent: *\n"
            b"Disallow: */page*\n"
            b"Allow: */page2/\n"
            b"Allow: */page3/\n"
        )
        with patch("auto_parser.sources.base.urlopen", return_value=robots):
            self.assertTrue(
                source.is_allowed(
                    "AutoListingsResearchBot/0.1",
                    source.base_url + "/tver/bmw/5-series/page2/?distance=200",
                )
            )
            self.assertFalse(
                source.is_allowed(
                    "AutoListingsResearchBot/0.1",
                    source.base_url + "/tver/bmw/5-series/page11/?distance=200",
                )
            )


if __name__ == "__main__":
    unittest.main()

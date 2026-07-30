import unittest

from auto_parser.cli import build_parser


class CliTests(unittest.TestCase):
    def test_viewer_starts_scheduler_by_default(self) -> None:
        parser = build_parser()

        defaults = parser.parse_args(["--viewer"])
        self.assertTrue(defaults.scheduler)
        self.assertEqual(defaults.images_per_listing, 1)
        self.assertEqual(defaults.search_limit, 200)
        self.assertEqual(defaults.search_pages, 0)
        self.assertEqual(defaults.source, "avito")
        self.assertEqual(
            parser.parse_args(["BMW E39", "--source", "auto_ru"]).source,
            "auto_ru",
        )
        self.assertEqual(
            parser.parse_args(["BMW E39", "--source", "drom"]).source,
            "drom",
        )
        self.assertFalse(
            parser.parse_args(["--viewer", "--no-scheduler"]).scheduler
        )


if __name__ == "__main__":
    unittest.main()

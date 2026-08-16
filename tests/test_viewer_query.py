import unittest

from auto_parser.viewer_query import build_filters, integer, normalize_text


class ViewerQueryTests(unittest.TestCase):
    def test_builds_parameterized_filters(self) -> None:
        where, values = build_filters(
            {
                "brand": ["Volvo"],
                "min_price": ["1000000"],
                "year": ["2015", "2016"],
                "q": ["XC90"],
            },
            apply_visibility=True,
            default_status="active",
        )

        self.assertIn("l.hidden = 0", where)
        self.assertIn("l.price >= ?", where)
        self.assertIn("json_extract", where)
        self.assertNotIn("Volvo", where)
        self.assertEqual(
            values,
            [
                1_000_000,
                "%volvo%",
                "active",
                '$."Год выпуска"',
                "2015",
                "2016",
                "%xc90%",
                "%xc90%",
                "%xc90%",
                "%xc90%",
                "%xc90%",
                "%xc90%",
            ],
        )

    def test_source_hidden_does_not_add_default_status(self) -> None:
        where, values = build_filters(
            {"visibility": ["source_hidden"]},
            apply_visibility=True,
            default_status="active",
        )
        self.assertEqual(where, " WHERE l.status = 'hidden'")
        self.assertEqual(values, [])

    def test_supports_multiple_default_statuses_for_analytics(self) -> None:
        where, values = build_filters(
            {},
            default_statuses=("active", "removed", "active"),
        )

        self.assertEqual(where, " WHERE l.status IN (?, ?)")
        self.assertEqual(values, ["active", "removed"])

    def test_integer_clamps_to_minimum(self) -> None:
        self.assertEqual(integer("-10", minimum=1), 1)
        self.assertIsNone(integer("not-a-number"))

    def test_normalizes_legacy_city_encoding_case_insensitively(self) -> None:
        legacy_moscow = "Москва".encode("cp1251").decode("latin1")
        self.assertEqual(normalize_text(legacy_moscow), "москва")
        where, values = build_filters({"location": ["МОСКВА"]})
        self.assertIn("NORMALIZE_TEXT(l.location)", where)
        self.assertEqual(values, ["%москва%"])


if __name__ == "__main__":
    unittest.main()

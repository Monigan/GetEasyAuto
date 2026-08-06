import unittest

from auto_parser.viewer_query import build_filters, integer


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

    def test_integer_clamps_to_minimum(self) -> None:
        self.assertEqual(integer("-10", minimum=1), 1)
        self.assertIsNone(integer("not-a-number"))


if __name__ == "__main__":
    unittest.main()

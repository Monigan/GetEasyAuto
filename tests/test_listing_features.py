import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from auto_parser.models import Listing
from auto_parser.storage import ListingRepository
from auto_parser.viewer import ViewerHandler


class ListingFeatureTests(unittest.TestCase):
    def test_model_analytics_favorites_notes_comparison_and_general_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "listings.db"
            cache = root / "images"
            cache.mkdir()
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id="one",
                            url="https://example.test/one",
                            title="Volvo XC90",
                            brand="Volvo",
                            model="XC90",
                            price=1_000_000,
                            mileage_km=150_000,
                            attributes={"Год выпуска": "2015"},
                        ),
                        Listing(
                            source="drom",
                            external_id="two",
                            url="https://example.test/two",
                            title="Volvo XC90",
                            brand="Volvo",
                            model="XC90",
                            price=3_000_000,
                            mileage_km=80_000,
                            attributes={"Год выпуска": "2017"},
                        ),
                        Listing(
                            source="auto_ru",
                            external_id="sold",
                            url="https://example.test/sold",
                            title="Volvo XC90",
                            brand="Volvo",
                            model="XC90",
                            price=2_000_000,
                            attributes={"Год выпуска": "2014"},
                        ),
                        Listing(
                            source="avito",
                            external_id="new-generation",
                            url="https://example.test/new-generation",
                            title="Volvo XC90",
                            brand="Volvo",
                            model="XC90",
                            price=5_000_000,
                            attributes={"Год выпуска": "2023"},
                        ),
                    ]
                )
                repository.mark_sold("auto_ru", "sold", "2026-08-01T00:00:00+00:00")
                repository.set_sold_price("auto_ru", "sold", 1_800_000)

            handler = type(
                "ListingFeaturesViewerHandler",
                (ViewerHandler,),
                {"database": database, "cache_dir": cache},
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(
                    f"{base_url}/api/listings?source=avito&external_id=one&visibility=all",
                    timeout=5,
                ) as response:
                    listing = json.load(response)["items"][0]

                user_request = Request(
                    f"{base_url}/api/listings/avito/one/user-data",
                    data=json.dumps(
                        {"favorite": True, "note": "Проверить коробку"}
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="PATCH",
                )
                with urlopen(user_request, timeout=5) as response:
                    user_data = json.load(response)
                with urlopen(
                    f"{base_url}/api/listings?favorite=1&visibility=all",
                    timeout=5,
                ) as response:
                    favorites = json.load(response)

                comparison_query = urlencode(
                    [
                        ("source", "avito"),
                        ("external_id", "one"),
                        ("source", "drom"),
                        ("external_id", "two"),
                    ]
                )
                with urlopen(
                    f"{base_url}/api/comparison?{comparison_query}", timeout=5
                ) as response:
                    comparison = json.load(response)

                analysis_request = Request(
                    f"{base_url}/api/vehicle-analysis",
                    data=json.dumps(
                        {
                            "source": "avito",
                            "external_id": "one",
                            "analysis": {
                                "model_analysis": {
                                    "summary": "Общий анализ модели",
                                    "weak_points": [],
                                }
                            },
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(analysis_request, timeout=5) as response:
                    saved_analysis = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        analytics = listing["market_analytics"]
        self.assertEqual(analytics["listings_count"], 3)
        self.assertEqual(analytics["year_from"], 2012)
        self.assertEqual(analytics["year_to"], 2018)
        self.assertEqual(analytics["active_count"], 2)
        self.assertEqual(analytics["sold_count"], 1)
        self.assertEqual(analytics["average_price"], 2_000_000)
        self.assertEqual(analytics["average_sold_price"], 1_800_000)
        self.assertEqual(analytics["price_difference_percent"], -50.0)
        self.assertTrue(user_data["favorite"])
        self.assertEqual(user_data["note"], "Проверить коробку")
        self.assertEqual(favorites["total"], 1)
        self.assertEqual(favorites["items"][0]["user_data"]["note"], "Проверить коробку")
        self.assertEqual(len(comparison["items"]), 2)
        self.assertEqual(saved_analysis["analysis_trim_name"], "")
        self.assertEqual(saved_analysis["vehicle_analysis"]["match_kind"], "model")


if __name__ == "__main__":
    unittest.main()

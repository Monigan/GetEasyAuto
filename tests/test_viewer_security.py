import base64
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from auto_parser.models import Listing, utc_now_iso
from auto_parser.storage import ListingRepository
from auto_parser.viewer import ViewerHandler
from auto_parser.viewer_security import (
    request_is_authorized,
    validate_remote_access,
)


class ViewerSecurityTests(unittest.TestCase):
    def test_remote_access_requires_flag_and_password(self) -> None:
        with self.assertRaisesRegex(ValueError, "allow-remote-viewer"):
            validate_remote_access(
                "0.0.0.0", allow_remote=False, password="secret"
            )
        with self.assertRaisesRegex(ValueError, "пароль"):
            validate_remote_access(
                "0.0.0.0", allow_remote=True, password=None
            )
        validate_remote_access(
            "0.0.0.0", allow_remote=True, password="secret"
        )

    def test_supports_basic_and_bearer_credentials(self) -> None:
        basic = base64.b64encode(b"autoscope:secret").decode("ascii")
        self.assertTrue(request_is_authorized(f"Basic {basic}", "secret"))
        self.assertTrue(request_is_authorized("Bearer secret", "secret"))
        self.assertFalse(request_is_authorized("Bearer wrong", "secret"))
        self.assertFalse(request_is_authorized("Basic invalid", "secret"))

    def test_password_protects_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "listings.db"
            cache = root / "images"
            cache.mkdir()
            with ListingRepository(database):
                pass
            handler = type(
                "ProtectedViewerHandler",
                (ViewerHandler,),
                {
                    "database": database,
                    "cache_dir": cache,
                    "viewer_password": "secret",
                },
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{base_url}/api/meta", timeout=5)
                raised.exception.close()
                credentials = base64.b64encode(
                    b"autoscope:secret"
                ).decode("ascii")
                request = Request(
                    f"{base_url}/api/meta",
                    headers={"Authorization": f"Basic {credentials}"},
                )
                with urlopen(request, timeout=5) as response:
                    payload = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertEqual(raised.exception.code, 401)
            self.assertIn("Basic", raised.exception.headers["WWW-Authenticate"])
            self.assertIn("app_version", payload)


class ViewerPaginationTests(unittest.TestCase):
    def test_sold_and_knowledge_endpoints_are_paginated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "listings.db"
            cache = root / "images"
            cache.mkdir()
            now = utc_now_iso()
            with ListingRepository(database) as repository:
                repository.upsert_many(
                    [
                        Listing(
                            source="avito",
                            external_id=str(index),
                            url=f"https://example.test/{index}",
                            title=f"BMW {index}",
                            brand="BMW",
                            model="3-Series",
                            price=500_000 + index,
                            status="sold",
                            collected_at=now,
                        )
                        for index in range(35)
                    ]
                )
                for index in range(25):
                    repository.save_vehicle_analysis(
                        "BMW",
                        "3-Series",
                        f"trim-{index:02d}",
                        {"summary": f"analysis {index}", "weak_points": []},
                        updated_at=now,
                    )
            handler = type(
                "PaginatedViewerHandler",
                (ViewerHandler,),
                {"database": database, "cache_dir": cache},
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(
                    f"{base_url}/api/sold?page=2&page_size=30", timeout=5
                ) as response:
                    sold = json.load(response)
                with urlopen(
                    f"{base_url}/api/vehicle-analyses?page=2&page_size=20",
                    timeout=5,
                ) as response:
                    knowledge = json.load(response)
                with urlopen(
                    f"{base_url}/api/vehicle-analyses?q=trim-24",
                    timeout=5,
                ) as response:
                    search = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertEqual(sold["summary"]["count"], 35)
            self.assertEqual(sold["pages"], 2)
            self.assertEqual(len(sold["items"]), 5)
            self.assertEqual(knowledge["summary"]["vehicle_groups"], 25)
            self.assertEqual(knowledge["pages"], 2)
            self.assertEqual(len(knowledge["items"]), 5)
            self.assertEqual(search["summary"]["vehicle_groups"], 1)
            self.assertEqual(search["items"][0]["trim_name"], "trim-24")


if __name__ == "__main__":
    unittest.main()

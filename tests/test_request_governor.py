import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from auto_parser.request_governor import (
    _PRIORITIES,
    RequestDeferredError,
    RequestGovernor,
)


class RequestGovernorTests(unittest.TestCase):
    def test_sources_have_independent_cooldowns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            avito = RequestGovernor(database, namespace="avito")
            auto_ru = RequestGovernor(database, namespace="auto_ru")
            avito.record_rate_limit(retry_after_seconds=60, kind="search")

            self.assertIsNotNone(avito.cooldown_until_for("search"))
            self.assertIsNone(auto_ru.cooldown_until_for("search"))

    def test_interactive_request_bypasses_background_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            governor = RequestGovernor(Path(directory) / "test.db")
            governor.record_rate_limit(
                retry_after_seconds=3600,
                kind="detail",
            )

            with governor.request("interactive_detail"):
                pass
            with governor.request("interactive_image"):
                pass
            with self.assertRaises(RequestDeferredError):
                with governor.request("detail"):
                    self.fail("regular background work must remain deferred")

    def test_card_refresh_bypasses_cooldown_until_source_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            governor = RequestGovernor(Path(directory) / "test.db")
            governor.record_rate_limit(
                retry_after_seconds=3600,
                kind="detail",
            )

            with governor.request("refresh_incomplete_detail"):
                pass
            with governor.request("refresh_incomplete_image"):
                pass
            with governor.request("refresh_stale_detail"):
                pass
            with governor.request("refresh_stale_image"):
                pass

    def test_interactive_and_incomplete_work_precedes_stale_refresh(self) -> None:
        self.assertLess(
            _PRIORITIES["interactive_detail"],
            _PRIORITIES["incomplete_detail"],
        )
        self.assertLess(
            _PRIORITIES["interactive_image"],
            _PRIORITIES["incomplete_detail"],
        )
        self.assertLess(
            _PRIORITIES["incomplete_detail"],
            _PRIORITIES["stale_detail"],
        )
        self.assertLess(
            _PRIORITIES["incomplete_image"],
            _PRIORITIES["stale_image"],
        )

    def test_hourly_budget_defers_requests_after_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            governor = RequestGovernor(Path(directory) / "test.db")
            with (
                patch(
                    "auto_parser.request_governor.HOURLY_REQUEST_BUDGET",
                    1,
                ),
                patch.dict(
                    "auto_parser.request_governor._DELAYS",
                    {"search": (0, 0), "image": (0, 0)},
                ),
            ):
                with governor.request("search"):
                    pass
                with governor.request("image"):
                    pass
                with self.assertRaises(RequestDeferredError):
                    with governor.request("search"):
                        self.fail("search budget must be independent")
                with self.assertRaises(RequestDeferredError):
                    with governor.request("image"):
                        self.fail("background budget must defer the request")

    def test_background_cooldown_does_not_block_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            governor = RequestGovernor(Path(directory) / "test.db")
            governor.record_rate_limit(
                retry_after_seconds=3600,
                kind="detail",
            )

            with governor.request("search"):
                pass
            with self.assertRaises(RequestDeferredError):
                with governor.request("image"):
                    self.fail("background request must remain deferred")

    def test_persists_cooldown_and_defers_new_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            governor = RequestGovernor(database)
            cooldown = governor.record_rate_limit(
                retry_after_seconds=3600
            )
            restored = RequestGovernor(database)

            with self.assertRaises(RequestDeferredError):
                with restored.request("search"):
                    self.fail("request must not start during cooldown")

        self.assertGreater(
            (cooldown - datetime.now(timezone.utc)).total_seconds(),
            3500,
        )


if __name__ == "__main__":
    unittest.main()

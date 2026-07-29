from __future__ import annotations

from abc import ABC, abstractmethod
import threading
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from auto_parser.models import Listing


_ROBOTS_CACHE: dict[str, RobotFileParser] = {}
_ROBOTS_CACHE_LOCK = threading.Lock()


class SourceError(RuntimeError):
    """A source could not be queried safely or parsed."""


class HttpSourceError(SourceError):
    def __init__(
        self,
        source: str,
        status_code: int,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{source} вернул HTTP {status_code}")


class Source(ABC):
    name: str
    base_url: str

    @abstractmethod
    def build_search_url(self, query: str) -> str:
        raise NotImplementedError

    def build_search_urls(self, query: str) -> list[str]:
        """Return primary and, optionally, safe fallback search URLs."""
        return [self.build_search_url(query)]

    def build_page_url(self, search_url: str, page: int) -> str | None:
        """Return a page URL, or None when the source has no pagination."""
        return search_url if page == 1 else None

    @abstractmethod
    def parse_search_page(self, html: str) -> list[Listing]:
        raise NotImplementedError

    def enrich_listing(self, listing: Listing, html: str) -> Listing:
        return listing

    def robots_url(self) -> str:
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    def is_allowed(self, user_agent: str, url: str) -> bool:
        robots_url = self.robots_url()
        with _ROBOTS_CACHE_LOCK:
            robots = _ROBOTS_CACHE.get(robots_url)
            if robots is None:
                robots = RobotFileParser()
                robots.set_url(robots_url)
                robots.read()
                _ROBOTS_CACHE[robots_url] = robots
        return robots.can_fetch(user_agent, url)

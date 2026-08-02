from __future__ import annotations

from abc import ABC, abstractmethod
import threading
from urllib.parse import quote, unquote, urlparse, urlsplit
from urllib.request import Request, urlopen
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

    def filter_search_results(self, listings: list[Listing]) -> list[Listing]:
        """Apply filters which cannot safely be sent to the source."""
        return listings

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
            request = Request(
                robots_url,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "text/plain,*/*;q=0.1",
                },
            )
            with urlopen(request, timeout=15) as response:
                body = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
            loaded = RobotFileParser()
            loaded.set_url(robots_url)
            loaded.parse(body.decode(charset, errors="replace").splitlines())
            with _ROBOTS_CACHE_LOCK:
                robots = _ROBOTS_CACHE.setdefault(robots_url, loaded)
        return _can_fetch(robots, user_agent, url)


def _can_fetch(
    robots: RobotFileParser,
    user_agent: str,
    url: str,
) -> bool:
    """Apply RFC-style rule specificity on Python versions with wildcard rules.

    Python 3.14's RobotFileParser measures the length consumed by ``*``. A
    broad rule such as ``*/page*`` can therefore incorrectly outrank the more
    specific ``Allow: */page2/`` rule used by Drom.
    """
    if robots.disallow_all:
        return False
    if robots.allow_all:
        return True
    find_entry = getattr(robots, "_find_entry", None)
    if find_entry is None:
        return robots.can_fetch(user_agent, url)
    entry = find_entry(user_agent)
    if entry is None:
        return True

    parsed = urlsplit(url)
    filename = parsed.path or "/"
    if parsed.query:
        filename += "?" + parsed.query
    filename = quote(unquote(filename, errors="surrogateescape"), errors="surrogateescape")
    best_specificity = -1
    allowed = True
    for rule in entry.rulelines:
        if not rule.applies_to(filename):
            continue
        pattern = str(rule.path)
        specificity = len(pattern.replace("*", ""))
        if specificity > best_specificity:
            best_specificity = specificity
            allowed = bool(rule.allowance)
        elif specificity == best_specificity and rule.allowance:
            allowed = True
    return allowed

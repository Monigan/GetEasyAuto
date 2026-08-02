from __future__ import annotations

import hashlib
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, OpenerDirector, Request, build_opener

from auto_parser.images import (
    deduplicate_image_urls,
    image_identity,
    is_supported_image_url,
)
from auto_parser.models import Listing
from auto_parser.models import utc_now_iso
from auto_parser.request_governor import (
    RequestDeferredError,
    RequestGovernor,
)
from auto_parser.sources.base import HttpSourceError, Source, SourceError


DEFAULT_USER_AGENT = "AutoListingsResearchBot/0.1 (respectful personal-use collector)"
logger = logging.getLogger(__name__)
IMAGE_EXTENSIONS = {
    "image/avif": ".avif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_SEARCH_LIMIT = 200
DEFAULT_SEARCH_PAGES = 0
_IMAGE_CACHE_LOCKS: dict[str, threading.Lock] = {}
_IMAGE_CACHE_LOCKS_GUARD = threading.Lock()


@dataclass(slots=True)
class SearchService:
    source: Source
    user_agent: str = DEFAULT_USER_AGENT
    timeout_seconds: float = 20.0
    min_delay_seconds: float = 2.0
    check_robots: bool = True
    governor: RequestGovernor | None = None
    _last_request_at: float = 0.0
    _last_page_url: str | None = None
    rate_limit_error: HttpSourceError | None = field(
        init=False,
        default=None,
        repr=False,
    )
    _opener: OpenerDirector = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # A single cookie-aware session mirrors normal navigation from search
        # results to a listing without spoofing a browser identity.
        self._opener = build_opener(HTTPCookieProcessor(CookieJar()))
        if self.governor is None:
            self.governor = RequestGovernor(namespace=self.source.name)

    def search(
        self,
        query: str,
        *,
        max_results: int = DEFAULT_SEARCH_LIMIT,
        max_pages: int | None = DEFAULT_SEARCH_PAGES,
        on_page: Callable[[list[Listing], int, int], None] | None = None,
    ) -> list[Listing]:
        if max_results < 1 or (max_pages is not None and max_pages < 0):
            raise ValueError(
                "Лимит объявлений должен быть больше нуля, а число страниц — неотрицательным"
            )
        page_limit = max_pages or None
        self.rate_limit_error = None
        urls = self.source.build_search_urls(query)
        logger.info(
            "Источник=%s, вариантов URL=%d, максимум объявлений=%d, страниц=%s",
            self.source.name,
            len(urls),
            max_results,
            page_limit or "все",
        )

        for attempt, url in enumerate(urls, start=1):
            logger.info(
                "Попытка %d/%d: %s",
                attempt,
                len(urls),
                _url_for_log(url),
            )
            listings: dict[str, Listing] = {}
            pages = (
                range(1, page_limit + 1)
                if page_limit is not None
                else itertools.count(1)
            )
            for page in pages:
                page_url = self.source.build_page_url(url, page)
                if page_url is None:
                    break
                logger.info(
                    "Страница выдачи %d/%s: %s",
                    page,
                    page_limit or "все",
                    _url_for_log(page_url),
                )
                try:
                    raw_page_listings = self._search_url(page_url)
                except HttpSourceError as error:
                    if listings and error.status_code in {429, 439}:
                        self.rate_limit_error = error
                        logger.warning(
                            "Собрано %d объявлений до ограничения HTTP %d; "
                            "частичный результат будет сохранён",
                            len(listings),
                            error.status_code,
                        )
                        break
                    raise
                if not raw_page_listings:
                    logger.info(
                        "Страница %d пуста; обход выдачи завершён",
                        page,
                    )
                    break
                page_listings = self.source.filter_search_results(
                    raw_page_listings
                )
                previous_count = len(listings)
                new_listings: list[Listing] = []
                for listing in page_listings:
                    if listing.external_id not in listings:
                        new_listings.append(listing)
                    listings[listing.external_id] = listing
                    if len(listings) >= max_results:
                        break
                if on_page is not None and new_listings:
                    on_page(new_listings, page, len(listings))
                logger.info(
                    "Страница %d: получено=%d, новых=%d, всего=%d",
                    page,
                    len(page_listings),
                    len(listings) - previous_count,
                    len(listings),
                )
                if (
                    len(listings) >= max_results
                    or len(listings) == previous_count
                ):
                    break
            found = list(listings.values())[:max_results]
            logger.info("Извлечено объявлений: %d", len(found))
            if found:
                return found
            if attempt < len(urls):
                logger.warning(
                    "Результатов нет, выполняется fallback через текстовый поиск"
                )
        return []

    def _search_url(self, url: str) -> list[Listing]:
        self._check_allowed(url)
        html = self._fetch_html(url, request_kind="search")
        return self.source.parse_search_page(html)

    def enrich_details(
        self, listings: list[Listing], *, limit: int
    ) -> list[Listing]:
        selected = listings[:limit]
        if not selected:
            return listings
        logger.info(
            "Переход к карточкам для описания и пробега: максимум %d",
            len(selected),
        )
        for index, listing in enumerate(selected, start=1):
            logger.info(
                "Карточка %d/%d: %s",
                index,
                len(selected),
                _url_for_log(listing.url),
            )
            try:
                self.enrich_listing_details(listing)
                logger.info(
                    "Карточка дополнена: описание=%s, пробег=%s",
                    "да" if listing.description else "нет",
                    listing.mileage_km if listing.mileage_km is not None else "нет",
                )
            except HttpSourceError as error:
                logger.warning("Не удалось загрузить карточку: %s", error)
                if error.status_code in {429, 439}:
                    logger.warning(
                        "%s ограничил запросы к карточкам; дальнейшее "
                        "обогащение остановлено",
                        self.source.name,
                    )
                    break
            except SourceError as error:
                logger.warning("Карточка пропущена: %s", error)
        return listings

    def enrich_listing_details(
        self,
        listing: Listing,
        *,
        request_kind: str = "detail",
    ) -> Listing:
        self._check_allowed(listing.url)
        html = self._fetch_html(
            listing.url,
            request_kind=request_kind,
        )
        return self.source.enrich_listing(listing, html)

    def validate_listing(self, listing: Listing) -> str:
        try:
            self._check_allowed(listing.url)
            html = self._fetch_html(
                listing.url,
                request_kind="validation",
            )
            self.source.enrich_listing(listing, html)
            if listing.status in {"inactive", "sold"}:
                listing.status = "sold"
                listing.sold_price = listing.sold_price or listing.price
                listing.sold_at = listing.sold_at or utc_now_iso()
                listing.last_validated_at = listing.sold_at
                return "sold"
            if listing.status == "hidden":
                listing.sold_price = None
                listing.sold_at = None
                listing.last_validated_at = (
                    listing.last_validated_at or utc_now_iso()
                )
                return "hidden"
            return "active"
        except HttpSourceError as error:
            if error.status_code in {404, 410}:
                listing.status = "sold"
                listing.sold_price = listing.price
                listing.sold_at = utc_now_iso()
                listing.last_validated_at = utc_now_iso()
                return "sold"
            raise

    def cache_images(
        self,
        listings: list[Listing],
        *,
        cache_dir: Path,
        listings_limit: int,
        images_per_listing: int,
        request_kind: str = "image",
    ) -> int:
        cached_count = 0
        cache_dir.mkdir(parents=True, exist_ok=True)
        selected = listings[:listings_limit]
        logger.info(
            "Кэш изображений: объявлений=%d, максимум фото на объявление=%s",
            len(selected),
            images_per_listing or "все",
        )
        for listing in selected:
            supported_urls = deduplicate_image_urls(
                listing.image_urls
            )
            image_urls = (
                supported_urls
                if images_per_listing == 0
                else supported_urls[:images_per_listing]
            )
            logger.info(
                "Объявление %s: доступно URL изображений=%d, выбрано=%d",
                listing.external_id,
                len(supported_urls),
                len(image_urls),
            )
            for image_url in image_urls:
                try:
                    local_path = self._cache_image(
                        image_url,
                        listing=listing,
                        cache_dir=cache_dir,
                        request_kind=request_kind,
                    )
                except RequestDeferredError:
                    raise
                except HttpSourceError as error:
                    logger.warning(
                        "Изображение пропущено для %s: %s",
                        listing.external_id,
                        error,
                    )
                    if error.status_code in {429, 439}:
                        raise
                    continue
                except (OSError, SourceError, URLError) as error:
                    logger.warning(
                        "Изображение пропущено для %s: %s",
                        listing.external_id,
                        error,
                    )
                    continue
                listing.cached_images[image_url] = str(local_path)
                cached_count += 1
        logger.info("Изображений доступно в кэше: %d", cached_count)
        return cached_count

    def _cache_image(
        self,
        image_url: str,
        *,
        listing: Listing,
        cache_dir: Path,
        request_kind: str = "image",
    ) -> Path:
        parsed = urlsplit(image_url)
        hostname = (parsed.hostname or "").lower()
        if not is_supported_image_url(image_url):
            raise SourceError(f"неразрешённый хост изображения: {hostname}")

        identity = image_identity(image_url)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        listing_dir = (
            cache_dir / listing.external_id
            if listing.source == "avito"
            else cache_dir / listing.source / listing.external_id
        )
        lock_key = str((listing_dir / digest).resolve())
        with _IMAGE_CACHE_LOCKS_GUARD:
            image_lock = _IMAGE_CACHE_LOCKS.setdefault(
                lock_key,
                threading.Lock(),
            )
        with image_lock:
            return self._cache_image_locked(
                image_url,
                listing=listing,
                listing_dir=listing_dir,
                digest=digest,
                request_kind=request_kind,
            )

    def _cache_image_locked(
        self,
        image_url: str,
        *,
        listing: Listing,
        listing_dir: Path,
        digest: str,
        request_kind: str,
    ) -> Path:
        existing = (
            next(
                (
                    path
                    for path in listing_dir.glob(f"{digest}.*")
                    if path.suffix != ".tmp"
                ),
                None,
            )
            if listing_dir.exists()
            else None
        )
        if existing:
            logger.debug("Изображение уже в кэше: %s", existing)
            return existing

        self._respect_delay()
        request = Request(
            image_url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*",
                "Referer": listing.url,
            },
        )
        try:
            assert self.governor is not None
            with self.governor.request(request_kind):
                with self._opener.open(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    content_type = (
                        response.headers.get_content_type().lower()
                    )
                    extension = IMAGE_EXTENSIONS.get(content_type)
                    if extension is None:
                        raise SourceError(
                            f"неподдерживаемый Content-Type: {content_type}"
                        )
                    content_length = response.headers.get("Content-Length")
                    if (
                        content_length
                        and int(content_length) > MAX_IMAGE_BYTES
                    ):
                        raise SourceError(
                            "изображение превышает лимит 10 МБ"
                        )
                    body = response.read(MAX_IMAGE_BYTES + 1)
                    if len(body) > MAX_IMAGE_BYTES:
                        raise SourceError(
                            "изображение превышает лимит 10 МБ"
                        )
            self.governor.record_success(request_kind)
        except HTTPError as error:
            retry_after = _retry_after_seconds(error)
            if error.code in {429, 439}:
                self.governor.record_rate_limit(
                    retry_after_seconds=retry_after,
                    kind=request_kind,
                )
            raise HttpSourceError(
                self.source.name,
                error.code,
                retry_after_seconds=retry_after,
            ) from error
        finally:
            self._last_request_at = time.monotonic()

        listing_dir.mkdir(parents=True, exist_ok=True)
        destination = listing_dir / f"{digest}{extension}"
        temporary = listing_dir / f"{digest}{extension}.tmp"
        temporary.write_bytes(body)
        temporary.replace(destination)
        logger.info("Изображение сохранено: %s", destination)
        return destination

    def _check_allowed(self, url: str) -> None:
        if self.check_robots:
            logger.info("Проверка robots.txt")
            try:
                allowed = self.source.is_allowed(self.user_agent, url)
            except (OSError, URLError) as error:
                raise SourceError(f"Не удалось проверить robots.txt: {error}") from error
            logger.info("robots.txt: запрос %s", "разрешён" if allowed else "запрещён")
            if not allowed:
                raise SourceError(
                    f"robots.txt источника {self.source.name} запрещает этот запрос"
                )

    def _fetch_html(
        self,
        url: str,
        *,
        request_kind: str = "detail",
    ) -> str:
        self._respect_delay()

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }
        if self._last_page_url:
            headers["Referer"] = self._last_page_url
        request = Request(url, headers=headers)
        try:
            assert self.governor is not None
            with self.governor.request(request_kind):
                with self._opener.open(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    body = response.read()
                    charset = (
                        response.headers.get_content_charset() or "utf-8"
                    )
                    hostname = (
                        urlsplit(response.geturl()).hostname or ""
                    ).lower()
                    if (
                        hostname == "avito.ru"
                        or hostname.endswith(".avito.ru")
                    ):
                        charset = "utf-8"
                    html = body.decode(
                        charset,
                        errors="replace",
                    )
                    logger.info(
                        "HTTP %s, получено %d байт, итоговый URL: %s",
                        response.status,
                        len(body),
                        _url_for_log(response.geturl()),
                    )
                    logger.debug(
                        "Content-Type=%s, charset=%s",
                        response.headers.get(
                            "Content-Type",
                            "не указан",
                        ),
                        charset,
                    )
                    self._last_page_url = response.geturl()
            self.governor.record_success(request_kind)
        except HTTPError as error:
            retry_after = _retry_after_seconds(error)
            if error.code in {429, 439}:
                self.governor.record_rate_limit(
                    retry_after_seconds=retry_after,
                    kind=request_kind,
                )
            if retry_after is not None:
                logger.warning(
                    "Источник просит повторить запрос не раньше чем через %d с",
                    retry_after,
                )
            raise HttpSourceError(
                self.source.name,
                error.code,
                retry_after_seconds=retry_after,
            ) from error
        except URLError as error:
            raise SourceError(f"Ошибка сети: {error.reason}") from error
        finally:
            self._last_request_at = time.monotonic()

        return html

    def _respect_delay(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_delay_seconds:
            delay = self.min_delay_seconds - elapsed
            logger.debug("Пауза перед запросом: %.2f с", delay)
            time.sleep(delay)


def _url_for_log(url: str) -> str:
    """Keep logs readable: context can contain hundreds of opaque characters."""
    parts = urlsplit(url)
    parameters = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "context"
    ]
    suffix = " (context скрыт)" if "context=" in parts.query else ""
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(parameters), "")
    ) + suffix


def _retry_after_seconds(error: HTTPError) -> int | None:
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return None
    try:
        return max(1, int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(
                1,
                round(
                    (retry_at - datetime.now(timezone.utc)).total_seconds()
                ),
            )
        except (TypeError, ValueError, OverflowError):
            return None

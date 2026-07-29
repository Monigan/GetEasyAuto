from __future__ import annotations

import hashlib
import json
import logging
import re
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlsplit,
    urlunsplit,
)

from auto_parser.images import deduplicate_avito_image_urls
from auto_parser.models import Listing, utc_now_iso
from auto_parser.sources.base import Source, SourceError


CAR_CATEGORY_PATHS = {
    "bmw e39": "bmw/5_seriya/e39-ASgBAgICA0Tgtg3klyjitg3UnCjqtg3yhCk",
    "toyota camry": "toyota/camry-ASgBAgICAkTgtg20mSjitg3UoCg",
    "volkswagen passat": "volkswagen/passat-ASgBAgICAkTgtg24mSjitg3SrCg",
}

_REGION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
logger = logging.getLogger(__name__)
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _StructuredDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside_json_ld = False
        self._chunks: list[str] = []
        self.documents: list[Any] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if (
            tag.lower() == "script"
            and attributes.get("type", "").lower() == "application/ld+json"
        ):
            self._inside_json_ld = True
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._inside_json_ld:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or not self._inside_json_ld:
            return
        self._inside_json_ld = False
        try:
            self.documents.append(json.loads("".join(self._chunks)))
        except json.JSONDecodeError:
            pass


class _SearchCardParser(HTMLParser):
    """Fallback parser for Avito's semantic SSR data-marker attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.current: dict[str, Any] | None = None
        self.capture: tuple[str, int] | None = None
        self.cards: list[dict[str, Any]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        marker = attributes.get("data-marker", "")
        if self.current is None:
            if marker != "item":
                return
            self.current = {
                "title": [],
                "price": [],
                "description": [],
                "location": [],
                "image_urls": [],
            }
            self.depth = 1
        elif tag not in _VOID_TAGS:
            self.depth += 1

        if self.current is None:
            return
        marker_fields = {
            "item-title": "title",
            "item-price": "price",
            "item-description": "description",
            "item-location": "location",
        }
        field = marker_fields.get(marker)
        if field:
            self.capture = (field, self.depth)

        href = attributes.get("href")
        if href and (
            marker == "item-title"
            or attributes.get("itemprop") == "url"
            or "/avtomobili/" in href
        ):
            self.current.setdefault("url", href)

        if (
            tag == "meta"
            and attributes.get("itemprop") == "price"
            and attributes.get("content")
        ):
            self.current["price_value"] = attributes["content"]

        if tag == "img":
            for image_url in _image_urls_from_attributes(attributes):
                self.current["image_urls"].append(image_url)

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.capture:
            self.current[self.capture[0]].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None or tag in _VOID_TAGS:
            return
        if self.capture and self.capture[1] == self.depth:
            self.capture = None
        self.depth -= 1
        if self.depth == 0:
            self.cards.append(self.current)
            self.current = None


class _DetailPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.capture: tuple[str, int] | None = None
        self.description_chunks: list[str] = []
        self.params_chunks: list[str] = []
        self.meta_description: str | None = None
        self.image_urls: list[str] = []
        self.date_chunks: list[str] = []
        self.views_chunks: list[str] = []
        self.attributes: dict[str, str] = {}
        self.param_item: tuple[int, list[str]] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag not in _VOID_TAGS:
            self.depth += 1

        marker = attributes.get("data-marker", "").lower()
        if "description" in marker:
            self.capture = ("description", self.depth)
        elif "total-views" in marker or marker.endswith("/views"):
            self.capture = ("views", self.depth)
            for key in ("title", "aria-label", "data-value"):
                if attributes.get(key):
                    self.views_chunks.append(str(attributes[key]))
        elif "item-date" in marker or marker.endswith("/date"):
            self.capture = ("date", self.depth)
        elif "param" in marker or "characteristic" in marker:
            self.capture = ("params", self.depth)
        elif tag == "li" and self.capture and self.capture[0] == "params":
            self.param_item = (self.depth, [])

        if tag == "meta":
            meta_name = (
                attributes.get("name") or attributes.get("property") or ""
            ).lower()
            if meta_name in {"description", "og:description"}:
                self.meta_description = attributes.get("content")
            elif meta_name == "og:image" and attributes.get("content"):
                self.image_urls.append(attributes["content"])
        elif tag == "img" and any(
            part in marker for part in ("image", "photo", "gallery")
        ):
            self.image_urls.extend(_image_urls_from_attributes(attributes))

    def handle_data(self, data: str) -> None:
        if not self.capture:
            return
        if self.capture[0] == "description":
            self.description_chunks.append(data)
        elif self.capture[0] == "params":
            self.params_chunks.append(data)
            if self.param_item:
                self.param_item[1].append(data)
        elif self.capture[0] == "views":
            self.views_chunks.append(data)
        elif self.capture[0] == "date":
            self.date_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self.param_item and self.param_item[0] == self.depth:
            chunks = [" ".join(chunk.split()) for chunk in self.param_item[1]]
            chunks = [chunk for chunk in chunks if chunk]
            if len(chunks) >= 2:
                self.attributes[chunks[0].rstrip(":")] = (
                    " ".join(chunks[1:]).lstrip(": ").strip()
                )
            elif chunks and ":" in chunks[0]:
                key, value = chunks[0].split(":", 1)
                self.attributes[key.strip()] = value.strip()
            self.param_item = None
        if self.capture and self.capture[1] == self.depth:
            self.capture = None
        self.depth -= 1


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


class AvitoSource(Source):
    name = "avito"
    base_url = "https://www.avito.ru"

    def __init__(
        self,
        *,
        region: str = "all",
        radius: int | None = None,
        search_url: str | None = None,
    ) -> None:
        normalized_region = region.strip().lower()
        if not _REGION_PATTERN.fullmatch(normalized_region):
            raise ValueError(
                "Регион должен быть slug из URL Avito, например tver или moskva"
            )
        if radius is not None and radius < 0:
            raise ValueError("Радиус поиска не может быть отрицательным")
        self.region = normalized_region
        self.radius = radius
        self.search_url = search_url.strip() if search_url else None
        self.query_brand: str | None = None
        self.query_model: str | None = None

    def build_search_url(self, query: str) -> str:
        return self.build_search_urls(query)[0]

    def build_search_urls(self, query: str) -> list[str]:
        if self.search_url:
            primary = _validate_search_url(self.search_url)
            normalized = " ".join(query.split())
            self.query_brand, self.query_model = _split_brand_model(normalized)
            if not normalized:
                return [primary]
            fallback = self._build_text_search_url(normalized)
            return [primary] if fallback == primary else [primary, fallback]

        normalized = " ".join(query.split())
        if not normalized:
            raise ValueError("Поисковый запрос не может быть пустым")
        self.query_brand, self.query_model = _split_brand_model(normalized)

        category_path = CAR_CATEGORY_PATHS.get(normalized.casefold())
        if category_path:
            path = f"/{self.region}/avtomobili/{category_path}"
            parameters: dict[str, str | int] = {"cd": 1}
        else:
            return [self._build_text_search_url(normalized)]

        if self.radius is not None:
            parameters.update(
                {
                    "localPriority": 0,
                    "radius": self.radius,
                    "searchRadius": self.radius,
                }
            )
        primary = f"{self.base_url}{path}?{urlencode(parameters)}"
        return [primary, self._build_text_search_url(normalized)]

    def _build_text_search_url(self, query: str) -> str:
        parameters: dict[str, str | int] = {"q": query, "cd": 1}
        if self.radius is not None:
            parameters.update(
                {
                    "localPriority": 0,
                    "radius": self.radius,
                    "searchRadius": self.radius,
                }
            )
        return (
            f"{self.base_url}/{self.region}/avtomobili?"
            f"{urlencode(parameters)}"
        )

    def build_page_url(self, search_url: str, page: int) -> str | None:
        if page < 1:
            raise ValueError("Номер страницы должен быть больше нуля")
        parts = urlsplit(search_url)
        parameters = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "p"
        ]
        if page > 1:
            parameters.append(("p", str(page)))
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(parameters),
                parts.fragment,
            )
        )

    def parse_search_page(self, html: str) -> list[Listing]:
        _raise_for_block_page(html)
        parser = _StructuredDataParser()
        parser.feed(html)
        logger.info("JSON-LD документов: %d", len(parser.documents))

        listings: dict[str, Listing] = {}
        for document in parser.documents:
            for item in _walk(document):
                listing = self._to_listing(item)
                if listing:
                    listings[listing.external_id] = listing

        if listings:
            logger.info("JSON-LD: распознано карточек=%d", len(listings))
            return list(listings.values())

        card_parser = _SearchCardParser()
        card_parser.feed(html)
        logger.info(
            "SSR data-marker: найдено контейнеров карточек=%d",
            len(card_parser.cards),
        )
        for card in card_parser.cards:
            listing = self._card_to_listing(card)
            if listing:
                listings[listing.external_id] = listing
        if not listings:
            title = _extract_html_title(html)
            logger.warning(
                "Объявления не распознаны; HTML title=%r, размер=%d символов",
                title,
                len(html),
            )
        return list(listings.values())

    def enrich_listing(self, listing: Listing, html: str) -> Listing:
        _raise_for_block_page(html)

        structured = _StructuredDataParser()
        structured.feed(html)
        for document in structured.documents:
            for item in _walk(document):
                detail = self._to_listing(item)
                if detail is None:
                    continue
                if detail.description:
                    listing.description = detail.description
                if detail.mileage_km is not None:
                    listing.mileage_km = detail.mileage_km
                if detail.price is not None:
                    listing.price = detail.price
                if detail.published_at:
                    listing.published_at = detail.published_at
                if detail.views_count is not None:
                    listing.views_count = detail.views_count
                if detail.attributes:
                    listing.attributes.update(detail.attributes)
                if detail.brand:
                    listing.brand = detail.brand
                if detail.model:
                    listing.model = detail.model
                break

        detail_parser = _DetailPageParser()
        detail_parser.feed(html)
        marker_description = _joined(detail_parser.description_chunks)
        if marker_description:
            listing.description = marker_description
        elif not listing.description:
            listing.description = _as_optional_text(
                detail_parser.meta_description
            )

        if listing.mileage_km is None:
            listing.mileage_km = _parse_mileage(
                _joined(detail_parser.params_chunks)
            )
        if listing.mileage_km is None:
            listing.mileage_km = _parse_mileage(listing.title)
        marker_date = _joined(detail_parser.date_chunks)
        if marker_date:
            listing.published_at = marker_date.lstrip("·• ").strip()
        views = _parse_views(_joined(detail_parser.views_chunks))
        if views is None:
            views = _extract_views_from_html(html)
        if views is not None:
            listing.views_count = views
        if listing.brand and not listing.model:
            listing.model = _model_from_title(listing.title, listing.brand)
        listing.attributes.update(detail_parser.attributes)
        hydration_images = _extract_hydration_images(
            html, listing.external_id
        )
        listing.image_urls = (
            hydration_images
            if hydration_images
            else _normalize_image_urls(
                listing.image_urls + detail_parser.image_urls
            )
        )
        listing.status = "active"
        listing.last_validated_at = utc_now_iso()
        return listing

    def _card_to_listing(self, card: dict[str, Any]) -> Listing | None:
        title = _joined(card.get("title"))
        url = card.get("url")
        if not title or not url:
            return None
        absolute_url = urljoin(self.base_url, str(url))
        if urlparse(absolute_url).netloc not in {"avito.ru", "www.avito.ru"}:
            return None
        brand, model = _brand_model(self.query_brand, self.query_model, title)
        return Listing(
            source=self.name,
            external_id=_extract_id({}, absolute_url),
            url=absolute_url,
            title=title,
            price=_parse_price(card.get("price_value") or _joined(card.get("price"))),
            currency="RUB",
            description=_joined(card.get("description")) or None,
            mileage_km=_parse_mileage(title),
            brand=brand,
            model=model,
            location=_joined(card.get("location")) or None,
            image_urls=_normalize_image_urls(card.get("image_urls")),
        )

    def _to_listing(self, item: dict[str, Any]) -> Listing | None:
        candidate = item.get("item") if isinstance(item.get("item"), dict) else item
        if not isinstance(candidate, dict):
            return None

        item_type = str(candidate.get("@type", "")).lower()
        url = candidate.get("url")
        title = candidate.get("name") or candidate.get("headline")
        if not url or not title or item_type not in {
            "product",
            "vehicle",
            "car",
            "offer",
        }:
            return None

        absolute_url = urljoin(self.base_url, str(url))
        if urlparse(absolute_url).netloc not in {"avito.ru", "www.avito.ru"}:
            return None

        offers = candidate.get("offers", {})
        if not isinstance(offers, dict):
            offers = {}
        price = _parse_price(offers.get("price") or candidate.get("price"))
        currency = offers.get("priceCurrency") or candidate.get("priceCurrency")

        address = candidate.get("address")
        location = None
        if isinstance(address, dict):
            location = address.get("addressLocality") or address.get("addressRegion")
        elif isinstance(address, str):
            location = address

        external_id = _extract_id(candidate, absolute_url)
        brand, model = _brand_model(
            self.query_brand,
            self.query_model,
            str(title),
            candidate,
        )
        return Listing(
            source=self.name,
            external_id=external_id,
            url=absolute_url,
            title=str(title),
            price=price,
            currency=str(currency) if currency else None,
            description=_as_optional_text(candidate.get("description")),
            mileage_km=_parse_structured_mileage(candidate),
            brand=brand,
            model=model,
            views_count=_parse_structured_views(candidate),
            location=str(location) if location else None,
            published_at=candidate.get("datePosted"),
            image_urls=_extract_structured_images(candidate),
            attributes=_extract_structured_attributes(candidate),
        )


def _parse_price(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).replace(" ", "").replace(",", ".")))
    except ValueError:
        return None


def _parse_structured_mileage(candidate: dict[str, Any]) -> int | None:
    for key in ("mileageFromOdometer", "vehicleMileage", "mileage"):
        value = candidate.get(key)
        if isinstance(value, dict):
            value = value.get("value") or value.get("name")
        mileage = _parse_mileage(value, plain_number=True)
        if mileage is not None:
            return mileage
    return _parse_mileage(candidate.get("name") or candidate.get("headline"))


def _parse_mileage(value: Any, *, plain_number: bool = False) -> int | None:
    if value is None:
        return None
    text = str(value)
    if plain_number and re.fullmatch(r"[\d\s\u00a0]+", text):
        digits = re.sub(r"\D", "", text)
        return int(digits) if digits else None
    match = re.search(
        r"(\d[\d\s\u00a0]{0,12})\s*(?:км|kilometers?)\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def _parse_views(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"(\d[\d\s\u00a0]*)", str(value))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def _parse_structured_views(candidate: dict[str, Any]) -> int | None:
    interaction = candidate.get("interactionStatistic")
    if isinstance(interaction, list):
        values = interaction
    else:
        values = [interaction]
    for value in values:
        if not isinstance(value, dict):
            continue
        count = value.get("userInteractionCount") or value.get("interactionCount")
        parsed = _parse_views(count)
        if parsed is not None:
            return parsed
    return _parse_views(candidate.get("views"))


def _extract_views_from_html(html: str) -> int | None:
    marker_match = re.search(
        r'data-marker=["\']item-view/total-views["\'][^>]*>'
        r'(?P<body>.{0,500}?)</',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if marker_match:
        text = re.sub(r"<[^>]+>", " ", marker_match.group("body"))
        parsed = _parse_views(text)
        if parsed is not None:
            return parsed
    for pattern in (
        r'"totalViews"\s*:\s*(\d+)',
        r'"total_views"\s*:\s*(\d+)',
        r'"viewsCount"\s*:\s*(\d+)',
    ):
        match = re.search(pattern, html)
        if match:
            return int(match.group(1))
    return None


def _extract_structured_attributes(candidate: dict[str, Any]) -> dict[str, str]:
    attributes: dict[str, str] = {}
    properties = candidate.get("additionalProperty")
    if isinstance(properties, dict):
        properties = [properties]
    if isinstance(properties, list):
        for item in properties:
            if not isinstance(item, dict):
                continue
            key = item.get("name")
            value = item.get("value") or item.get("valueReference")
            if key and value and not isinstance(value, (dict, list)):
                attributes[str(key)] = str(value)
    direct = {
        "Цвет": candidate.get("color"),
        "Топливо": candidate.get("fuelType"),
        "Коробка передач": candidate.get("vehicleTransmission"),
        "Кузов": candidate.get("bodyType"),
        "Год выпуска": candidate.get("productionDate"),
    }
    engine = candidate.get("vehicleEngine")
    if isinstance(engine, dict):
        direct["Двигатель"] = (
            engine.get("name")
            or engine.get("engineDisplacement")
            or engine.get("enginePower")
        )
    for key, value in direct.items():
        if value and not isinstance(value, (dict, list)):
            attributes.setdefault(key, str(value))
    return attributes


def _split_brand_model(query: str) -> tuple[str | None, str | None]:
    parts = query.split()
    if not parts:
        return None, None
    return parts[0].upper() if len(parts[0]) <= 3 else parts[0].title(), (
        " ".join(parts[1:]) or None
    )


def _brand_model(
    query_brand: str | None,
    query_model: str | None,
    title: str,
    candidate: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    candidate = candidate or {}
    brand_value = candidate.get("brand")
    if isinstance(brand_value, dict):
        brand_value = brand_value.get("name")
    model_value = candidate.get("model")
    if isinstance(model_value, dict):
        model_value = model_value.get("name")
    if brand_value:
        query_brand = str(brand_value)
    if model_value:
        query_model = str(model_value)
    if query_brand:
        return query_brand, query_model or _model_from_title(title, query_brand)
    parts = title.replace(",", " ").split()
    return (parts[0] if parts else None, parts[1] if len(parts) > 1 else None)


def _model_from_title(title: str, brand: str) -> str | None:
    normalized = title.replace(",", " ").split()
    brand_parts = brand.split()
    remaining = normalized[len(brand_parts):]
    model_parts: list[str] = []
    for part in remaining:
        if re.fullmatch(r"\d\.\d", part) or re.fullmatch(r"(?:19|20)\d{2}", part):
            break
        if part.upper() in {"AT", "MT", "CVT", "AMT"}:
            break
        model_parts.append(part)
        if len(model_parts) >= 2 and part.casefold() != "серия":
            break
    return " ".join(model_parts) or None


def _image_urls_from_attributes(attributes: dict[str, str | None]) -> list[str]:
    values: list[str] = []
    for key in ("src", "data-src"):
        if attributes.get(key):
            values.append(str(attributes[key]))
    for key in ("srcset", "data-srcset"):
        if attributes.get(key):
            values.extend(
                candidate.strip().split()[0]
                for candidate in str(attributes[key]).split(",")
                if candidate.strip()
            )
    return values


def _normalize_image_urls(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    urls: list[str] = []
    for candidate in value:
        if not isinstance(candidate, str):
            continue
        absolute = urljoin(AvitoSource.base_url, candidate.strip())
        parsed = urlparse(absolute)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme == "https"
            and (
                hostname == "img.avito.st"
                or hostname.endswith(".img.avito.st")
            )
        ):
            urls.append(absolute)
    return deduplicate_avito_image_urls(urls)


def _extract_structured_images(candidate: dict[str, Any]) -> list[str]:
    value = candidate.get("image")
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, list):
        raw = [
            item.get("url") if isinstance(item, dict) else item
            for item in value
        ]
    elif isinstance(value, dict):
        raw = [value.get("url") or value.get("contentUrl")]
    else:
        raw = []
    return _normalize_image_urls(raw)


def _extract_hydration_images(
    html: str,
    external_id: str,
) -> list[str]:
    pattern = re.compile(
        r"window\.__staticRouterHydrationData\s*=\s*"
        r"JSON\.parse\((\"(?:\\.|[^\"\\])*\")\)"
    )
    for match in pattern.finditer(html):
        try:
            serialized = json.loads(match.group(1))
            payload = json.loads(serialized)
        except (json.JSONDecodeError, TypeError):
            continue
        for candidate in _walk(payload):
            if (
                str(candidate.get("id")) != str(external_id)
                or not isinstance(candidate.get("imageUrls"), list)
            ):
                continue
            urls = []
            for image in candidate["imageUrls"]:
                if not isinstance(image, dict):
                    continue
                variants = (
                    image.get("urls")
                    if isinstance(image.get("urls"), dict)
                    else image
                )
                url = (
                    variants.get("1280x960")
                    or variants.get("640x480")
                )
                if isinstance(url, str):
                    urls.append(url)
            return _normalize_image_urls(urls)
    return []


def _as_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _joined(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return " ".join(" ".join(str(chunk).split()) for chunk in value).strip()


def _extract_html_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return _as_optional_text(match.group(1)) if match else None


def _raise_for_block_page(html: str) -> None:
    lowered = html.casefold()
    markers = (
        "доступ ограничен",
        "доступ временно заблокирован",
        'data-marker="captcha"',
        "пройдите проверку, чтобы продолжить",
    )
    if any(marker in lowered for marker in markers):
        raise SourceError(
            "Avito вернул страницу ограничения доступа/CAPTCHA. "
            "Автоматический обход не выполняется; попробуйте позже или используйте "
            "официальный API/разрешённый источник."
        )


def _validate_search_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() not in {"avito.ru", "www.avito.ru"}
        or "/avtomobili" not in parsed.path
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(
            "Ожидалась HTTPS-ссылка на раздел автомобилей сайта avito.ru"
        )
    return url


def _extract_id(candidate: dict[str, Any], url: str) -> str:
    for key in ("sku", "productID", "identifier"):
        if candidate.get(key):
            return str(candidate[key])
    tail = urlparse(url).path.rstrip("/").rsplit("_", 1)[-1]
    if tail.isdigit():
        return tail
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]

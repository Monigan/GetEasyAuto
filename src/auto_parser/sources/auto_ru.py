from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from auto_parser.images import deduplicate_image_urls
from auto_parser.models import Listing, utc_now_iso
from auto_parser.sources.base import Source, SourceError


_REGION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_OFFER_ID_PATTERN = re.compile(r"/cars/(?:used|new)/sale/[^/]+/[^/]+/(\d+)(?:-[^/]+)?/")
_GROUPED_NUMBER = r"(?:\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)"
_PRICE_PATTERN = re.compile(
    rf"(?<!\d)({_GROUPED_NUMBER})\s*(?:₽|руб(?:лей|ля|ль|\.)?)",
    re.I,
)
_MILEAGE_PATTERN = re.compile(
    rf"(?<!\d)({_GROUPED_NUMBER})\s*км",
    re.I,
)
_YEAR_PATTERN = re.compile(r"\b((?:19|20)\d{2})\b")
_POWER_PATTERN = re.compile(r"(\d+)\s*л\.с\.", re.I)
_VOLUME_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*л", re.I)
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


def _text(chunks: list[str]) -> str:
    return " ".join(" ".join(chunks).split())


def _number(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def _largest_image(attributes: dict[str, str | None]) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for item in (attributes.get("srcset") or "").split(","):
        parts = item.strip().split()
        if not parts:
            continue
        url = parts[0]
        if url.startswith("//"):
            url = "https:" + url
        width = _number(parts[1]) if len(parts) > 1 else 0
        candidates.append((width or 0, url))
    src = attributes.get("src") or attributes.get("data-src")
    if src:
        candidates.append((0, src if not src.startswith("//") else "https:" + src))
    return [max(candidates, default=(0, ""))[1]] if candidates else []


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.card: dict[str, Any] | None = None
        self.card_depth = 0
        self.capture: tuple[str, int] | None = None
        self.cards: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag not in _VOID_TAGS:
            self.depth += 1
        is_card = any(
            name.startswith("ListingItemUniversal-") and "__" not in name
            for name in classes
        )
        if self.card is None and is_card:
            self.card = {"title": [], "subtitle": [], "specs": [], "location": [], "all": [], "image_urls": []}
            self.card_depth = self.depth
        if self.card is None:
            return
        class_name = " ".join(classes)
        href = attributes.get("href")
        if tag == "a" and href and "/cars/used/sale/" in href:
            self.card.setdefault("url", urljoin("https://auto.ru", href))
        if "ListingItemTitle__link" in class_name:
            self.capture = ("title", self.depth)
        elif "ListingItemUniversalSpecs__subtitle" in class_name:
            self.capture = ("subtitle", self.depth)
        elif "ListingItemUniversalSpecs__spec" in class_name:
            self.capture = ("specs", self.depth)
            self.card["specs"].append([])
        elif "MetroListPlace__regionName" in class_name:
            self.capture = ("location", self.depth)
        if tag == "img":
            self.card["image_urls"].extend(_largest_image(attributes))

    def handle_data(self, data: str) -> None:
        if self.card is None:
            return
        self.card["all"].append(data)
        if not self.capture:
            return
        field = self.capture[0]
        if field == "specs":
            self.card["specs"][-1].append(data)
        else:
            self.card[field].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self.capture and self.capture[1] == self.depth:
            self.capture = None
        if self.card is not None and self.depth == self.card_depth:
            self.cards.append(self.card)
            self.card = None
        self.depth -= 1


class _DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.capture: tuple[str, int] | None = None
        self.title: list[str] = []
        self.description: list[str] = []
        self.date: list[str] = []
        self.views: list[str] = []
        self.location: list[str] = []
        self.image_urls: list[str] = []
        self.attributes: dict[str, str] = {}
        self.pending_label: str | None = None
        self.label_chunks: list[str] = []
        self.value_chunks: list[str] = []
        self.meta_description: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        class_name = attributes.get("class") or ""
        if tag not in _VOID_TAGS:
            self.depth += 1
        if "CardHead__title" in class_name:
            self.capture = ("title", self.depth)
        elif "CardDescription__textInner" in class_name:
            self.capture = ("description", self.depth)
        elif "CardHead__creationDate" in class_name:
            self.capture = ("date", self.depth)
        elif "CardHead__views" in class_name:
            self.capture = ("views", self.depth)
        elif "MetroListPlace__regionName" in class_name:
            self.capture = ("location", self.depth)
        elif "CardInfoSummarySimpleRow__label" in class_name or "CardInfoSummaryComplexRow__cellTitle" in class_name:
            self.label_chunks = []
            self.capture = ("label", self.depth)
        elif "CardInfoSummarySimpleRow__content" in class_name or "CardInfoSummaryComplexRow__cellValue" in class_name:
            self.value_chunks = []
            self.capture = ("value", self.depth)
        elif self.pending_label and tag == "a" and "Link_color_black" in class_name:
            self.value_chunks = []
            self.capture = ("value", self.depth)
        if tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            if key in {"description", "og:description"} and attributes.get("content"):
                self.meta_description = attributes["content"]
            if key == "og:image" and attributes.get("content"):
                self.image_urls.append(attributes["content"])
        if tag == "img" and "ImageGallery" in class_name:
            self.image_urls.extend(_largest_image(attributes))

    def handle_data(self, data: str) -> None:
        if not self.capture:
            return
        field = self.capture[0]
        if field == "label":
            self.label_chunks.append(data)
        elif field == "value":
            self.value_chunks.append(data)
        else:
            getattr(self, field).append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self.capture and self.capture[1] == self.depth:
            field = self.capture[0]
            if field == "label":
                self.pending_label = _text(self.label_chunks)
            elif field == "value" and self.pending_label:
                value = _text(self.value_chunks)
                if value:
                    self.attributes[self.pending_label] = value
                    self.pending_label = None
            self.capture = None
        self.depth -= 1


class AutoRuSource(Source):
    name = "auto_ru"
    base_url = "https://auto.ru"

    def __init__(self, *, region: str = "all", radius: int | None = None, search_url: str | None = None) -> None:
        normalized_region = region.strip().lower()
        if not _REGION_PATTERN.fullmatch(normalized_region):
            raise ValueError("Регион должен быть slug из URL Auto.ru, например tver или moskva")
        if radius is not None and radius < 0:
            raise ValueError("Радиус поиска не может быть отрицательным")
        self.region = normalized_region
        self.radius = radius
        self.search_url = search_url.strip() if search_url else None

    def build_search_url(self, query: str) -> str:
        if self.search_url:
            return _validate_search_url(self.search_url)
        normalized = " ".join(query.split())
        path = f"/{self.region}/cars/all/"
        if not normalized:
            return self.base_url + path
        return f"{self.base_url}{path}?{urlencode({'query': normalized, 'from': 'searchline'})}"

    def build_page_url(self, search_url: str, page: int) -> str | None:
        if page < 1:
            raise ValueError("Номер страницы должен быть больше нуля")
        parts = urlsplit(search_url)
        params = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "page"]
        if page > 1:
            params.append(("page", str(page)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params, doseq=True), ""))

    def parse_search_page(self, html: str) -> list[Listing]:
        if "auth.auto.ru/login" in html or "Вход на сайт" in html:
            raise SourceError("Auto.ru запросил авторизацию вместо выдачи")
        parser = _SearchParser()
        parser.feed(html)
        listings: list[Listing] = []
        seen: set[str] = set()
        for card in parser.cards:
            url = str(card.get("url") or "")
            match = _OFFER_ID_PATTERN.search(url)
            title = _text(card["title"])
            if not match or not title or match.group(1) in seen:
                continue
            seen.add(match.group(1))
            full_text = _text(card["all"])
            specs = [_text(item) for item in card["specs"] if _text(item)]
            attributes = _attributes_from_specs(specs)
            year_match = _YEAR_PATTERN.search(full_text)
            if year_match:
                attributes.setdefault("Год выпуска", year_match.group(1))
            brand, model = _brand_model_from_url(url, title)
            listings.append(
                Listing(
                    source=self.name,
                    external_id=match.group(1),
                    url=url,
                    title=title,
                    price=_number(_PRICE_PATTERN.search(full_text).group(1)) if _PRICE_PATTERN.search(full_text) else None,
                    currency="RUB",
                    mileage_km=_number(_MILEAGE_PATTERN.search(full_text).group(1)) if _MILEAGE_PATTERN.search(full_text) else None,
                    brand=brand,
                    model=model,
                    location=_text(card["location"]) or None,
                    image_urls=deduplicate_image_urls(card["image_urls"]),
                    attributes=attributes,
                )
            )
        if not listings and "ListingCars" not in html:
            raise SourceError("Auto.ru вернул страницу без распознаваемой выдачи")
        return listings

    def enrich_listing(self, listing: Listing, html: str) -> Listing:
        parser = _DetailParser()
        parser.feed(html)
        title = _text(parser.title)
        if title:
            listing.title = re.sub(r",\s*(?:19|20)\d{2}\s*$", "", title)
        description = _text(parser.description)
        if description:
            listing.description = description
        views_match = re.match(r"[\s\u00a0]*([\d\s\u00a0]+)", _text(parser.views))
        listing.views_count = (
            _number(views_match.group(1)) if views_match else listing.views_count
        )
        listing.published_at = _published_at(_text(parser.date)) or listing.published_at
        listing.location = _text(parser.location) or listing.location
        listing.image_urls = deduplicate_image_urls([*listing.image_urls, *parser.image_urls])
        listing.attributes.update(parser.attributes)
        meta = parser.meta_description or ""
        mileage = _MILEAGE_PATTERN.search(meta)
        if mileage:
            listing.mileage_km = _number(mileage.group(1))
        price = _PRICE_PATTERN.search(meta)
        if price:
            listing.price = _number(price.group(1))
            listing.currency = "RUB"
        power = _POWER_PATTERN.search(meta)
        volume = _VOLUME_PATTERN.search(meta)
        if power:
            listing.attributes["Мощность"] = f"{power.group(1)} л.с."
        if volume:
            listing.attributes["Объём двигателя"] = f"{volume.group(1).replace(',', '.')} л"
        if listing.mileage_km is not None:
            listing.attributes["Пробег"] = f"{listing.mileage_km} км"
        listing.last_validated_at = utc_now_iso()
        return listing


def _validate_search_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {"auto.ru", "www.auto.ru"}:
        raise ValueError("Поддерживаются только HTTPS-ссылки Auto.ru")
    if "/cars/" not in parsed.path or "/all/" not in parsed.path:
        raise ValueError("Ссылка должна вести на выдачу легковых автомобилей Auto.ru")
    return urlunsplit(("https", "auto.ru", parsed.path, parsed.query, ""))


def _brand_model_from_url(url: str, title: str) -> tuple[str | None, str | None]:
    parts = urlsplit(url).path.strip("/").split("/")
    try:
        sale_index = parts.index("sale")
        brand_slug, model_slug = parts[sale_index + 1:sale_index + 3]
    except (ValueError, IndexError):
        words = title.split()
        return (words[0] if words else None, words[1] if len(words) > 1 else None)
    brand = brand_slug.replace("-", " ").title()
    model = model_slug.replace("-", " ").upper() if len(model_slug) <= 3 else model_slug.replace("-", " ").title()
    return brand, model


def _attributes_from_specs(specs: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for spec in specs:
        lower = spec.casefold()
        power = _POWER_PATTERN.search(spec)
        volume = _VOLUME_PATTERN.search(spec)
        if power:
            result["Мощность"] = f"{power.group(1)} л.с."
        if volume:
            result["Объём двигателя"] = f"{volume.group(1).replace(',', '.')} л"
        if any(fuel in lower for fuel in ("бензин", "дизель", "электро", "гибрид")):
            result["Тип двигателя"] = next(fuel for fuel in ("бензин", "дизель", "электро", "гибрид") if fuel in lower).title()
        elif "привод" in lower:
            result["Привод"] = spec
        elif lower in {"автомат", "механика", "вариатор", "робот"}:
            result["Коробка передач"] = spec
        elif lower in {"седан", "хэтчбек", "универсал", "внедорожник", "лифтбек", "купе", "минивэн", "пикап"}:
            result["Тип кузова"] = spec
    return result


def _published_at(value: str) -> str | None:
    normalized = value.casefold().strip()
    now = datetime.now(timezone.utc)
    if normalized.startswith("сегодня"):
        return now.isoformat()
    if normalized.startswith("вчера"):
        return (now - timedelta(days=1)).isoformat()
    match = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", normalized)
    if not match or match.group(2) not in _MONTHS:
        return None
    year = int(match.group(3)) if match.group(3) else now.year
    result = datetime(year, _MONTHS[match.group(2)], int(match.group(1)), tzinfo=timezone.utc)
    if not match.group(3) and result > now + timedelta(days=1):
        result = result.replace(year=year - 1)
    return result.isoformat()

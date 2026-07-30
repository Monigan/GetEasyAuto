from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from auto_parser.images import deduplicate_image_urls, is_drom_image_url
from auto_parser.models import Listing, utc_now_iso
from auto_parser.sources.base import Source, SourceError


_REGION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_price_range(
    min_price: int | None,
    max_price: int | None,
) -> None:
    if min_price is not None and min_price < 0:
        raise ValueError("Минимальная цена не может быть отрицательной")
    if max_price is not None and max_price < 0:
        raise ValueError("Максимальная цена не может быть отрицательной")
    if (
        min_price is not None
        and max_price is not None
        and min_price > max_price
    ):
        raise ValueError("Минимальная цена не может быть больше максимальной")
_LISTING_ID_PATTERN = re.compile(r"/(\d+)\.html(?:$|[?#])")
_GROUPED_NUMBER = r"(?:\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)"
_PRICE_PATTERN = re.compile(rf"(?<!\d)({_GROUPED_NUMBER})\s*(?:₽|руб)", re.I)
_MILEAGE_PATTERN = re.compile(rf"(?<!\d)({_GROUPED_NUMBER})\s*км", re.I)
_YEAR_PATTERN = re.compile(r"\b((?:19|20)\d{2})\b")
_POWER_PATTERN = re.compile(r"(\d+)\s*л\.с\.", re.I)
_VOLUME_PATTERN = re.compile(r"(\d+(?:[.,]\d+)?)\s*л", re.I)
_DETAIL_DATE_PATTERN = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "source", "track", "wbr",
}
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_QUERY_PATH_ALIASES = {
    "bmw e39": ("bmw", "5-series"),
    "bmw 5 серия": ("bmw", "5-series"),
    "bmw 5 series": ("bmw", "5-series"),
    "bmw x5": ("bmw", "x5"),
    "bmw 3 серия": ("bmw", "3-series"),
    "toyota camry": ("toyota", "camry"),
    "volkswagen passat": ("volkswagen", "passat"),
    "vw passat": ("volkswagen", "passat"),
    "volvo xc90": ("volvo", "xc90"),
    "ford focus": ("ford", "focus"),
    "lada vesta": ("lada", "vesta"),
}
_BRAND_ALIASES = {"vw": "volkswagen"}


def _text(chunks: list[str]) -> str:
    return " ".join(" ".join(chunks).split())


def _number(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def _largest_image(attributes: dict[str, str | None]) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for item in (attributes.get("srcset") or "").split(","):
        parts = item.strip().split()
        if not parts:
            continue
        url = parts[0]
        multiplier = 2 if len(parts) > 1 and parts[1] == "2x" else 1
        width_match = re.search(r"gen(\d+)", url)
        width = int(width_match.group(1)) * multiplier if width_match else 0
        candidates.append((width, url))
    src = attributes.get("src") or attributes.get("data-src")
    if src:
        candidates.append((0, src))
    return [max(candidates, default=(0, ""))[1]] if candidates else []


def _canonical_listing_url(value: str) -> str:
    absolute = urljoin(DromSource.base_url, value)
    parsed = urlsplit(absolute)
    return urlunsplit(("https", "auto.drom.ru", parsed.path, "", ""))


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.card_depth = 0
        self.card: dict[str, Any] | None = None
        self.capture: tuple[str, int] | None = None
        self.cards: list[dict[str, Any]] = []
        self.ignored_depth: int | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        marker = attributes.get("data-ftid") or ""
        if tag not in _VOID_TAGS:
            self.depth += 1
        if tag in {"style", "script"}:
            self.ignored_depth = self.depth
        if self.card is None and marker == "bulls-list_bull":
            self.card = {
                "title": [], "subtitle": [], "price": [], "location": [],
                "date": [], "description_items": [], "image_urls": [],
                "image_alt": [], "status": "active",
            }
            self.card_depth = self.depth
        if self.card is None:
            return
        href = attributes.get("href")
        if tag == "a" and href and _LISTING_ID_PATTERN.search(href):
            self.card.setdefault("url", _canonical_listing_url(href))
        field_by_marker = {
            "bull_title": "title",
            "bull_subtitle": "subtitle",
            "bull_price": "price",
            "bull_location": "location",
            "bull_date": "date",
        }
        if marker in field_by_marker:
            self.capture = (field_by_marker[marker], self.depth)
        elif marker == "bull_description-item":
            self.card["description_items"].append([])
            self.capture = ("description_item", self.depth)
        elif marker == "bull_sold":
            self.card["status"] = "sold"
        if tag == "img":
            self.card["image_urls"].extend(_largest_image(attributes))
            if attributes.get("alt"):
                self.card["image_alt"].append(attributes["alt"])

    def handle_data(self, data: str) -> None:
        if (
            self.card is None
            or self.capture is None
            or self.ignored_depth is not None
        ):
            return
        field = self.capture[0]
        if field == "description_item":
            self.card["description_items"][-1].append(data)
        else:
            self.card[field].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self.capture and self.capture[1] == self.depth:
            self.capture = None
        if self.ignored_depth == self.depth:
            self.ignored_depth = None
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
        self.price: list[str] = []
        self.views: list[str] = []
        self.description: list[str] = []
        self.location: list[str] = []
        self.image_urls: list[str] = []
        self.attributes: dict[str, str] = {}
        self.label_chunks: list[str] = []
        self.value_chunks: list[str] = []
        self.pending_label: str | None = None
        self.spec_depth: int | None = None
        self.description_depth: int | None = None
        self.info_depth: int | None = None
        self.city_depth: int | None = None
        self.gallery_depth: int | None = None
        self.json_ld_chunks: list[str] = []
        self.in_json_ld = False
        self.meta_description: str | None = None
        self.ignored_depth: int | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        marker = attributes.get("data-ftid") or ""
        if tag not in _VOID_TAGS:
            self.depth += 1
        is_json_ld = tag == "script" and (
            attributes.get("type") or ""
        ).casefold() == "application/ld+json"
        if tag == "style" or (tag == "script" and not is_json_ld):
            self.ignored_depth = self.depth
        if marker == "bulletin-specifications":
            self.spec_depth = self.depth
        elif marker == "bull-page_bull-gallery_thumbnails":
            self.gallery_depth = self.depth
        elif marker == "bulletin-description":
            self.description_depth = self.depth
        elif self.description_depth is not None and marker == "info-full":
            self.info_depth = self.depth
        elif self.description_depth is not None and marker == "city":
            self.city_depth = self.depth

        if marker == "page-title" or (tag == "h1" and not self.title):
            self.capture = ("title", self.depth)
        elif marker == "bulletin-price":
            self.capture = ("price", self.depth)
        elif marker == "bull-page_bull-views":
            self.capture = ("views", self.depth)
        elif self.spec_depth is not None and marker == "property":
            self.label_chunks = []
            self.capture = ("label", self.depth)
        elif self.spec_depth is not None and marker == "value":
            self.value_chunks = []
            self.capture = ("spec_value", self.depth)
        elif self.info_depth is not None and marker == "value":
            self.capture = ("description", self.depth)
        elif self.city_depth is not None and marker == "value":
            self.capture = ("location", self.depth)

        if is_json_ld:
            self.in_json_ld = True
            self.json_ld_chunks.append("")
        if tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").casefold()
            if key in {"description", "og:description"} and attributes.get("content"):
                self.meta_description = attributes["content"]
            if key == "og:image" and attributes.get("content"):
                self.image_urls.append(attributes["content"])
        href = attributes.get("href")
        if self.gallery_depth is not None and href and is_drom_image_url(href):
            self.image_urls.append(href)
        if self.gallery_depth is not None and tag == "img":
            self.image_urls.extend(_largest_image(attributes))

    def handle_data(self, data: str) -> None:
        if self.in_json_ld and self.json_ld_chunks:
            self.json_ld_chunks[-1] += data
            return
        if self.ignored_depth is not None:
            return
        if self.capture is None:
            return
        field = self.capture[0]
        if field == "label":
            self.label_chunks.append(data)
        elif field == "spec_value":
            self.value_chunks.append(data)
        else:
            getattr(self, field).append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if tag == "script" and self.in_json_ld:
            self.in_json_ld = False
        if self.ignored_depth == self.depth:
            self.ignored_depth = None
        if self.capture and self.capture[1] == self.depth:
            field = self.capture[0]
            if field == "label":
                self.pending_label = _text(self.label_chunks)
            elif field == "spec_value" and self.pending_label:
                value = re.sub(r",?\s*налог\s*$", "", _text(self.value_chunks), flags=re.I)
                if value:
                    self.attributes[self.pending_label] = value
                self.pending_label = None
            self.capture = None
        if self.spec_depth == self.depth:
            self.spec_depth = None
        if self.info_depth == self.depth:
            self.info_depth = None
        if self.city_depth == self.depth:
            self.city_depth = None
        if self.gallery_depth == self.depth:
            self.gallery_depth = None
        if self.description_depth == self.depth:
            self.description_depth = None
        self.depth -= 1


class DromSource(Source):
    name = "drom"
    base_url = "https://auto.drom.ru"

    def __init__(
        self,
        *,
        region: str = "all",
        radius: int | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        search_url: str | None = None,
    ) -> None:
        normalized_region = region.strip().lower()
        if not _REGION_PATTERN.fullmatch(normalized_region):
            raise ValueError(
                "Регион должен быть slug из URL Drom, например tver или moskva"
            )
        if radius is not None and radius < 0:
            raise ValueError("Радиус поиска не может быть отрицательным")
        _validate_price_range(min_price, max_price)
        self.region = normalized_region
        self.radius = radius
        self.min_price = min_price
        self.max_price = max_price
        self.search_url = search_url.strip() if search_url else None

    def build_search_url(self, query: str) -> str:
        if self.search_url:
            return self._with_price_filters(
                _validate_search_url(self.search_url)
            )
        normalized = " ".join(query.casefold().split())
        if not normalized:
            region_path = "" if self.region == "all" else f"/{self.region}"
            parameters = (
                [("distance", str(self.radius))]
                if self.radius
                else []
            )
            self._append_price_filters(parameters)
            return urlunsplit(
                (
                    "https",
                    "auto.drom.ru",
                    f"{region_path}/",
                    urlencode(parameters),
                    "",
                )
            )
        brand, model = _query_path(normalized)
        region_path = "" if self.region == "all" else f"/{self.region}"
        path = (
            f"{region_path}/{brand}/{model}/"
            if model
            else f"{region_path}/{brand}/all/"
        )
        parameters: list[tuple[str, str]] = []
        if self.radius:
            parameters.append(("distance", str(self.radius)))
        self._append_price_filters(parameters)
        query_string = urlencode(parameters)
        return urlunsplit(("https", "auto.drom.ru", path, query_string, ""))

    def _append_price_filters(
        self, parameters: list[tuple[str, str]]
    ) -> None:
        if self.min_price is not None:
            parameters.append(("minprice", str(self.min_price)))
        if self.max_price is not None:
            parameters.append(("maxprice", str(self.max_price)))

    def _with_price_filters(self, url: str) -> str:
        parts = urlsplit(url)
        parameters = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not (
                (key == "minprice" and self.min_price is not None)
                or (key == "maxprice" and self.max_price is not None)
            )
        ]
        self._append_price_filters(parameters)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(parameters), "")
        )

    def build_page_url(self, search_url: str, page: int) -> str | None:
        if page < 1:
            raise ValueError("Номер страницы должен быть больше нуля")
        parts = urlsplit(_validate_search_url(search_url))
        path = re.sub(r"/page\d+/?$", "/", parts.path)
        if page > 1:
            path = path.rstrip("/") + f"/page{page}/"
        return urlunsplit(("https", "auto.drom.ru", path, parts.query, ""))

    def parse_search_page(self, html: str) -> list[Listing]:
        _raise_for_block_page(html)
        parser = _SearchParser()
        parser.feed(html)
        listings: list[Listing] = []
        seen: set[str] = set()
        for card in parser.cards:
            url = str(card.get("url") or "")
            id_match = _LISTING_ID_PATTERN.search(url)
            title = _text(card["title"])
            if not id_match or not title or id_match.group(1) in seen:
                continue
            seen.add(id_match.group(1))
            description_items = [
                _text(chunks).rstrip(",")
                for chunks in card["description_items"]
                if _text(chunks)
            ]
            attributes = _attributes_from_specs(description_items)
            subtitle = _text(card["subtitle"])
            if subtitle:
                attributes["Комплектация"] = subtitle
            year = _YEAR_PATTERN.search(title)
            if year:
                attributes["Год выпуска"] = year.group(1)
            body_type = _body_type_from_alt(_text(card["image_alt"]))
            if body_type:
                attributes["Тип кузова"] = body_type
            mileage = next(
                (
                    _number(mileage_match.group(1))
                    for item in description_items
                    if (mileage_match := _MILEAGE_PATTERN.search(item))
                ),
                None,
            )
            brand, model = _brand_model(title, url)
            listings.append(
                Listing(
                    source=self.name,
                    external_id=id_match.group(1),
                    url=url,
                    title=title,
                    price=_number(_text(card["price"])),
                    currency="RUB",
                    mileage_km=mileage,
                    brand=brand,
                    model=model,
                    location=_text(card["location"]) or None,
                    published_at=_published_at(_text(card["date"])),
                    status=str(card["status"]),
                    image_urls=deduplicate_image_urls(card["image_urls"]),
                    attributes=attributes,
                )
            )
        if not listings and "bulls-list_bull" not in html and not any(
            phrase in html.casefold()
            for phrase in ("объявлений не найдено", "ничего не найдено")
        ):
            raise SourceError("Drom вернул страницу без распознаваемой выдачи")
        return listings

    def enrich_listing(self, listing: Listing, html: str) -> Listing:
        _raise_for_block_page(html)
        parser = _DetailParser()
        parser.feed(html)
        car_data = _car_json_ld(parser.json_ld_chunks)

        title = _normalize_detail_title(_text(parser.title))
        if title:
            listing.title = title
        elif car_data.get("name"):
            year = str(car_data.get("vehicleModelDate") or "").strip()
            listing.title = " ".join(
                filter(None, (str(car_data["name"]).strip(), year))
            )
        description = _text(parser.description)
        if description:
            listing.description = description
        elif car_data.get("description") and not listing.description:
            listing.description = str(car_data["description"])
        location = _text(parser.location)
        if location:
            listing.location = location

        offer = car_data.get("offers") if isinstance(car_data.get("offers"), dict) else {}
        price = _number(_text(parser.price)) or _number(offer.get("price"))
        if price is not None:
            listing.price = price
            listing.currency = str(offer.get("priceCurrency") or "RUB")
        mileage_data = car_data.get("mileageFromOdometer")
        if isinstance(mileage_data, dict):
            listing.mileage_km = _number(mileage_data.get("value")) or listing.mileage_km

        brand_data = car_data.get("brand")
        if isinstance(brand_data, dict) and brand_data.get("name"):
            listing.brand = str(brand_data["name"])
        elif isinstance(brand_data, str):
            listing.brand = brand_data
        if car_data.get("model"):
            listing.model = str(car_data["model"])

        listing.attributes.update(parser.attributes)
        mileage_value = listing.attributes.get("Пробег")
        if mileage_value and (match := _MILEAGE_PATTERN.search(mileage_value)):
            listing.mileage_km = _number(match.group(1))
        if listing.mileage_km is not None:
            listing.attributes["Пробег"] = f"{listing.mileage_km} км"
        year = str(car_data.get("vehicleModelDate") or "").strip()
        if year:
            listing.attributes.setdefault("Год выпуска", year)

        structured_images = _json_ld_images(car_data.get("image"))
        listing.image_urls = deduplicate_image_urls(
            [*listing.image_urls, *parser.image_urls, *structured_images]
        )
        views_text = _text(parser.views)
        views_match = re.search(
            r"\d{2}\.\d{2}\.\d{4}\s+([\d\s\u00a0]+)\s*$",
            views_text,
        )
        if views_match:
            listing.views_count = _number(views_match.group(1))
        date_match = _DETAIL_DATE_PATTERN.search(views_text)
        if date_match:
            listing.published_at = datetime(
                int(date_match.group(3)),
                int(date_match.group(2)),
                int(date_match.group(1)),
                tzinfo=timezone.utc,
            ).isoformat()
        availability = str(offer.get("availability") or "").casefold()
        if "outofstock" in availability or "снят с продажи" in html.casefold():
            listing.status = "sold"
        listing.last_validated_at = utc_now_iso()
        return listing


def _query_path(query: str) -> tuple[str, str | None]:
    if query in _QUERY_PATH_ALIASES:
        return _QUERY_PATH_ALIASES[query]
    words = query.split()
    brand = _BRAND_ALIASES.get(words[0], words[0])
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", brand):
        raise ValueError(
            "Для этого запроса Drom используйте готовую ссылку на выдачу"
        )
    if len(words) == 1:
        return brand, None
    model = "-".join(words[1:]).replace("_", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", model):
        raise ValueError(
            "Модель не удалось преобразовать в URL Drom; используйте готовую ссылку"
        )
    return brand, model


def _validate_search_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "auto.drom.ru":
        raise ValueError("Поддерживаются только HTTPS-ссылки auto.drom.ru")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if parts and re.fullmatch(r"page\d+", parts[-1]):
        parts.pop()
    if (
        len(parts) not in {0, 1, 2, 3}
        or any(part in {"addbull", "moto", "spec"} for part in parts)
        or any(part.endswith(".html") for part in parts)
    ):
        raise ValueError("Ссылка должна вести на выдачу легковых автомобилей Drom")
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    return urlunsplit(
        ("https", "auto.drom.ru", parsed.path, urlencode(parameters, doseq=True), "")
    )


def _brand_model(title: str, url: str) -> tuple[str | None, str | None]:
    title_without_year = re.sub(r",\s*(?:19|20)\d{2}\s*$", "", title).strip()
    words = title_without_year.split(maxsplit=1)
    if words:
        return words[0], words[1] if len(words) > 1 else None
    parts = urlsplit(url).path.strip("/").split("/")
    if len(parts) >= 4:
        return parts[-3].replace("-", " ").title(), parts[-2].replace("-", " ").title()
    return None, None


def _attributes_from_specs(specs: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for spec in specs:
        lower = spec.casefold().strip()
        power = _POWER_PATTERN.search(spec)
        volume = _VOLUME_PATTERN.search(spec)
        mileage = _MILEAGE_PATTERN.search(spec)
        if power:
            result["Мощность"] = f"{power.group(1)} л.с."
        if volume:
            result["Объём двигателя"] = f"{volume.group(1).replace(',', '.')} л"
        if mileage:
            result["Пробег"] = f"{_number(mileage.group(1))} км"
        if any(fuel in lower for fuel in ("бензин", "дизель", "электро", "гибрид")):
            fuel = next(
                fuel for fuel in ("бензин", "дизель", "электро", "гибрид")
                if fuel in lower
            )
            result["Тип двигателя"] = fuel.title()
        if lower in {"механика", "акпп", "автомат", "вариатор", "робот"}:
            result["Коробка передач"] = spec
        if lower in {"передний", "задний", "4wd", "полный"}:
            result["Привод"] = "Полный" if lower == "4wd" else spec
    return result


def _body_type_from_alt(value: str) -> str | None:
    lower = value.casefold()
    body_types = (
        "седан", "универсал", "хэтчбек", "лифтбек", "купе", "минивэн",
        "пикап", "кабриолет", "родстер", "suv или внедорожник",
    )
    for body_type in body_types:
        if lower.startswith(body_type):
            return body_type.capitalize()
    return None


def _published_at(value: str) -> str | None:
    normalized = value.casefold().strip()
    now = datetime.now(timezone.utc)
    match = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", normalized)
    if not match or match.group(2) not in _MONTHS:
        return None
    year = int(match.group(3)) if match.group(3) else now.year
    result = datetime(
        year, _MONTHS[match.group(2)], int(match.group(1)), tzinfo=timezone.utc
    )
    if not match.group(3) and result > now + timedelta(days=1):
        result = result.replace(year=year - 1)
    return result.isoformat()


def _normalize_detail_title(value: str) -> str | None:
    if not value:
        return None
    match = re.match(
        r"^Продажа\s+(.+?),\s*((?:19|20)\d{2})\s+год(?:а)?\b",
        value,
        flags=re.I,
    )
    if match:
        return f"{match.group(1)}, {match.group(2)}"
    return value


def _car_json_ld(chunks: list[str]) -> dict[str, Any]:
    candidates: list[Any] = []
    for chunk in chunks:
        try:
            candidates.append(json.loads(chunk))
        except (json.JSONDecodeError, TypeError):
            continue
    while candidates:
        candidate = candidates.pop(0)
        if isinstance(candidate, list):
            candidates.extend(candidate)
            continue
        if not isinstance(candidate, dict):
            continue
        kind = candidate.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(str(item).casefold() == "car" for item in kinds):
            return candidate
        graph = candidate.get("@graph")
        if isinstance(graph, list):
            candidates.extend(graph)
    return {}


def _json_ld_images(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        url = value.get("url") or value.get("contentUrl")
        return [str(url)] if url else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_json_ld_images(item))
        return result
    return []


def _raise_for_block_page(html: str) -> None:
    lowered = html.casefold()
    if any(
        marker in lowered
        for marker in (
            "доступ ограничен", "подтвердите, что вы не робот",
            "слишком много запросов",
        )
    ):
        raise SourceError("Drom вернул страницу ограничения доступа/CAPTCHA")

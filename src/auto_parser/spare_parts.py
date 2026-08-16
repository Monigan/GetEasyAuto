from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus, unquote_plus, unquote_to_bytes, urlencode, urljoin, urlparse


_NUMBER = re.compile(r"\d+")
_YEARS = re.compile(r"\b((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2})\b")
_MODEL_PATH = re.compile(r"/model/([^/?#]+)/?", re.I)
_ID_FROM_URL = re.compile(r"(?:-g|/g|/)(\d+)\.html(?:$|[?#])", re.I)
_CATEGORY_PATH = re.compile(r"/sell_spare_parts/\+/([^/]+)/model/", re.I)
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_MAIN_CATEGORIES = (
    "Система отопления и кондиционирования",
    "Двигатель и элементы двигателя",
    "Расходники и комплектующие",
    "Дополнительное оборудование",
    "Система подачи воздуха",
    "Выхлопная система",
    "Детали кузова",
    "Запчасти для ТО",
    "Топливная система",
    "Тормозная система",
    "Ходовая часть",
    "Трансмиссия",
    "Электрика",
    "Интерьер",
    "Оптика",
)
_CATEGORY_HINTS = (
    (("фонар", "фара", "оптик", "поворотник", "противотуман"), "Оптика"),
    (("тормоз", "колод", "суппорт", "диск"), "Тормозная система"),
    (("амортиз", "стойк", "рычаг", "ступиц", "подвес"), "Ходовая часть"),
    (("радиатор", "термостат", "помп", "печк", "кондиционер"), "Система отопления и кондиционирования"),
    (("топлив", "форсунк", "бензонасос", "тнвд"), "Топливная система"),
    (("генератор", "стартер", "датчик", "катушк", "электр"), "Электрика"),
    (("сцеплен", "короб", "кпп", "редуктор", "привод"), "Трансмиссия"),
    (("бампер", "крыло", "двер", "капот", "кузов", "зеркал"), "Детали кузова"),
    (("двигател", "грм", "порш", "клапан", "головк"), "Двигатель и элементы двигателя"),
)
_ANALYSIS_CATALOG_LIMIT = 80


def _classes(attributes: dict[str, str | None]) -> set[str]:
    return set((attributes.get("class") or "").split())


def _clean(chunks: list[str]) -> str:
    return " ".join(" ".join(chunks).split())


def _price(value: str) -> int | None:
    digits = "".join(_NUMBER.findall(value))
    return int(digits) if digits else None


def normalize_vehicle(value: str | None) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", str(value or "").casefold().replace("ё", "е"))


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _money_amount(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value)) if value not in {None, ""} else default
    except (TypeError, ValueError):
        return default


def is_drom_verification_page(html: str, final_url: str = "") -> bool:
    normalized = html.casefold()
    return (
        "/verify?" in final_url
        or "вы не робот?" in normalized
        or "подозрительный трафик" in normalized
        or "поставьте отметку, чтобы продолжить" in normalized
    )


@dataclass(slots=True)
class SparePartOffer:
    source: str
    external_id: str
    source_url: str
    brand: str
    model: str
    generation: int | None
    year_from: int | None
    year_to: int | None
    fuel: str | None
    engine_volume_cc: int | None
    category: str | None
    subcategory: str | None
    name: str
    description: str | None
    price: int | None
    image_url: str | None
    seller: str | None
    location: str | None


@dataclass(slots=True)
class DromPartsIndex:
    categories: list[str]
    generations: list[dict[str, int | None]]
    category_links: list[dict[str, str]]


class _DromPartsIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.capture: tuple[str, int, str] | None = None
        self.chunks: list[str] = []
        self.category_links: list[tuple[str, str]] = []
        self.generation_links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag not in _VOID_TAGS:
            self.depth += 1
        href = attributes.get("href") or ""
        if tag != "a" or not href:
            return
        if "/sell_spare_parts/+/" in href and "/model/" in href:
            self.capture = ("category", self.depth, href)
            self.chunks = []
        elif "autoPartsGeneration=" in href and "/sell_spare_parts/model/" in href:
            self.capture = ("generation", self.depth, href)
            self.chunks = []

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self.capture and self.capture[1] == self.depth:
            record = (self.capture[2], _clean(self.chunks))
            if self.capture[0] == "category":
                self.category_links.append(record)
            else:
                self.generation_links.append(record)
            self.capture = None
            self.chunks = []
        self.depth -= 1


_TRANSLITERATION = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})
_MODEL_SLUG_ALIASES = {
    ("bmw", "5 серия"): "bmw+5-series",
    ("bmw", "3 серия"): "bmw+3-series",
    ("mercedes-benz", "e-класс"): "mercedes-benz+e-class",
}
_KNOWN_GENERATIONS = {
    ("bmw", "5-series"): (
        (1, 1972, 1981), (2, 1981, 1988), (3, 1988, 1996),
        (4, 1995, 2004), (5, 2003, 2010), (6, 2009, 2017),
        (7, 2016, 2023), (8, 2023, 2030),
    ),
    ("ford", "mondeo"): (
        (1, 1993, 1996), (2, 1996, 2000), (3, 2000, 2007),
        (4, 2007, 2015), (5, 2014, 2022),
    ),
    ("opel", "vectra"): (
        (1, 1988, 1995), (2, 1995, 2003), (3, 2002, 2008),
    ),
}


def _slug_piece(value: str) -> str:
    transliterated = value.casefold().translate(_TRANSLITERATION)
    normalized = re.sub(r"[^a-z0-9-]+", " ", transliterated)
    return quote_plus(" ".join(normalized.split()), safe="-")


def drom_vehicle_slug(brand: str, model: str) -> str:
    alias = _MODEL_SLUG_ALIASES.get((brand.casefold().strip(), model.casefold().strip()))
    return alias or f"{_slug_piece(brand)}+{_slug_piece(model)}"


def _fuel_key(value: str | None) -> str | None:
    normalized = str(value or "").casefold()
    if "диз" in normalized or normalized == "diesel":
        return "diesel"
    if "бенз" in normalized or normalized == "gasoline":
        return "gasoline"
    return None


def _volume_cc(value: str | int | None) -> int | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)", str(value or ""))
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    return round(number * 1000) if number < 100 else round(number)


def _roman_number(value: str) -> int | None:
    match = re.search(r"\b([IVX]{1,5})\b", value.upper())
    if not match:
        return None
    values = {"I": 1, "V": 5, "X": 10}
    total = 0
    previous = 0
    for character in reversed(match.group(1)):
        current = values[character]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total or None


def drom_generation(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value
    number = re.search(r"\b(\d{1,2})\b", str(value or ""))
    return int(number.group(1)) if number else _roman_number(str(value or ""))


def known_drom_generation(
    brand: str,
    model: str,
    year: int | None,
) -> int | None:
    if not year:
        return None
    matches = [
        generation
        for generation, year_from, year_to in _KNOWN_GENERATIONS.get(
            (brand.casefold().strip(), model.casefold().strip()),
            (),
        )
        if year_from <= year <= year_to
    ]
    return max(matches) if matches else None


def known_generation_years(
    brand: str,
    model: str,
    generation: int | None,
) -> tuple[int | None, int | None]:
    if generation is None:
        return None, None
    for number, year_from, year_to in _KNOWN_GENERATIONS.get(
        (brand.casefold().strip(), model.casefold().strip()),
        (),
    ):
        if number == generation:
            return year_from, year_to
    return None, None


def build_drom_parts_url(
    brand: str,
    model: str,
    *,
    category: str | None = None,
    fuel: str | None = None,
    generation: int | None = None,
    engine_volume: str | int | None = None,
) -> str:
    slug = drom_vehicle_slug(brand, model)
    if category:
        try:
            encoded_category = quote(category.casefold().encode("cp1251"), safe="")
        except UnicodeEncodeError:
            encoded_category = quote(category.casefold(), safe="")
        encoded_category = encoded_category.replace("%20", "+")
        path = f"/sell_spare_parts/+/{encoded_category}/model/{slug}/"
    else:
        path = f"/sell_spare_parts/model/{slug}/"
    parameters: list[tuple[str, str]] = []
    fuel_key = _fuel_key(fuel)
    if fuel_key:
        parameters.append(("autoPartsFuel", fuel_key))
    if generation:
        parameters.append(("autoPartsGeneration", str(generation)))
    volume = _volume_cc(engine_volume)
    if volume:
        parameters.append(("autoPartsVolume", str(volume)))
    return "https://baza.drom.ru" + path + ("?" + urlencode(parameters) if parameters else "")


def drom_parts_url_with_filters(
    url: str,
    *,
    fuel: str | None = None,
    generation: int | None = None,
    engine_volume: str | int | None = None,
) -> str:
    """Preserve a Drom-provided category path and apply vehicle filters."""
    parsed = urlparse(urljoin("https://baza.drom.ru", url))
    if parsed.scheme != "https" or parsed.hostname not in {"baza.drom.ru", "www.baza.drom.ru"}:
        raise ValueError("Категория должна вести на HTTPS-страницу baza.drom.ru")
    query = parse_qs(parsed.query)
    for key in ("autoPartsFuel", "autoPartsGeneration", "autoPartsVolume"):
        query.pop(key, None)
    fuel_key = _fuel_key(fuel)
    if fuel_key:
        query["autoPartsFuel"] = [fuel_key]
    if generation:
        query["autoPartsGeneration"] = [str(generation)]
    volume = _volume_cc(engine_volume)
    if volume:
        query["autoPartsVolume"] = [str(volume)]
    parameters = [
        (key, value)
        for key, values in query.items()
        for value in values
    ]
    return parsed._replace(query=urlencode(parameters), fragment="").geturl()


def parse_drom_parts_index(html: str) -> DromPartsIndex:
    parser = _DromPartsIndexParser()
    parser.feed(html)
    categories: list[str] = []
    category_links: list[dict[str, str]] = []
    for href, text in parser.category_links:
        absolute_url = urljoin("https://baza.drom.ru", href)
        category, subcategory = _category_from_url(absolute_url)
        label = " ".join(filter(None, (category, subcategory))) or text
        if label and label not in categories:
            categories.append(label)
            category_links.append({"category": label, "url": absolute_url})
    generations: list[dict[str, int | None]] = []
    seen: set[int] = set()
    for href, text in parser.generation_links:
        generation = _price(parse_qs(urlparse(urljoin("https://baza.drom.ru", href)).query).get("autoPartsGeneration", [""])[0])
        if generation is None or generation in seen:
            continue
        seen.add(generation)
        years = _YEARS.search(text)
        generations.append({
            "generation": generation,
            "year_from": int(years.group(1)) if years else None,
            "year_to": int(years.group(2)) if years else None,
        })
    return DromPartsIndex(
        categories=categories,
        generations=generations,
        category_links=category_links,
    )


def generation_for_year(index: DromPartsIndex, year: int | None) -> int | None:
    if not year:
        return None
    exact = [
        item for item in index.generations
        if item["year_from"] and item["year_to"] and item["year_from"] <= year <= item["year_to"]
    ]
    if not exact:
        return None
    # Overlapping restylings are usually listed as the later generation.
    return max(int(item["generation"] or 0) for item in exact) or None


def _category_from_url(page_url: str) -> tuple[str | None, str | None]:
    match = _CATEGORY_PATH.search(urlparse(page_url).path)
    if not match:
        return None, None
    raw = unquote_to_bytes(match.group(1).replace("+", " "))
    for encoding in ("utf-8", "cp1251"):
        try:
            value = " ".join(raw.decode(encoding).split())
            normalized = value.casefold().replace("ё", "е")
            for main in _MAIN_CATEGORIES:
                main_normalized = main.casefold().replace("ё", "е")
                if normalized == main_normalized:
                    return main, None
                if normalized.startswith(main_normalized + " "):
                    return main, value[len(main):].strip() or None
            return value, None
        except UnicodeDecodeError:
            continue
    return None, None


def nested_categories_for_analysis(analysis: dict[str, Any] | None) -> list[str]:
    """Return precise marketplace tags, never guessed parent categories."""
    result: list[str] = []
    normalized_analysis = normalize_vehicle_analysis_parts(analysis or {})
    for point in normalized_analysis.get("weak_points") or []:
        for part in point.get("repair_parts") or []:
            marketplace_tags = part.get("marketplace_tags") or {}
            tags = marketplace_tags.get("drom") or [part.get("name")]
            if isinstance(tags, str):
                tags = [tags]
            for tag in tags:
                value = " ".join(str(tag or "").split())
                if value and value not in result:
                    result.append(value)
    return result


def analysis_parts_catalog(
    offers: list[dict[str, Any]],
    *,
    limit: int = _ANALYSIS_CATALOG_LIMIT,
    compatibility_scope: str = "vehicle_filters",
) -> dict[str, Any]:
    """Build a compact, diverse catalog snapshot suitable for an LLM prompt."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    category_counts: dict[str, int] = {}
    priced_count = 0
    for offer in offers:
        if offer.get("price") is not None:
            priced_count += 1
        key = (
            str(offer.get("category") or "Без категории"),
            str(offer.get("subcategory") or ""),
        )
        groups.setdefault(key, []).append(offer)
        category_counts[key[0]] = category_counts.get(key[0], 0) + 1

    selected: list[dict[str, Any]] = []
    active_groups = [list(group) for group in groups.values()]
    while active_groups and len(selected) < max(1, limit):
        remaining_groups: list[list[dict[str, Any]]] = []
        for group in active_groups:
            if len(selected) >= max(1, limit):
                break
            selected.append(group.pop(0))
            if group:
                remaining_groups.append(group)
        active_groups = remaining_groups

    items = []
    for offer in selected:
        description = " ".join(str(offer.get("description") or "").split())
        items.append({
            "catalog_offer_id": offer.get("id"),
            "name": offer.get("name"),
            "category": offer.get("category"),
            "subcategory": offer.get("subcategory"),
            "price_rub": offer.get("price"),
            "description": description[:180] or None,
        })
    return {
        "total_offers": len(offers),
        "included_offers": len(items),
        "priced_offers": priced_count,
        "price_min": min(
            (int(offer["price"]) for offer in offers if offer.get("price") is not None),
            default=None,
        ),
        "price_max": max(
            (int(offer["price"]) for offer in offers if offer.get("price") is not None),
            default=None,
        ),
        "category_counts": [
            {"category": category, "count": count}
            for category, count in sorted(
                category_counts.items(),
                key=lambda entry: (-entry[1], entry[0].casefold()),
            )
        ],
        "last_observed_at": max(
            (str(offer.get("observed_at")) for offer in offers if offer.get("observed_at")),
            default=None,
        ),
        "truncated": len(items) < len(offers),
        "compatibility_scope": compatibility_scope,
        "selection_policy": (
            "Разнообразная выборка по категориям и подкатегориям; "
            "это только уже импортированные совместимые предложения."
        ),
        "items": items,
    }


class _DromPartsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.page_text: list[str] = []
        self.card: dict[str, Any] | None = None
        self.card_depth: int | None = None
        self.capture: tuple[str, int] | None = None
        self.cards: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag not in _VOID_TAGS:
            self.depth += 1
        classes = _classes(attributes)
        if self.card is None and tag == "div" and "bull-item" in classes:
            self.card = {
                "external_id": attributes.get("data-bulletin-id"),
                "name": [], "price": [], "annotation": [], "seller": [],
                "location": [], "image_url": None, "source_url": None,
            }
            self.card_depth = self.depth
        if self.card is None:
            return
        if tag == "a" and "bull-item__self-link" in classes:
            self.card["source_url"] = attributes.get("href")
            self.capture = ("name", self.depth)
        elif "price-block__price" in classes or attributes.get("data-role") == "price":
            self.capture = ("price", self.depth)
        elif "searchSnippet" in classes:
            self.capture = ("annotation", self.depth)
        elif "ellipsis-text__left-side" in classes:
            self.capture = ("seller", self.depth)
        elif "bull-delivery__city" in classes:
            self.capture = ("location", self.depth)
        if tag == "img" and not self.card["image_url"]:
            self.card["image_url"] = attributes.get("src") or attributes.get("data-src")

    def handle_data(self, data: str) -> None:
        if self.card is None:
            self.page_text.append(data)
            return
        if self.capture:
            self.card[self.capture[0]].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self.capture and self.capture[1] == self.depth:
            self.capture = None
        if self.card is not None and self.card_depth == self.depth:
            self.cards.append(self.card)
            self.card = None
            self.card_depth = None
            self.capture = None
        self.depth -= 1


class _DromPartDetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.capture_depth: int | None = None
        self.description: list[str] = []
        self.meta_description: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag not in _VOID_TAGS:
            self.depth += 1
        if tag == "meta" and (attributes.get("name") or attributes.get("property") or "").casefold() in {"description", "og:description"}:
            self.meta_description = attributes.get("content") or self.meta_description
        if attributes.get("data-ftid") in {"bulletin-description", "info-full"}:
            self.capture_depth = self.depth

    def handle_data(self, data: str) -> None:
        if self.capture_depth is not None:
            self.description.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self.capture_depth == self.depth:
            self.capture_depth = None
        self.depth -= 1

    def value(self) -> str | None:
        value = _clean(self.description)
        return value or self.meta_description


def parse_drom_part_description(html: str) -> str | None:
    parser = _DromPartDetailParser()
    parser.feed(html)
    return parser.value()


def parse_drom_parts_page(html: str, page_url: str) -> list[SparePartOffer]:
    parsed_url = urlparse(page_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {"baza.drom.ru", "www.baza.drom.ru"}:
        raise ValueError("Поддерживаются только HTTPS-ссылки на baza.drom.ru")
    model_match = _MODEL_PATH.search(parsed_url.path)
    if not model_match:
        raise ValueError("Ссылка должна вести на список запчастей конкретной модели Drom")
    model_bits = unquote_plus(model_match.group(1)).split()
    if len(model_bits) < 2:
        raise ValueError("В ссылке не удалось определить марку и модель")
    brand, model = model_bits[0], " ".join(model_bits[1:])
    query = parse_qs(parsed_url.query)
    generation = _price(query.get("autoPartsGeneration", [""])[0])
    volume = _price(query.get("autoPartsVolume", [""])[0])
    fuel = query.get("autoPartsFuel", [None])[0]
    category, subcategory = _category_from_url(page_url)

    parser = _DromPartsParser()
    parser.feed(html)
    page_text = _clean(parser.page_text)
    years_match = _YEARS.search(page_text)
    year_from = int(years_match.group(1)) if years_match else None
    year_to = int(years_match.group(2)) if years_match else None
    known_year_from, known_year_to = known_generation_years(brand, model, generation)
    if known_year_from is not None and (
        year_from is None or year_to is None or year_to - year_from > 15
    ):
        year_from, year_to = known_year_from, known_year_to
    offers: list[SparePartOffer] = []
    for card in parser.cards:
        source_url = urljoin("https://baza.drom.ru", str(card.get("source_url") or ""))
        if urlparse(source_url).hostname not in {"baza.drom.ru", "www.baza.drom.ru"}:
            continue
        external_id = str(card.get("external_id") or "")
        if not external_id:
            match = _ID_FROM_URL.search(source_url)
            external_id = match.group(1) if match else ""
        name = _clean(card["name"])
        if not external_id or not name:
            continue
        annotation = _clean(card["annotation"])
        offers.append(SparePartOffer(
            source="drom",
            external_id=external_id,
            source_url=source_url,
            brand=brand,
            model=model,
            generation=generation,
            year_from=year_from,
            year_to=year_to,
            fuel=fuel,
            engine_volume_cc=volume,
            category=category,
            subcategory=subcategory,
            name=name,
            description=annotation or None,
            price=_price(_clean(card["price"])),
            image_url=str(card.get("image_url") or "") or None,
            seller=_clean(card["seller"]) or None,
            location=_clean(card["location"]) or None,
        ))
    return offers


_PART_LINK_RULES: tuple[tuple[tuple[str, ...], dict[str, Any]], ...] = (
    (("помп", "водян насос", "насос охлажда"), {
        "part_type": "water_pump",
        "search_terms": ["помпа", "водяная помпа", "водяной насос", "насос охлаждающей жидкости"],
        "exclude_terms": ["печка", "заслонка", "сервопривод", "омыватель", "топливный насос"],
    }),
    (("термостат",), {
        "part_type": "thermostat",
        "search_terms": ["термостат"],
        "exclude_terms": ["печка", "заслонка", "сервопривод"],
    }),
    (("патруб", "шланг охлажда"), {
        "part_type": "coolant_hose",
        "search_terms": ["патрубок охлаждения", "патрубок радиатора", "шланг охлаждения", "патрубок"],
        "exclude_terms": ["тормозной шланг", "топливный шланг", "воздушный патрубок"],
    }),
    (("радиатор",), {
        "part_type": "radiator",
        "search_terms": ["радиатор охлаждения", "радиатор двигателя", "радиатор"],
        "exclude_terms": [
            "радиатор печки", "радиатор отопителя", "радиатор салона",
            "радиатор кондиционера", "вентилятор радиатора", "диффузор радиатора",
            "масляный радиатор", "интеркулер",
        ],
    }),
    (("масляный фильтр", "маслянный фильтр", "фильтр масла"), {
        "part_type": "oil_filter",
        "search_terms": ["масляный фильтр", "фильтр масляный", "фильтр масла"],
        "exclude_terms": [
            "корпус масляного фильтра", "прокладка корпуса масляного фильтра",
            "крышка масляного фильтра", "масляный радиатор",
        ],
    }),
)


_SYSTEM_TAG_ALIASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("охлажд",), "система охлаждения"),
    (("смаз", "масл"), "система смазки"),
    (("тормоз",), "тормозная система"),
    (("сцеплен", "короб", "трансмисс"), "трансмиссия"),
    (("подвес", "ходов"), "ходовая часть"),
    (("топлив",), "топливная система"),
    (("электр", "зажиган"), "электрика"),
    (("кузов",), "кузов"),
)


def _system_tag(point: dict[str, Any]) -> str:
    raw = " ".join(filter(None, (point.get("system"), point.get("fault_type"))))
    normalized = raw.casefold().replace("ё", "е")
    return next(
        (tag for hints, tag in _SYSTEM_TAG_ALIASES if any(hint in normalized for hint in hints)),
        " ".join(str(point.get("system") or point.get("fault_type") or "прочее").casefold().split()),
    )


def _repair_part_from_name(name: str) -> dict[str, Any]:
    cleaned = " ".join(str(name).split())
    cleaned = re.sub(r"\bмаслянн(ый|ого|ому|ым|ом)\b", r"маслян\1", cleaned, flags=re.I)
    normalized = cleaned.casefold().replace("ё", "е")
    result: dict[str, Any] = {
        "name": cleaned,
        "part_type": "other",
        "search_terms": [cleaned],
        "exclude_terms": [],
    }
    for hints, values in _PART_LINK_RULES:
        if any(hint in normalized for hint in hints):
            result.update(deepcopy(values))
            break
    return result


def normalize_vehicle_analysis_parts(analysis: dict[str, Any]) -> dict[str, Any]:
    """Add explicit repair-part links while preserving the public JSON shape."""
    normalized_analysis = deepcopy(analysis)
    for point in normalized_analysis.get("weak_points") or []:
        if not isinstance(point, dict):
            continue
        raw_parts = point.get("repair_parts")
        if not isinstance(raw_parts, list) or not raw_parts:
            raw_parts = point.get("replacement_parts") or point.get("parts") or []
        if isinstance(raw_parts, (str, dict)):
            raw_parts = [raw_parts]
        repair_parts: list[dict[str, Any]] = []
        for raw_part in raw_parts:
            if isinstance(raw_part, str):
                part = _repair_part_from_name(raw_part)
            elif isinstance(raw_part, dict):
                part = _repair_part_from_name(str(raw_part.get("name") or ""))
                for key in (
                    "part_type", "category", "search_terms", "exclude_terms",
                    "system_tag", "part_tag", "marketplace_tags",
                    "catalog_offer_ids", "catalog_match_status",
                ):
                    if raw_part.get(key) is not None and raw_part.get(key) != "":
                        if key == "exclude_terms":
                            supplied = raw_part[key]
                            if isinstance(supplied, str):
                                supplied = [supplied]
                            part[key] = [*(part.get(key) or []), *deepcopy(supplied)]
                        else:
                            part[key] = deepcopy(raw_part[key])
            else:
                continue
            if not part["name"]:
                continue
            for key in ("search_terms", "exclude_terms"):
                values = part.get(key) or []
                if isinstance(values, str):
                    values = [values]
                part[key] = list(dict.fromkeys(
                    " ".join(str(value).split()) for value in values if str(value).strip()
                ))
            part["system_tag"] = " ".join(
                str(part.get("system_tag") or _system_tag(point)).casefold().split()
            )
            part["part_tag"] = " ".join(
                str(part.get("part_tag") or part.get("part_type") or "other").casefold().split()
            )
            marketplace_tags = part.get("marketplace_tags") or {}
            if not isinstance(marketplace_tags, dict):
                marketplace_tags = {}
            drom_tags = marketplace_tags.get("drom") or [part["name"]]
            if isinstance(drom_tags, str):
                drom_tags = [drom_tags]
            marketplace_tags["drom"] = list(dict.fromkeys(
                " ".join(str(value).split())
                for value in drom_tags if str(value).strip()
            ))
            part["marketplace_tags"] = marketplace_tags
            raw_offer_ids = part.get("catalog_offer_ids") or []
            if not isinstance(raw_offer_ids, list):
                raw_offer_ids = [raw_offer_ids]
            part["catalog_offer_ids"] = list(dict.fromkeys(
                offer_id
                for value in raw_offer_ids
                if (offer_id := _positive_integer(value)) is not None
            ))
            repair_parts.append(part)
        point["repair_parts"] = repair_parts
        point["replacement_parts"] = [part["name"] for part in repair_parts]
        point["repair_context"] = {
            "system": point.get("system"),
            "fault_type": point.get("fault_type"),
            "matching_policy": "part_name_required",
        }
    return normalized_analysis


def normalize_listing_assessment(assessment: dict[str, Any]) -> dict[str, Any]:
    """Keep one authoritative status per weak point and derive legacy ID lists."""
    normalized = deepcopy(assessment)
    raw_statuses = normalized.get("weak_point_statuses")
    if not isinstance(raw_statuses, list):
        return normalized
    statuses: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_statuses:
        if not isinstance(raw, dict):
            continue
        point_id = " ".join(str(raw.get("weak_point_id") or "").split())
        status = str(raw.get("status") or "").strip()
        if (
            not point_id
            or point_id in seen
            or status not in {
                "confirmed", "suspected", "reported_fixed", "not_mentioned",
            }
        ):
            continue
        seen.add(point_id)
        statuses.append({
            "weak_point_id": point_id,
            "status": status,
            "evidence": " ".join(str(raw.get("evidence") or "").split()) or None,
        })
    normalized["weak_point_statuses"] = statuses
    normalized["excluded_weak_point_ids"] = [
        entry["weak_point_id"]
        for entry in statuses
        if entry["status"] == "reported_fixed"
    ]
    normalized["relevant_weak_point_ids"] = [
        entry["weak_point_id"]
        for entry in statuses
        if entry["status"] in {"confirmed", "suspected"}
    ]
    return normalized


def _term_matches_haystack(term: str, haystack: str) -> bool:
    tokens = [
        normalize_vehicle(token)
        for token in re.findall(r"[a-zа-яё0-9]{3,}", term.casefold())
    ]
    if not tokens:
        return False
    stems = [token if len(token) <= 6 else token[:max(5, len(token) - 2)] for token in tokens]
    return all(token in haystack or stem in haystack for token, stem in zip(tokens, stems))


def offer_matches_repair_part(offer: dict[str, Any], repair_part: dict[str, Any]) -> bool:
    return offer_repair_part_score(offer, repair_part) is not None


def offer_repair_part_score(
    offer: dict[str, Any],
    repair_part: dict[str, Any],
) -> int | None:
    """Score an offer by where a precise part term matched."""
    title = normalize_vehicle(str(offer.get("name") or ""))
    taxonomy = normalize_vehicle(" ".join(filter(None, (
        offer.get("category"), offer.get("subcategory"),
    ))))
    description = normalize_vehicle(str(offer.get("description") or ""))
    haystack = title + taxonomy + description
    if any(
        _term_matches_haystack(str(term), haystack)
        for term in repair_part.get("exclude_terms") or []
    ):
        return None
    terms = repair_part.get("search_terms") or [repair_part.get("name")]
    scores = []
    for term in terms:
        if not term:
            continue
        if _term_matches_haystack(str(term), title):
            scores.append(100 + min(len(normalize_vehicle(str(term))), 30))
        elif _term_matches_haystack(str(term), taxonomy):
            scores.append(65)
        elif _term_matches_haystack(str(term), description):
            scores.append(25)
    if not scores:
        return None
    part_type = str(repair_part.get("part_type") or "")
    part_name = str(repair_part.get("name") or "").casefold()
    if (part_type.endswith("_kit") or "комплект" in part_name) and not (
        "комплект" in str(offer.get("name") or "").casefold()
        or "набор" in str(offer.get("name") or "").casefold()
    ):
        return None
    return max(scores)


def _offer_has_conflicting_vehicle_model(
    offer: dict[str, Any],
    brand: str,
    model: str,
) -> bool:
    """Reject an explicitly named different BMW series while allowing generic offers."""
    if normalize_vehicle(brand) != "bmw":
        return False
    title = str(offer.get("name") or "")
    explicit = re.search(r"\bBMW\s+(X\d|[1-8](?:[- ]?Series)?)\b", title, re.I)
    if not explicit:
        return False
    requested = normalize_vehicle(model)
    named = normalize_vehicle(explicit.group(1))
    requested_number = re.search(r"[1-8]", requested)
    named_number = re.search(r"[1-8]", named)
    return (
        named.startswith("x") != requested.startswith("x")
        or bool(requested_number and named_number and requested_number.group() != named_number.group())
    )


def matching_vehicle_spare_part_offers(
    repository: Any,
    item: dict[str, Any],
) -> tuple[list[dict[str, Any]], int | None, str]:
    """Load compatible offers once and report how strict the filter was."""
    attributes = item.get("attributes") or {}
    generation = drom_generation(
        attributes.get("Поколение") or attributes.get("Generation")
    ) or known_drom_generation(
        str(item.get("brand") or ""),
        str(item.get("model") or ""),
        item.get("year"),
    )
    requested_fuel = attributes.get("Тип двигателя") or attributes.get("Топливо")
    requested_volume = (
        attributes.get("Объём двигателя")
        or attributes.get("Объем двигателя")
    )
    offers = repository.matching_spare_part_offers(
        item.get("brand"),
        item.get("model"),
        year=item.get("year"),
        fuel=requested_fuel,
        engine_volume=requested_volume,
        generation=generation,
    )
    compatibility_scope = (
        "vehicle_filters"
        if requested_fuel or requested_volume
        else "model_generation_only"
    )
    if not offers:
        compatibility_scope = "model_generation_only"
        offers = repository.matching_spare_part_offers(
            item.get("brand"),
            item.get("model"),
            year=item.get("year"),
            generation=generation,
        )
    return offers, generation, compatibility_scope


def listing_spare_parts_payload(
    repository: Any,
    item: dict[str, Any],
    *,
    include_analysis_catalog: bool = False,
) -> dict[str, Any]:
    attributes = item.get("attributes") or {}
    offers, generation, compatibility_scope = matching_vehicle_spare_part_offers(
        repository,
        item,
    )
    analysis = normalize_vehicle_analysis_parts(
        (item.get("vehicle_analysis") or {}).get("data") or {}
    )
    assessment = (item.get("listing_assessment") or {}).get("data") or {}
    excluded = set(assessment.get("excluded_weak_point_ids") or [])
    relevant = set(assessment.get("relevant_weak_point_ids") or [])
    status_by_id = {
        entry.get("weak_point_id"): entry
        for entry in assessment.get("weak_point_statuses") or []
        if isinstance(entry, dict) and entry.get("weak_point_id")
    }
    has_structured_statuses = bool(status_by_id)
    matches: list[dict[str, Any]] = []
    chosen_ids: set[int] = set()
    parts_total = 0
    parts_max = 0
    labor_min = 0
    labor_max = 0
    reserve_parts_min = 0
    reserve_parts_max = 0
    reserve_labor_min = 0
    reserve_labor_max = 0
    offers_by_id = {
        offer_id: offer
        for offer in offers
        if (offer_id := _positive_integer(offer.get("id"))) is not None
    }
    for point in analysis.get("weak_points") or []:
        point_id = point.get("id")
        status_entry = status_by_id.get(point_id) or {}
        point_status = status_entry.get("status")
        if point_status not in {
            "confirmed", "suspected", "reported_fixed", "not_mentioned",
        }:
            point_status = None
        if not point_status:
            if point_id and point_id in excluded:
                point_status = "reported_fixed"
            elif relevant and point_id in relevant:
                point_status = "suspected"
            else:
                point_status = "not_mentioned"
        is_current_investment = (
            point_status in {"confirmed", "suspected"}
            if has_structured_statuses
            else not (point_id and point_id in excluded)
            and (not relevant or not point_id or point_id in relevant)
        )
        repair_parts = point.get("repair_parts") or []
        matched: list[dict[str, Any]] = []
        selected_ids: list[int] = []
        matched_repair_parts = 0
        priced_repair_parts = 0
        point_parts_total = 0
        billable_parts_total = 0
        for repair_part in repair_parts:
            requested_ids = repair_part.get("catalog_offer_ids") or []
            scored_matches = [
                {
                    **offers_by_id[offer_id],
                    "matched_repair_part": repair_part.get("name"),
                    "match_score": 1000,
                    "match_confidence": "catalog_exact",
                    "matched_by": "catalog_offer_id",
                }
                for raw_offer_id in requested_ids
                if (offer_id := _positive_integer(raw_offer_id)) in offers_by_id
                and not _offer_has_conflicting_vehicle_model(
                    offers_by_id[offer_id],
                    str(item.get("brand") or ""),
                    str(item.get("model") or ""),
                )
            ]
            if not scored_matches:
                for offer in offers:
                    if _offer_has_conflicting_vehicle_model(
                        offer,
                        str(item.get("brand") or ""),
                        str(item.get("model") or ""),
                    ):
                        continue
                    score = offer_repair_part_score(offer, repair_part)
                    if score is None:
                        continue
                    scored_matches.append({
                        **offer,
                        "matched_repair_part": repair_part.get("name"),
                        "match_score": score,
                        "match_confidence": "exact" if score >= 100 else "possible",
                        "matched_by": "search_terms",
                    })
            part_matches = sorted(
                scored_matches,
                key=lambda offer: (
                    -int(offer["match_score"]),
                    offer.get("price") is None,
                    offer.get("price") or 0,
                ),
            )[:3]
            if part_matches:
                matched_repair_parts += 1
            matched.extend(
                offer for offer in part_matches
                if offer["id"] not in {existing["id"] for existing in matched}
            )
            chosen_part = next(
                (
                    offer for offer in part_matches
                    if offer.get("price") is not None and offer["match_score"] >= 100
                ),
                None,
            )
            if chosen_part:
                priced_repair_parts += 1
                if chosen_part["id"] not in selected_ids:
                    selected_ids.append(chosen_part["id"])
                    point_parts_total += chosen_part.get("price") or 0
                if chosen_part["id"] not in chosen_ids:
                    chosen_ids.add(chosen_part["id"])
                    billable_parts_total += chosen_part.get("price") or 0
        estimated_parts_min = int(point.get("parts_cost_min") or 0)
        estimated_parts_max = int(
            point.get("parts_cost_max") or estimated_parts_min
        )
        catalog_priced = bool(repair_parts) and priced_repair_parts == len(repair_parts)
        point_parts_min = (
            billable_parts_total
            if catalog_priced
            else max(billable_parts_total, estimated_parts_min)
        )
        point_parts_max = (
            billable_parts_total
            if catalog_priced
            else max(billable_parts_total, estimated_parts_max, point_parts_min)
        )
        point_labor_min = int(point.get("labor_cost_min") or 0)
        point_labor_max = int(point.get("labor_cost_max") or point_labor_min)
        if is_current_investment:
            parts_total += point_parts_min
            parts_max += point_parts_max
            labor_min += point_labor_min
            labor_max += point_labor_max
        elif point_status != "reported_fixed":
            reserve_parts_min += point_parts_min
            reserve_parts_max += point_parts_max
            reserve_labor_min += point_labor_min
            reserve_labor_max += point_labor_max
        matches.append({
            "weak_point_id": point_id,
            "title": " — ".join(filter(None, (point.get("system"), point.get("issue")))) or "Слабое место",
            "listing_status": point_status,
            "status_evidence": status_entry.get("evidence"),
            "cost_role": (
                "current"
                if is_current_investment
                else "resolved"
                if point_status == "reported_fixed"
                else "reserve"
            ),
            "offers": matched,
            "selected_offer_id": selected_ids[0] if selected_ids else None,
            "selected_offer_ids": selected_ids,
            "repair_parts": repair_parts,
            "catalog_coverage": {
                "requested_parts": len(repair_parts),
                "matched_parts": matched_repair_parts,
                "priced_parts": priced_repair_parts,
                "complete": catalog_priced,
            },
            "parts_cost_min": point_parts_total if catalog_priced else point_parts_min,
            "parts_cost_max": point_parts_total if catalog_priced else point_parts_max,
        })
    structured_budget = assessment.get("investment_budget") or {}

    def budget_section(name: str) -> dict[str, int] | None:
        raw = structured_budget.get(name)
        if not isinstance(raw, dict) or not any(
            raw.get(key) is not None
            for key in (
                "parts_min", "parts_max", "labor_min", "labor_max",
                "total_min", "total_max",
            )
        ):
            return None
        section_parts_min = _money_amount(raw.get("parts_min"))
        section_parts_max = max(
            section_parts_min,
            _money_amount(raw.get("parts_max"), section_parts_min),
        )
        section_labor_min = _money_amount(raw.get("labor_min"))
        section_labor_max = max(
            section_labor_min,
            _money_amount(raw.get("labor_max"), section_labor_min),
        )
        calculated_min = section_parts_min + section_labor_min
        calculated_max = section_parts_max + section_labor_max
        total_min = max(calculated_min, _money_amount(raw.get("total_min")))
        return {
            "parts_min": section_parts_min,
            "parts_max": section_parts_max,
            "labor_min": section_labor_min,
            "labor_max": section_labor_max,
            "total_min": total_min,
            "total_max": max(
                calculated_max,
                _money_amount(raw.get("total_max")),
                total_min,
            ),
        }

    immediate_budget = budget_section("immediate")
    preventive_budget = budget_section("preventive")
    risk_budget = budget_section("risk_reserve")
    estimated = (
        {}
        if has_structured_statuses
        else assessment.get("parts_investment_total")
        or analysis.get("estimated_initial_investment")
        or {}
    )
    estimated_min = int(estimated.get("min") or 0) if isinstance(estimated, dict) else 0
    estimated_max = int(estimated.get("max") or estimated_min) if isinstance(estimated, dict) else estimated_min
    live_min = parts_total + labor_min
    live_max = parts_max + labor_max
    immediate_min = max(
        live_min,
        immediate_budget["total_min"] if immediate_budget else 0,
        estimated_min,
    )
    immediate_max = max(
        live_max,
        immediate_budget["total_max"] if immediate_budget else 0,
        estimated_max,
        immediate_min,
    )
    preventive_min = preventive_budget["total_min"] if preventive_budget else 0
    preventive_max = preventive_budget["total_max"] if preventive_budget else 0
    investment_min = immediate_min + preventive_min
    investment_max = max(immediate_max + preventive_max, investment_min)
    if risk_budget:
        reserve_parts_min = risk_budget["parts_min"]
        reserve_parts_max = risk_budget["parts_max"]
        reserve_labor_min = risk_budget["labor_min"]
        reserve_labor_max = risk_budget["labor_max"]
        reserve_min = risk_budget["total_min"]
        reserve_max = risk_budget["total_max"]
    else:
        reserve_min = reserve_parts_min + reserve_labor_min
        reserve_max = reserve_parts_max + reserve_labor_max
    purchase_price = int(item.get("price") or 0)
    result = {
        "offers_count": len(offers),
        "compatibility_scope": compatibility_scope,
        "compatible_offers": offers[:12],
        "matches": matches,
        "search_url": build_drom_parts_url(
            str(item.get("brand") or ""),
            str(item.get("model") or ""),
            fuel=attributes.get("Тип двигателя") or attributes.get("Топливо"),
            generation=generation,
            engine_volume=(
                attributes.get("Объём двигателя")
                or attributes.get("Объем двигателя")
            ),
        ),
        "costs": {
            "parts": parts_total,
            "parts_max": parts_max,
            "labor_min": labor_min,
            "labor_max": labor_max,
            "service_min": live_min,
            "service_max": live_max,
            "immediate_min": immediate_min,
            "immediate_max": immediate_max,
            "preventive_min": preventive_min,
            "preventive_max": preventive_max,
            "investment_min": investment_min,
            "investment_max": investment_max,
            "total_entry_min": purchase_price + investment_min,
            "total_entry_max": purchase_price + investment_max,
            "risk_reserve_parts_min": reserve_parts_min,
            "risk_reserve_parts_max": reserve_parts_max,
            "risk_reserve_labor_min": reserve_labor_min,
            "risk_reserve_labor_max": reserve_labor_max,
            "risk_reserve_min": reserve_min,
            "risk_reserve_max": reserve_max,
            "total_with_risk_reserve_min": purchase_price + investment_min + reserve_min,
            "total_with_risk_reserve_max": purchase_price + investment_max + reserve_max,
        },
    }
    if include_analysis_catalog:
        result["analysis_catalog"] = analysis_parts_catalog(
            offers,
            compatibility_scope=compatibility_scope,
        )
    return result

from urllib.parse import urlsplit

from .avito import AvitoSource
from .auto_ru import AutoRuSource
from .base import Source
from .drom import DromSource


ALL_CARS_QUERY = "__all_cars__"


def source_from_name(
    name: str,
    *,
    region: str = "all",
    radius: int | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    search_url: str | None = None,
) -> Source:
    normalized = name.strip().lower()
    if normalized == "avito":
        return AvitoSource(
            region=region,
            radius=radius,
            min_price=min_price,
            max_price=max_price,
            search_url=search_url,
        )
    if normalized in {"auto_ru", "auto.ru"}:
        return AutoRuSource(
            region=region,
            radius=radius,
            min_price=min_price,
            max_price=max_price,
            search_url=search_url,
        )
    if normalized in {"drom", "drom.ru"}:
        return DromSource(
            region=region,
            radius=radius,
            min_price=min_price,
            max_price=max_price,
            search_url=search_url,
        )
    raise ValueError(f"Неподдерживаемый источник: {name}")


def source_name_from_url(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower()
    if hostname in {"avito.ru", "www.avito.ru"}:
        return "avito"
    if hostname in {"auto.ru", "www.auto.ru"}:
        return "auto_ru"
    if hostname == "auto.drom.ru":
        return "drom"
    raise ValueError("Поддерживаются ссылки Avito, Auto.ru и Drom")


def source_label(name: str) -> str:
    normalized = name.strip().lower()
    if normalized == "avito":
        return "Avito"
    if normalized in {"auto_ru", "auto.ru"}:
        return "Auto.ru"
    if normalized in {"drom", "drom.ru"}:
        return "Drom"
    return name


__all__ = [
    "AutoRuSource",
    "ALL_CARS_QUERY",
    "AvitoSource",
    "DromSource",
    "Source",
    "source_from_name",
    "source_label",
    "source_name_from_url",
]

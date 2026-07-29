from __future__ import annotations

from textwrap import shorten

from auto_parser.models import Listing


CURRENCY_SYMBOLS = {
    "RUB": "руб.",
    "RUR": "руб.",
    "USD": "$",
    "EUR": "€",
}


def format_price(price: int | None, currency: str | None) -> str:
    if price is None:
        return "Цена не указана"
    amount = f"{price:,}".replace(",", " ")
    suffix = CURRENCY_SYMBOLS.get(currency or "", currency or "")
    return f"{amount} {suffix}".rstrip()


def format_listing(listing: Listing, *, description_width: int = 240) -> str:
    description = shorten(
        listing.description or "Описание отсутствует",
        width=description_width,
        placeholder="…",
    )
    mileage = (
        f"{listing.mileage_km:,}".replace(",", " ") + " км"
        if listing.mileage_km is not None
        else "Не указан"
    )
    lines = [
        listing.title,
        f"Цена:     {format_price(listing.price, listing.currency)}",
        f"Пробег:   {mileage}",
        f"Описание: {description}",
    ]
    if listing.location:
        lines.append(f"Место:    {listing.location}")
    if listing.published_at:
        lines.append(f"Дата:     {listing.published_at}")
    if listing.views_count is not None:
        lines.append(f"Просмотры:{listing.views_count:>9}")
    if listing.attributes:
        preview = " · ".join(
            f"{key}: {value}"
            for key, value in list(listing.attributes.items())[:4]
        )
        lines.append(f"Параметры: {preview}")
    if listing.cached_images:
        lines.append(
            f"Фото:     {len(listing.cached_images)}/"
            f"{len(listing.image_urls)} в кэше"
        )
    lines.append(f"Ссылка:   {listing.url}")
    return "\n".join(lines)

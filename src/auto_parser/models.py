from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Listing:
    source: str
    external_id: str
    url: str
    title: str
    price: int | None = None
    currency: str | None = None
    description: str | None = None
    mileage_km: int | None = None
    brand: str | None = None
    model: str | None = None
    views_count: int | None = None
    location: str | None = None
    published_at: str | None = None
    status: str = "active"
    sold_price: int | None = None
    sold_at: str | None = None
    last_validated_at: str | None = None
    collected_at: str = ""
    image_urls: list[str] = field(default_factory=list)
    cached_images: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.title = " ".join(self.title.split())
        self.url = self.url.strip()
        if self.description:
            self.description = " ".join(self.description.split())
        if self.brand:
            self.brand = " ".join(self.brand.split())
        if self.model:
            self.model = " ".join(self.model.split())
        self.attributes = {
            " ".join(str(key).split()): " ".join(str(value).split())
            for key, value in self.attributes.items()
            if str(key).strip() and str(value).strip()
        }
        self.image_urls = list(dict.fromkeys(
            url.strip() for url in self.image_urls if url.strip()
        ))
        if not self.collected_at:
            self.collected_at = utc_now_iso()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

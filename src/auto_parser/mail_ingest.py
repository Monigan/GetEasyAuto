from __future__ import annotations

import hashlib
import html
import imaplib
import logging
import os
import re
from dataclasses import dataclass
from datetime import timezone
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from auto_parser.images import deduplicate_image_urls
from auto_parser.models import Listing, utc_now_iso
from auto_parser.storage import ListingRepository


logger = logging.getLogger(__name__)
_OFFER_ID_PATTERN = re.compile(
    r"/cars/(?:used|new)/sale/[^/]+/[^/]+/(\d+)(?:-[^/]+)?/",
    re.I,
)
_PRICE_PATTERN = re.compile(
    r"(?<!\d)(\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)\s*(?:₽|руб)",
    re.I,
)
_MILEAGE_PATTERN = re.compile(
    r"(?<!\d)(\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)\s*км",
    re.I,
)
_YEAR_PATTERN = re.compile(r"\b((?:19|20)\d{2})\b")


@dataclass(frozen=True, slots=True)
class MailImportConfig:
    host: str
    username: str
    password: str
    port: int = 993
    mailbox: str = "INBOX"
    sender_filter: str = "auto.ru"
    max_messages: int = 100

    @classmethod
    def from_env(cls) -> "MailImportConfig | None":
        host = os.getenv("AUTO_RU_IMAP_HOST", "").strip()
        username = os.getenv("AUTO_RU_IMAP_USERNAME", "").strip()
        password = os.getenv("AUTO_RU_IMAP_PASSWORD", "")
        if not host or not username or not password:
            return None
        return cls(
            host=host,
            username=username,
            password=password,
            port=_environment_int("AUTO_RU_IMAP_PORT", 993, minimum=1),
            mailbox=os.getenv("AUTO_RU_IMAP_MAILBOX", "INBOX").strip() or "INBOX",
            sender_filter=(
                os.getenv("AUTO_RU_IMAP_SENDER", "auto.ru").strip() or "auto.ru"
            ),
            max_messages=_environment_int(
                "AUTO_RU_IMAP_MAX_MESSAGES", 100, minimum=1
            ),
        )


@dataclass(slots=True)
class ParsedMail:
    message_id: str
    sender: str | None
    subject: str | None
    received_at: str | None
    listings: list[Listing]


class _MailHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self.links: list[dict[str, object]] = []
        self.images: list[tuple[int, str]] = []
        self._link: dict[str, object] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self._link = {
                "href": attributes["href"],
                "start": len(self.chunks),
                "text": [],
                "images": [],
            }
        if tag == "img":
            source = attributes.get("src") or attributes.get("data-src")
            if source and source.startswith(("https://", "http://")):
                position = len(self.chunks)
                self.images.append((position, source))
                if self._link is not None:
                    self._link["images"].append(source)  # type: ignore[index]

    def handle_data(self, data: str) -> None:
        normalized = " ".join(data.split())
        if not normalized:
            return
        self.chunks.append(normalized)
        if self._link is not None:
            self._link["text"].append(normalized)  # type: ignore[index]

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            self._link["end"] = len(self.chunks)
            self.links.append(self._link)
            self._link = None


def parse_auto_ru_message(raw_message: bytes) -> ParsedMail:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    sender = _decoded_header(message.get("From"))
    subject = _decoded_header(message.get("Subject"))
    received_at = _message_date(message)
    message_id = str(message.get("Message-ID") or "").strip()
    if not message_id:
        message_id = "sha256:" + hashlib.sha256(raw_message).hexdigest()

    parser = _MailHtmlParser()
    plain_parts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/html", "text/plain"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if content_type == "text/html":
            parser.feed(str(content))
        else:
            plain_parts.append(str(content))

    listings: list[Listing] = []
    seen: set[str] = set()
    for link in parser.links:
        url = _auto_ru_listing_url(str(link.get("href") or ""))
        match = _OFFER_ID_PATTERN.search(url or "")
        if not url or not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        start = int(link.get("start") or 0)
        end = int(link.get("end") or start)
        context = " ".join(parser.chunks[max(0, start - 8):end + 12])
        anchor_text = " ".join(str(item) for item in link.get("text", []))
        title = _listing_title(anchor_text, context, url)
        price_match = _PRICE_PATTERN.search(context)
        mileage_match = _MILEAGE_PATTERN.search(context)
        year_match = _YEAR_PATTERN.search(context)
        brand, model = _brand_model(title, url)
        images = list(link.get("images", []))
        if not images and parser.images:
            nearest = min(parser.images, key=lambda item: abs(item[0] - start))
            if abs(nearest[0] - start) <= 15:
                images.append(nearest[1])
        attributes = {"Канал получения": "Письмо сохранённого поиска Auto.ru"}
        if year_match:
            attributes["Год выпуска"] = year_match.group(1)
        listings.append(
            Listing(
                source="auto_ru",
                external_id=match.group(1),
                url=url,
                title=title,
                price=_number(price_match.group(1)) if price_match else None,
                currency="RUB" if price_match else None,
                description=context[:1200] or None,
                mileage_km=(
                    _number(mileage_match.group(1)) if mileage_match else None
                ),
                brand=brand,
                model=model,
                published_at=received_at,
                image_urls=deduplicate_image_urls(images),
                attributes=attributes,
            )
        )

    if not listings and plain_parts:
        listings = _parse_plain_text("\n".join(plain_parts), received_at)
    return ParsedMail(message_id, sender, subject, received_at, listings)


class AutoRuMailImporter:
    def __init__(self, database: Path, config: MailImportConfig) -> None:
        self.database = database
        self.config = config

    def poll(self) -> int:
        imported = 0
        with imaplib.IMAP4_SSL(
            self.config.host,
            self.config.port,
            timeout=20,
        ) as client:
            client.login(self.config.username, self.config.password)
            status, _ = client.select(self.config.mailbox, readonly=True)
            if status != "OK":
                raise RuntimeError(
                    f"Не удалось открыть IMAP-папку {self.config.mailbox}"
                )
            status, data = client.uid("search", None, "ALL")
            if status != "OK" or not data:
                return 0
            uids = data[0].split()[-self.config.max_messages:]
            for uid in reversed(uids):
                status, response = client.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                raw = next(
                    (
                        item[1]
                        for item in response
                        if isinstance(item, tuple) and isinstance(item[1], bytes)
                    ),
                    None,
                )
                if raw is None:
                    continue
                parsed = parse_auto_ru_message(raw)
                if not _is_auto_ru_mail(parsed, self.config.sender_filter):
                    continue
                with ListingRepository(self.database) as repository:
                    if repository.mail_import_processed(parsed.message_id):
                        continue
                    count = repository.upsert_many(parsed.listings)
                    repository.record_mail_import(
                        message_id=parsed.message_id,
                        sender=parsed.sender,
                        subject=parsed.subject,
                        received_at=parsed.received_at,
                        processed_at=utc_now_iso(),
                        listing_count=count,
                    )
                imported += count
        if imported:
            logger.info("Из писем Auto.ru импортировано объявлений: %d", imported)
        return imported


def _is_auto_ru_mail(parsed: ParsedMail, sender_filter: str) -> bool:
    marker = sender_filter.casefold()
    headers = f"{parsed.sender or ''} {parsed.subject or ''}".casefold()
    return marker in headers or "авто.ру" in headers


def _decoded_header(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(make_header(decode_header(str(value))))
    except (LookupError, UnicodeDecodeError):
        return str(value)


def _message_date(message: Message) -> str | None:
    value = message.get("Date")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _auto_ru_listing_url(value: str) -> str | None:
    candidate = html.unescape(unquote(value.strip()))
    for _ in range(3):
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").lower()
        if hostname in {"auto.ru", "www.auto.ru"} and _OFFER_ID_PATTERN.search(
            parsed.path
        ):
            return f"https://auto.ru{parsed.path}"
        nested = [
            unquote(item)
            for _, item in parse_qsl(parsed.query, keep_blank_values=True)
            if "auto.ru" in unquote(item)
        ]
        if not nested:
            break
        candidate = nested[0]
    return None


def _listing_title(anchor_text: str, context: str, url: str) -> str:
    candidate = " ".join(anchor_text.split())
    if len(candidate) >= 4 and not candidate.lower().startswith(("смотреть", "открыть")):
        return candidate[:200]
    brand, model = _brand_model("", url)
    year = _YEAR_PATTERN.search(context)
    return " ".join(filter(None, [brand, model, year.group(1) if year else None])) or "Автомобиль Auto.ru"


def _brand_model(title: str, url: str) -> tuple[str | None, str | None]:
    parts = urlsplit(url).path.strip("/").split("/")
    try:
        sale = parts.index("sale")
        brand_slug, model_slug = parts[sale + 1:sale + 3]
        return brand_slug.replace("-", " ").title(), model_slug.replace("-", " ").title()
    except (ValueError, IndexError):
        words = title.split()
        return (words[0] if words else None, words[1] if len(words) > 1 else None)


def _parse_plain_text(text: str, received_at: str | None) -> list[Listing]:
    listings: list[Listing] = []
    for match in re.finditer(r"https?://[^\s<>]+", text):
        url = _auto_ru_listing_url(match.group())
        offer = _OFFER_ID_PATTERN.search(url or "")
        if not url or not offer:
            continue
        context = text[max(0, match.start() - 400):match.end() + 400]
        price = _PRICE_PATTERN.search(context)
        mileage = _MILEAGE_PATTERN.search(context)
        brand, model = _brand_model("", url)
        listings.append(
            Listing(
                source="auto_ru",
                external_id=offer.group(1),
                url=url,
                title=" ".join(filter(None, [brand, model])) or "Автомобиль Auto.ru",
                price=_number(price.group(1)) if price else None,
                currency="RUB" if price else None,
                mileage_km=_number(mileage.group(1)) if mileage else None,
                description=" ".join(context.split())[:1200],
                brand=brand,
                model=model,
                published_at=received_at,
                attributes={"Канал получения": "Письмо сохранённого поиска Auto.ru"},
            )
        )
    unique = {item.external_id: item for item in listings}
    return list(unique.values())


def _number(value: str) -> int | None:
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def _environment_int(name: str, default: int, *, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default

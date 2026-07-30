from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


_AVITO_ID_PATTERN = re.compile(r"^/image/1/1\.([^./?]{6})a")
_AVITO_LEGACY_QUALITY_PATTERN = re.compile(
    r"^/image/1/1\.[^./?]{6}a(\d)"
)
_AUTO_RU_SIZE_PATTERN = re.compile(r"/(\d+)x(\d+)$")
_DROM_HOST_PATTERN = re.compile(r"^s\d*\.auto\.drom\.ru$")
_DROM_PHOTO_PATTERN = re.compile(r"^/photo/v\d+/([^/]+)/")
_DROM_SIZE_PATTERN = re.compile(r"/gen(\d+)(x2)?(?:[^/]*)\.[a-z0-9]+$", re.I)


def is_avito_image_url(url: str) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and (
            hostname == "img.avito.st"
            or hostname.endswith(".img.avito.st")
        )
    )


def avito_image_identity(url: str) -> str:
    """Return an identity shared by all quality variants of one photo."""
    parsed = urlsplit(url)
    match = _AVITO_ID_PATTERN.match(parsed.path)
    if is_avito_image_url(url) and match is not None:
        return f"avito:{match.group(1)}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            "",
            "",
        )
    )


def is_auto_ru_image_url(url: str) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        hostname == "avatars.avto.ru" or hostname.endswith(".avatars.avto.ru")
    )


def is_drom_image_url(url: str) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and _DROM_HOST_PATTERN.fullmatch(hostname) is not None
        and parsed.path.startswith("/photo/")
    )


def image_identity(url: str) -> str:
    if is_avito_image_url(url):
        return avito_image_identity(url)
    parsed = urlsplit(url)
    if is_auto_ru_image_url(url):
        path = _AUTO_RU_SIZE_PATTERN.sub("", parsed.path)
        return f"auto_ru:{parsed.netloc.lower()}{path}"
    if is_drom_image_url(url):
        match = _DROM_PHOTO_PATTERN.match(parsed.path)
        if match:
            return f"drom:{match.group(1)}"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", ""))


def is_supported_image_url(url: str) -> bool:
    return (
        is_avito_image_url(url)
        or is_auto_ru_image_url(url)
        or is_drom_image_url(url)
    )


def _quality_rank(url: str) -> int:
    match = _AVITO_LEGACY_QUALITY_PATTERN.match(urlsplit(url).path)
    return int(match.group(1)) if match is not None else 0


def deduplicate_avito_image_urls(urls: list[str]) -> list[str]:
    selected: list[str] = []
    positions: dict[str, int] = {}
    qualities: dict[str, int] = {}

    for url in urls:
        if not is_avito_image_url(url):
            continue
        image_id = avito_image_identity(url)
        quality = _quality_rank(url)
        if image_id not in positions:
            positions[image_id] = len(selected)
            qualities[image_id] = quality
            selected.append(url)
        elif quality > qualities[image_id]:
            selected[positions[image_id]] = url
            qualities[image_id] = quality

    return selected


def _generic_quality_rank(url: str) -> int:
    if is_avito_image_url(url):
        return _quality_rank(url)
    if is_drom_image_url(url):
        match = _DROM_SIZE_PATTERN.search(urlsplit(url).path)
        if match:
            return int(match.group(1)) * (2 if match.group(2) else 1)
        return 1
    match = _AUTO_RU_SIZE_PATTERN.search(urlsplit(url).path)
    return int(match.group(1)) * int(match.group(2)) if match else 0


def deduplicate_image_urls(urls: list[str]) -> list[str]:
    selected: list[str] = []
    positions: dict[str, int] = {}
    qualities: dict[str, int] = {}
    for url in urls:
        if not is_supported_image_url(url):
            continue
        identity = image_identity(url)
        quality = _generic_quality_rank(url)
        if identity not in positions:
            positions[identity] = len(selected)
            qualities[identity] = quality
            selected.append(url)
        elif quality > qualities[identity]:
            selected[positions[identity]] = url
            qualities[identity] = quality
    return selected


def remap_cached_avito_images(
    selected_urls: list[str],
    cached_images: dict[str, str],
) -> dict[str, str]:
    cached_by_identity = {
        avito_image_identity(url): path
        for url, path in cached_images.items()
        if path
    }
    return {
        url: cached_by_identity[avito_image_identity(url)]
        for url in selected_urls
        if avito_image_identity(url) in cached_by_identity
    }


def remap_cached_images(
    selected_urls: list[str],
    cached_images: dict[str, str],
) -> dict[str, str]:
    cached_by_identity = {
        image_identity(url): path
        for url, path in cached_images.items()
        if path
    }
    return {
        url: cached_by_identity[image_identity(url)]
        for url in selected_urls
        if image_identity(url) in cached_by_identity
    }


def has_thumbnail_variants(urls: list[str]) -> bool:
    for url in urls:
        match = _AVITO_LEGACY_QUALITY_PATTERN.match(urlsplit(url).path)
        if match is not None and int(match.group(1)) == 3:
            return True
    return False

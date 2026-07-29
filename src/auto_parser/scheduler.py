from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auto_parser.activity import (
    finish_listing_activity,
    set_listing_activity,
)
from auto_parser.mail_ingest import AutoRuMailImporter, MailImportConfig
from auto_parser.request_governor import (
    RequestDeferredError,
    RequestGovernor,
)
from auto_parser.service import SearchService
from auto_parser.sources import source_from_name, source_label, source_name_from_url
from auto_parser.sources.base import HttpSourceError, SourceError
from auto_parser.storage import ListingRepository


logger = logging.getLogger(__name__)
RATE_LIMIT_BASE_MINUTES = 15
RATE_LIMIT_MAX_MINUTES = 240
IMAGE_BATCH_SIZE = 40
DETAIL_BATCH_SIZE = 25
DETAIL_REFRESH_BATCH_SIZE = 10
DETAIL_REFRESH_INTERVAL_MINUTES = 15
DETAIL_INCOMPLETE_RETRY_MINUTES = 60
DETAIL_STALE_HOURS = 24
DETAIL_MAX_CONSECUTIVE_RATE_LIMITS = 3
DETAIL_RETRYABLE_HTTP_STATUSES = {403, 429, 439}
MAIL_IMPORT_INTERVAL_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _listing_is_incomplete(listing: object) -> bool:
    return (
        not getattr(listing, "description", None)
        or not getattr(listing, "attributes", None)
        or not getattr(listing, "image_urls", None)
    )


class BackgroundScheduler:
    def __init__(
        self,
        *,
        database: Path,
        cache_dir: Path,
        validation_interval_minutes: int = 60,
        poll_seconds: int = 5,
        source_gap_seconds: int = 10,
    ) -> None:
        self.database = database
        self.cache_dir = cache_dir
        self.validation_interval = timedelta(
            minutes=max(15, validation_interval_minutes)
        )
        self.poll_seconds = max(5, poll_seconds)
        self.source_gap_seconds = max(5, source_gap_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_validation: datetime | None = _now()
        self._last_detail_refresh: datetime | None = None
        self._source_cooldown_until: datetime | None = None
        self._reported_cooldown_until: datetime | None = None
        self._rate_limit_strikes = 0
        self.governor = RequestGovernor(database)
        self._source_governors = {"avito": self.governor}
        self._last_mail_import: datetime | None = None
        mail_config = MailImportConfig.from_env()
        self._mail_importer = (
            AutoRuMailImporter(database, mail_config) if mail_config else None
        )

    def _governor_for(self, source_name: str) -> RequestGovernor:
        if source_name not in self._source_governors:
            self._source_governors[source_name] = RequestGovernor(
                self.database,
                namespace=source_name,
            )
        return self._source_governors[source_name]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="auto-parser-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("Фоновый планировщик запущен")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("Ошибка цикла планировщика")
            self._stop_event.wait(self.poll_seconds)

    def _tick(self) -> None:
        now = _now()
        if (
            self._mail_importer is not None
            and (
                self._last_mail_import is None
                or (now - self._last_mail_import).total_seconds()
                >= MAIL_IMPORT_INTERVAL_SECONDS
            )
        ):
            self._last_mail_import = now
            try:
                self._mail_importer.poll()
            except Exception as error:
                logger.warning("Не удалось проверить письма Auto.ru: %s", error)
        persisted_cooldown = self.governor.cooldown_until_for("search")
        if (
            persisted_cooldown is not None
            and persisted_cooldown > now
        ):
            self._source_cooldown_until = persisted_cooldown
        search_blocked = (
            self._source_cooldown_until is not None
            and now < self._source_cooldown_until
        )
        if search_blocked:
            if self._reported_cooldown_until != self._source_cooldown_until:
                remaining = max(
                    1,
                    round(
                        (
                            self._source_cooldown_until - now
                        ).total_seconds()
                    ),
                )
                logger.info(
                    "Автопоиск ожидает снятия ограничения Avito до %s "
                    "(ещё около %d с)",
                    _iso(self._source_cooldown_until),
                    remaining,
                )
                self._reported_cooldown_until = self._source_cooldown_until
        else:
            self._reported_cooldown_until = None
        with ListingRepository(self.database) as repository:
            profiles = [dict(row) for row in repository.search_profiles(
                due_before=_iso(now)
            )]
        runnable_profiles = [
            profile
            for profile in profiles
            if not (
                search_blocked
                and str(profile.get("source") or "avito") == "avito"
            )
        ]
        for index, profile in enumerate(runnable_profiles):
            if self._stop_event.is_set():
                break
            if (
                str(profile.get("source") or "avito") == "avito"
                and self._source_cooldown_until is not None
                and _now() < self._source_cooldown_until
            ):
                continue
            self._run_profile(profile)
            if (
                index < len(runnable_profiles) - 1
                and self._stop_event.wait(self.source_gap_seconds)
            ):
                break

        if (
            self._last_detail_refresh is None
            or now - self._last_detail_refresh
            >= timedelta(minutes=DETAIL_REFRESH_INTERVAL_MINUTES)
        ):
            self._refresh_listing_details()
            self._last_detail_refresh = _now()

        if (
            self._last_validation is None
            or now - self._last_validation >= self.validation_interval
        ):
            self._validate_existing()
            self._last_validation = _now()

    def _register_rate_limit(
        self,
        error: HttpSourceError,
        *,
        already_recorded: bool = False,
        kind: str = "search",
        source_name: str = "avito",
    ) -> datetime:
        governor = self._governor_for(source_name)
        if kind == "search":
            self._rate_limit_strikes += 1
        cooldown = (
            governor.cooldown_until_for(kind)
            if already_recorded
            else None
        )
        if cooldown is None:
            cooldown = governor.record_rate_limit(
                retry_after_seconds=error.retry_after_seconds,
                kind=kind,
            )
        if cooldown is None:
            exponential_seconds = min(
                RATE_LIMIT_MAX_MINUTES * 60,
                RATE_LIMIT_BASE_MINUTES
                * 60
                * (2 ** (max(1, self._rate_limit_strikes) - 1)),
            )
            cooldown = _now() + timedelta(seconds=exponential_seconds)
        if kind == "search" and source_name == "avito":
            self._source_cooldown_until = cooldown
        if kind.startswith("refresh_"):
            logger.warning(
                "%s ограничил фоновое обновление (HTTP %d). "
                "Локальный cooldown зафиксирован до %s; "
                "принудительный проход продолжится до лимита ошибок",
                source_name,
                error.status_code,
                _iso(cooldown),
            )
        else:
            logger.warning(
                "%s ограничил контур %s (HTTP %d). "
                "Запросы этого контура приостановлены до %s",
                source_name,
                "автопоиска" if kind == "search" else "фоновых данных",
                error.status_code,
                _iso(cooldown),
            )
        return cooldown

    def _clear_rate_limit(self) -> None:
        self._rate_limit_strikes = 0
        persisted = self.governor.cooldown_until_for("search")
        self._source_cooldown_until = (
            persisted
            if persisted is not None and persisted > _now()
            else None
        )

    def _validate_existing(self) -> None:
        with ListingRepository(self.database) as repository:
            listings = repository.list_for_validation(
                due_before=_iso(_now() - self.validation_interval),
                limit=100,
            )
        if not listings:
            return
        logger.info("Валидация сохранённых объявлений: %d", len(listings))
        for index, listing in enumerate(listings, start=1):
            if self._stop_event.is_set():
                break
            if (
                listing.source == "auto_ru"
                and listing.attributes.get("Канал получения")
                == "Письмо сохранённого поиска Auto.ru"
            ):
                continue
            logger.info(
                "Валидация %d/%d: %s", index, len(listings), listing.external_id
            )
            try:
                service = SearchService(
                    source_from_name(listing.source),
                    governor=self._governor_for(listing.source),
                )
                status = service.validate_listing(listing)
            except RequestDeferredError as error:
                logger.info(
                    "Фоновая валидация отложена до %s; "
                    "автопоиск продолжит работу",
                    _iso(error.retry_at),
                )
                break
            except HttpSourceError as error:
                logger.warning("Валидация остановлена: %s", error)
                if error.status_code in {429, 439}:
                    self._register_rate_limit(
                        error,
                        already_recorded=True,
                        kind="validation",
                        source_name=listing.source,
                    )
                    break
                continue
            except SourceError as error:
                logger.warning("Объявление пропущено: %s", error)
                continue
            with ListingRepository(self.database) as repository:
                if status == "inactive":
                    repository.mark_inactive(
                        listing.source,
                        listing.external_id,
                        listing.last_validated_at or _iso(),
                    )
                else:
                    repository.upsert_many([listing])
                    repository.replace_listing_images(listing)

    def _refresh_listing_details(self) -> None:
        now = _now()
        listings = []
        with ListingRepository(self.database) as repository:
            for source_name in ("avito", "auto_ru"):
                listings.extend(repository.list_for_detail_refresh(
                    source_name,
                    incomplete_due_before=_iso(
                        now - timedelta(
                            minutes=DETAIL_INCOMPLETE_RETRY_MINUTES
                        )
                    ),
                    stale_due_before=_iso(
                        now - timedelta(hours=DETAIL_STALE_HOURS)
                    ),
                    limit=DETAIL_REFRESH_BATCH_SIZE,
                ))
        if not listings:
            return
        logger.info(
            "Автоматическое обновление неполных или устаревших карточек: %d",
            len(listings),
        )
        consecutive_rate_limits = 0
        last_rate_limit_status: int | None = None
        for index, listing in enumerate(listings, start=1):
            if self._stop_event.is_set():
                break
            logger.info(
                "Обновление карточки %d/%d: %s",
                index,
                len(listings),
                listing.external_id,
            )
            service = SearchService(
                source_from_name(listing.source),
                governor=self._governor_for(listing.source),
            )
            incomplete = _listing_is_incomplete(listing)
            request_kind = (
                "refresh_incomplete_detail"
                if incomplete
                else "refresh_stale_detail"
            )
            activity_priority = "incomplete" if incomplete else "stale"
            set_listing_activity(
                listing.source,
                listing.external_id,
                (
                    "В фоне обновляются недостающие данные…"
                    if incomplete
                    else "В фоне проверяется актуальность данных…"
                ),
                priority=activity_priority,
            )
            with ListingRepository(self.database) as repository:
                repository.mark_detail_attempted(
                    listing.source,
                    listing.external_id,
                    _iso(),
                )
            mail_imported = (
                listing.source == "auto_ru"
                and listing.attributes.get("Канал получения")
                == "Письмо сохранённого поиска Auto.ru"
            )
            if mail_imported:
                downloaded = 0
                if listing.image_urls and not listing.cached_images:
                    set_listing_activity(
                        listing.source,
                        listing.external_id,
                        "В фоне загружается обложка из письма Auto.ru…",
                        priority=activity_priority,
                    )
                    try:
                        downloaded = service.cache_images(
                            [listing],
                            cache_dir=self.cache_dir,
                            listings_limit=1,
                            images_per_listing=1,
                            request_kind="refresh_incomplete_image",
                        )
                    except (OSError, SourceError) as error:
                        logger.warning(
                            "Не удалось загрузить обложку из письма Auto.ru %s: %s",
                            listing.external_id,
                            error,
                        )
                with ListingRepository(self.database) as repository:
                    repository.upsert_many([listing])
                finish_listing_activity(
                    listing.source,
                    listing.external_id,
                    (
                        "Данные из письма Auto.ru сохранены"
                        if downloaded or listing.cached_images
                        else "Карточка импортирована из письма Auto.ru"
                    ),
                    priority=activity_priority,
                    state="success",
                )
                continue
            try:
                service.enrich_listing_details(
                    listing,
                    request_kind=request_kind,
                )
            except RequestDeferredError as error:
                finish_listing_activity(
                    listing.source,
                    listing.external_id,
                    f"Фоновое обновление отложено до {_iso(error.retry_at)}",
                    priority=activity_priority,
                    state="error",
                )
                logger.info(
                    "Обновление карточек отложено до %s",
                    _iso(error.retry_at),
                )
                break
            except HttpSourceError as error:
                if error.status_code in DETAIL_RETRYABLE_HTTP_STATUSES:
                    if error.status_code == last_rate_limit_status:
                        consecutive_rate_limits += 1
                    else:
                        consecutive_rate_limits = 1
                        last_rate_limit_status = error.status_code
                    self._register_rate_limit(
                        error,
                        already_recorded=True,
                        kind=request_kind,
                        source_name=listing.source,
                    )
                    finish_listing_activity(
                        listing.source,
                        listing.external_id,
                        (
                            f"{source_label(listing.source)} HTTP {error.status_code}: "
                            f"{consecutive_rate_limits}/"
                            f"{DETAIL_MAX_CONSECUTIVE_RATE_LIMITS}"
                        ),
                        priority=activity_priority,
                        state="error",
                    )
                    logger.warning(
                        "Фоновое обновление получило HTTP %d (%d/%d) "
                        "на карточке %s",
                        error.status_code,
                        consecutive_rate_limits,
                        DETAIL_MAX_CONSECUTIVE_RATE_LIMITS,
                        listing.external_id,
                    )
                    if (
                        consecutive_rate_limits
                        >= DETAIL_MAX_CONSECUTIVE_RATE_LIMITS
                    ):
                        logger.warning(
                            "Фоновый проход остановлен после %d "
                            "последовательных ограничений источника %s",
                            consecutive_rate_limits,
                            listing.source,
                        )
                        break
                    continue
                logger.warning(
                    "Фоновое обновление остановлено на карточке %s: %s",
                    listing.external_id,
                    error,
                )
                finish_listing_activity(
                    listing.source,
                    listing.external_id,
                    f"Фоновое обновление остановлено: {error}",
                    priority=activity_priority,
                    state="error",
                )
                break
            except SourceError as error:
                logger.warning(
                    "Фоновое обновление остановлено на карточке %s: %s",
                    listing.external_id,
                    error,
                )
                finish_listing_activity(
                    listing.source,
                    listing.external_id,
                    f"Фоновое обновление остановлено: {error}",
                    priority=activity_priority,
                    state="error",
                )
                break
            consecutive_rate_limits = 0
            last_rate_limit_status = None
            with ListingRepository(self.database) as repository:
                repository.upsert_many([listing])
                repository.replace_listing_images(listing)
            if listing.image_urls and not listing.cached_images:
                set_listing_activity(
                    listing.source,
                    listing.external_id,
                    "В фоне загружается обложка…",
                    priority=activity_priority,
                )
                try:
                    service.cache_images(
                        [listing],
                        cache_dir=self.cache_dir,
                        listings_limit=1,
                        images_per_listing=1,
                        request_kind=(
                            "refresh_incomplete_image"
                            if incomplete
                            else "refresh_stale_image"
                        ),
                    )
                except HttpSourceError as error:
                    if error.status_code in DETAIL_RETRYABLE_HTTP_STATUSES:
                        if error.status_code == last_rate_limit_status:
                            consecutive_rate_limits += 1
                        else:
                            consecutive_rate_limits = 1
                            last_rate_limit_status = error.status_code
                        self._register_rate_limit(
                            error,
                            already_recorded=True,
                            kind=(
                                "refresh_incomplete_image"
                                if incomplete
                                else "refresh_stale_image"
                            ),
                            source_name=listing.source,
                        )
                        finish_listing_activity(
                            listing.source,
                            listing.external_id,
                            (
                                f"Фото: {source_label(listing.source)} HTTP {error.status_code}: "
                                f"{consecutive_rate_limits}/"
                                f"{DETAIL_MAX_CONSECUTIVE_RATE_LIMITS}"
                            ),
                            priority=activity_priority,
                            state="error",
                        )
                        if (
                            consecutive_rate_limits
                            >= DETAIL_MAX_CONSECUTIVE_RATE_LIMITS
                        ):
                            logger.warning(
                                "Фоновый проход остановлен после %d "
                                "последовательных ограничений источника %s",
                                consecutive_rate_limits,
                                listing.source,
                            )
                            break
                        continue
                    logger.warning(
                        "Фоновое обновление остановлено при загрузке "
                        "обложки %s: %s",
                        listing.external_id,
                        error,
                    )
                    finish_listing_activity(
                        listing.source,
                        listing.external_id,
                        f"Фоновое обновление остановлено: {error}",
                        priority=activity_priority,
                        state="error",
                    )
                    break
                except (RequestDeferredError, SourceError) as error:
                    logger.warning(
                        "Фоновое обновление остановлено при загрузке "
                        "обложки %s: %s",
                        listing.external_id,
                        error,
                    )
                    finish_listing_activity(
                        listing.source,
                        listing.external_id,
                        f"Фоновое обновление остановлено: {error}",
                        priority=activity_priority,
                        state="error",
                    )
                    break
                else:
                    consecutive_rate_limits = 0
                    last_rate_limit_status = None
                    with ListingRepository(self.database) as repository:
                        repository.upsert_many([listing])
                        repository.replace_listing_images(listing)
            finish_listing_activity(
                listing.source,
                listing.external_id,
                "Карточка обновлена в фоне",
                priority=activity_priority,
                state="success",
            )
            logger.info(
                "Карточка %s успешно обновлена в фоне",
                listing.external_id,
            )

    def _run_profile(self, profile: dict[str, object]) -> None:
        started = _now()
        interval = max(15, int(profile["interval_minutes"]))
        count = 0
        status = "ok"
        retry_not_before: datetime | None = None
        with ListingRepository(self.database) as repository:
            repository.begin_search_profile(
                int(profile["id"]),
                started_at=_iso(started),
            )
        profile_query_hint = str(profile["query"]).strip()
        profile_source_hint = str(profile.get("source") or "avito")
        if profile_query_hint.startswith(("https://", "http://")):
            profile_source_hint = source_name_from_url(profile_query_hint)
        if profile_source_hint == "auto_ru":
            configured = MailImportConfig.from_env() is not None
            status = (
                "Почтовый импорт Auto.ru активен; ожидаются письма сохранённого поиска"
                if configured
                else "Настройте AUTO_RU_IMAP_* для автоматического импорта Auto.ru"
            )
            next_run = started + timedelta(minutes=interval)
            with ListingRepository(self.database) as repository:
                count = int(
                    repository.connection.execute(
                        "SELECT COUNT(*) FROM listings WHERE source = 'auto_ru'"
                    ).fetchone()[0]
                )
                repository.finish_search_profile(
                    int(profile["id"]),
                    last_run_at=_iso(started),
                    next_run_at=_iso(next_run),
                    status=status,
                    result_count=count,
                )
            logger.info("Профиль Auto.ru переведён на почтовый импорт")
            return
        try:
            profile_query = str(profile["query"]).strip()
            profile_source = str(profile.get("source") or "avito")
            search_url = (
                profile_query
                if profile_query.startswith(("https://", "http://"))
                else None
            )
            if search_url:
                profile_source = source_name_from_url(search_url)
            source = source_from_name(
                profile_source,
                region=str(profile["region"]),
                radius=(
                    int(profile["radius"])
                    if profile["radius"] is not None
                    else None
                ),
                search_url=search_url,
            )
            service = SearchService(
                source,
                governor=self._governor_for(source.name),
            )
            listings = service.search("" if search_url else profile_query)
            if service.rate_limit_error is None and source.name == "avito":
                self._clear_rate_limit()
            with ListingRepository(self.database) as repository:
                count = repository.upsert_many(listings)
                persisted = [
                    repository.get_listing(item.source, item.external_id)
                    for item in listings
                ]
            if service.rate_limit_error is not None:
                raise service.rate_limit_error

            cover_batch = [
                listing
                for listing in persisted
                if listing is not None
                and listing.image_urls
                and not listing.cached_images
            ][:IMAGE_BATCH_SIZE]
            for listing in cover_batch:
                if self._stop_event.is_set():
                    break
                try:
                    service.cache_images(
                        [listing],
                        cache_dir=self.cache_dir,
                        listings_limit=1,
                        images_per_listing=1,
                    )
                except RequestDeferredError as error:
                    logger.info(
                        "Загрузка обложек отложена до %s; "
                        "результаты поиска уже сохранены",
                        _iso(error.retry_at),
                    )
                    break
                except HttpSourceError as error:
                    if error.status_code in {429, 439}:
                        self._register_rate_limit(
                            error,
                            already_recorded=True,
                            kind="image",
                            source_name=source.name,
                        )
                        break
                    raise
                with ListingRepository(self.database) as repository:
                    repository.upsert_many([listing])
                    repository.replace_listing_images(listing)

            with ListingRepository(self.database) as repository:
                pending = repository.list_for_enrichment(
                    source.name,
                    [listing.external_id for listing in listings],
                )[:DETAIL_BATCH_SIZE]
            for listing in pending:
                if self._stop_event.is_set():
                    break
                with ListingRepository(self.database) as repository:
                    repository.mark_detail_attempted(
                        listing.source,
                        listing.external_id,
                        _iso(),
                    )
                try:
                    service.enrich_listing_details(
                        listing,
                        request_kind="incomplete_detail",
                    )
                except RequestDeferredError as error:
                    status = (
                        "Поиск завершён; догрузка данных отложена до "
                        f"{_iso(error.retry_at)}"
                    )
                    logger.info(status)
                    break
                except HttpSourceError as error:
                    logger.warning(
                        "Не удалось дополнить объявление %s: %s",
                        listing.external_id,
                        error,
                    )
                    if error.status_code in {429, 439}:
                        detail_retry = self._register_rate_limit(
                            error,
                            already_recorded=True,
                            kind="detail",
                            source_name=source.name,
                        )
                        status = (
                            "Поиск завершён; догрузка данных ограничена "
                            f"{source_label(source.name)} HTTP {error.status_code} до "
                            f"{_iso(detail_retry)}"
                        )
                        break
                    continue
                except SourceError as error:
                    logger.warning(
                        "Не удалось дополнить объявление %s: %s",
                        listing.external_id,
                        error,
                    )
                    continue
                with ListingRepository(self.database) as repository:
                    repository.upsert_many([listing])
                    repository.replace_listing_images(listing)
                try:
                    service.cache_images(
                        [listing],
                        cache_dir=self.cache_dir,
                        listings_limit=1,
                        images_per_listing=1,
                    )
                except RequestDeferredError as error:
                    status = (
                        "Поиск завершён; изображения отложены до "
                        f"{_iso(error.retry_at)}"
                    )
                    logger.info(status)
                    break
                except HttpSourceError as error:
                    if error.status_code in {429, 439}:
                        image_retry = self._register_rate_limit(
                            error,
                            already_recorded=True,
                            kind="image",
                            source_name=source.name,
                        )
                        status = (
                            "Поиск завершён; изображения ограничены "
                            f"{source_label(source.name)} HTTP {error.status_code} до "
                            f"{_iso(image_retry)}"
                        )
                        break
                    raise
                with ListingRepository(self.database) as repository:
                    repository.upsert_many([listing])
                    repository.replace_listing_images(listing)
        except RequestDeferredError as error:
            if error.lane == "search":
                retry_not_before = error.retry_at
                self._source_cooldown_until = error.retry_at
                status = (
                    "Автопоиск отложен своим лимитером; "
                    f"повтор после {_iso(error.retry_at)}"
                )
            else:
                status = (
                    "Поиск завершён; фоновые данные отложены до "
                    f"{_iso(error.retry_at)}"
                )
            logger.info("Профиль %s отложен", profile["query"])
        except HttpSourceError as error:
            if error.status_code in {429, 439}:
                retry_not_before = self._register_rate_limit(
                    error,
                    already_recorded=True,
                    source_name=source.name,
                )
                status = (
                    (
                        f"Частично сохранено: {count}; ограничение"
                        if count
                        else "Ограничение"
                    )
                    + f" {source_label(source.name)} HTTP {error.status_code}; "
                    f"повтор после {_iso(retry_not_before)}"
                )
                logger.warning(
                    "Профиль %s отложен: %s",
                    profile["query"],
                    status,
                )
            else:
                status = f"error: {error}"
                logger.exception("Ошибка профиля %s", profile["query"])
        except Exception as error:
            status = f"error: {error}"
            logger.exception("Ошибка профиля %s", profile["query"])
        next_run = started + timedelta(minutes=interval)
        if retry_not_before is not None:
            next_run = max(next_run, retry_not_before)
        with ListingRepository(self.database) as repository:
            repository.finish_search_profile(
                int(profile["id"]),
                last_run_at=_iso(started),
                next_run_at=_iso(next_run),
                status=status[:500],
                result_count=count,
            )
        logger.info(
            "Профиль %s завершён: %s, объявлений=%d",
            profile["query"],
            status,
            count,
        )

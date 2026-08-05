from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from auto_parser.formatting import format_listing
from auto_parser.request_governor import RequestGovernor
from auto_parser.service import SearchService
from auto_parser.sources import source_from_name, source_name_from_url
from auto_parser.sources.base import SourceError
from auto_parser.storage import ListingRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="auto-parser",
        description="Поиск автомобильных объявлений по марке и модели",
    )
    parser.add_argument(
        "query",
        nargs="?",
        help='Например: "Toyota Camry"; не требуется вместе с --url',
    )
    parser.add_argument(
        "--region",
        default="all",
        help="Регион из URL источника: tver, moskva и т. п. (по умолчанию all)",
    )
    parser.add_argument(
        "--source",
        choices=("avito", "auto_ru", "drom"),
        default="avito",
        help="Источник объявлений: avito, auto_ru или drom",
    )
    parser.add_argument(
        "--radius",
        type=_non_negative_int,
        help="Радиус поиска в километрах",
    )
    parser.add_argument(
        "--url",
        help="Готовая HTTPS-ссылка поиска Avito, Auto.ru или Drom со всеми фильтрами",
    )
    parser.add_argument(
        "--database",
        default="listings.db",
        type=Path,
        help="Путь к базе SQLite (по умолчанию listings.db)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Напечатать найденные объявления в JSON",
    )
    parser.add_argument(
        "--limit",
        default=20,
        type=_positive_int,
        help="Максимум карточек в консоли (по умолчанию 20)",
    )
    parser.add_argument(
        "--search-limit",
        default=200,
        type=_positive_int,
        help="Максимум объявлений собрать из выдачи (по умолчанию 200)",
    )
    parser.add_argument(
        "--search-pages",
        default=0,
        type=_non_negative_int,
        help="Максимум страниц выдачи проверить (по умолчанию 0 — до конца выдачи)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Показать расширенные диагностические логи",
    )
    parser.add_argument(
        "--details-limit",
        default=5,
        type=_non_negative_int,
        help=(
            "Сколько первых карточек открыть для полного описания "
            "и пробега (по умолчанию 5, 0 — отключить)"
        ),
    )
    parser.add_argument(
        "--images-limit",
        default=5,
        type=_non_negative_int,
        help=(
            "Для скольких первых объявлений кэшировать изображения "
            "(по умолчанию 5, 0 — отключить)"
        ),
    )
    parser.add_argument(
        "--images-per-listing",
        default=1,
        type=_non_negative_int,
        help=(
            "Максимум изображений на объявление "
            "(по умолчанию 1, 0 — все найденные)"
        ),
    )
    parser.add_argument(
        "--cache-dir",
        default=Path(".cache/auto_parser/images"),
        type=Path,
        help="Каталог кэша изображений",
    )
    parser.add_argument(
        "--viewer",
        action="store_true",
        help="Открыть локальную веб-панель сохранённых объявлений",
    )
    parser.add_argument(
        "--viewer-host",
        default="127.0.0.1",
        help="Адрес веб-панели (по умолчанию 127.0.0.1)",
    )
    parser.add_argument(
        "--viewer-port",
        default=8080,
        type=_positive_int,
        help="Порт веб-панели (по умолчанию 8080)",
    )
    parser.add_argument(
        "--allow-remote-viewer",
        action="store_true",
        help="Явно разрешить веб-панели слушать нелокальный адрес",
    )
    parser.add_argument(
        "--viewer-password",
        help=(
            "Пароль веб-панели; для удалённого доступа также можно задать "
            "AUTOSCOPE_VIEWER_PASSWORD"
        ),
    )
    parser.add_argument(
        "--scheduler",
        dest="scheduler",
        action="store_true",
        default=True,
        help="Запустить фоновый поиск и валидацию (включено по умолчанию с --viewer)",
    )
    parser.add_argument(
        "--no-scheduler",
        dest="scheduler",
        action="store_false",
        help="Отключить фоновый поиск при запуске веб-панели",
    )
    parser.add_argument(
        "--validation-interval",
        default=60,
        type=_positive_int,
        help="Интервал валидации сохранённых объявлений в минутах",
    )
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return number


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("значение не может быть отрицательным")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.viewer:
        from auto_parser.scheduler import BackgroundScheduler
        from auto_parser.viewer import serve

        scheduler = None
        try:
            if args.scheduler:
                # Complete schema migrations before concurrent workers open
                # their own SQLite connections.
                with ListingRepository(args.database):
                    pass
                scheduler = BackgroundScheduler(
                    database=args.database,
                    cache_dir=args.cache_dir,
                    validation_interval_minutes=args.validation_interval,
                )
                scheduler.start()
            serve(
                args.database,
                cache_dir=args.cache_dir,
                host=args.viewer_host,
                port=args.viewer_port,
                allow_remote=args.allow_remote_viewer,
                password=args.viewer_password,
            )
        except (OSError, ValueError) as error:
            print(f"Ошибка панели: {error}", file=sys.stderr)
            return 2
        finally:
            if scheduler:
                scheduler.stop()
        return 0
    if not args.query and not args.url:
        parser.error("укажите поисковый запрос или --url")

    try:
        source_name = source_name_from_url(args.url) if args.url else args.source
        source = source_from_name(
            source_name,
            region=args.region,
            radius=args.radius,
            search_url=args.url,
        )
        service = SearchService(
            source,
            governor=RequestGovernor(
                args.database,
                namespace=source.name,
            ),
        )
        listings = service.search(
            args.query or "",
            max_results=args.search_limit,
            max_pages=args.search_pages,
        )
        if args.details_limit and service.rate_limit_error is None:
            service.enrich_details(listings, limit=args.details_limit)
        if args.images_limit and service.rate_limit_error is None:
            service.cache_images(
                listings,
                cache_dir=args.cache_dir,
                listings_limit=args.images_limit,
                images_per_listing=args.images_per_listing,
            )
    except (SourceError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2

    with ListingRepository(args.database) as repository:
        saved = repository.upsert_many(listings)

    if args.json:
        print(
            json.dumps(
                [listing.as_dict() for listing in listings],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"Найдено и сохранено объявлений: {saved}")
        for index, listing in enumerate(listings[: args.limit], start=1):
            print(f"\n[{index}] {format_listing(listing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

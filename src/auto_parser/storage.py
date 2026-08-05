from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from auto_parser.images import (
    avito_image_identity,
    deduplicate_avito_image_urls,
    deduplicate_image_urls,
    image_identity,
    remap_cached_avito_images,
    remap_cached_images,
)
from auto_parser.models import Listing


def _vehicle_key(value: str | None) -> str:
    normalized = str(value or "").casefold().replace("ё", "е")
    normalized = re.sub(r"\bсер(?:ия|ии)\b", "series", normalized)
    return re.sub(r"[^a-zа-я0-9]+", "", normalized)


def _listing_year(listing: Listing) -> int | None:
    raw = listing.attributes.get("Год выпуска", "")
    match = re.search(r"\b((?:19|20)\d{2})\b", raw or listing.title)
    return int(match.group(1)) if match else None


class ListingRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS listings (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                price INTEGER,
                currency TEXT,
                description TEXT,
                mileage_km INTEGER,
                brand TEXT,
                model TEXT,
                views_count INTEGER,
                attributes_json TEXT NOT NULL DEFAULT '{}',
                location TEXT,
                published_at TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                sold_price INTEGER,
                sold_at TEXT,
                hidden INTEGER NOT NULL DEFAULT 0,
                last_validated_at TEXT,
                last_detail_attempt_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (source, external_id)
            );
            CREATE TABLE IF NOT EXISTS price_history (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                price INTEGER,
                currency TEXT,
                observed_at TEXT NOT NULL,
                UNIQUE (source, external_id, observed_at)
            );
            CREATE TABLE IF NOT EXISTS listing_images (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                image_url TEXT NOT NULL,
                local_path TEXT,
                cached_at TEXT NOT NULL,
                PRIMARY KEY (source, external_id, image_url)
            );
            CREATE TABLE IF NOT EXISTS displayed_listings (
                client_id TEXT NOT NULL,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                seen_at TEXT NOT NULL,
                PRIMARY KEY (client_id, source, external_id)
            );
            CREATE INDEX IF NOT EXISTS displayed_listings_seen_idx
                ON displayed_listings(seen_at);
            CREATE INDEX IF NOT EXISTS displayed_listings_listing_idx
                ON displayed_listings(source, external_id, seen_at);
            CREATE TABLE IF NOT EXISTS vehicle_trims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_key TEXT NOT NULL,
                model_key TEXT NOT NULL,
                year INTEGER NOT NULL,
                name TEXT NOT NULL,
                attributes_json TEXT NOT NULL DEFAULT '{}',
                source_url TEXT,
                observed_at TEXT NOT NULL,
                UNIQUE(brand_key, model_key, year, name)
            );
            CREATE INDEX IF NOT EXISTS vehicle_trims_match_idx
                ON vehicle_trims(brand_key, model_key, year);
            CREATE TABLE IF NOT EXISTS vehicle_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_key TEXT NOT NULL,
                model_key TEXT NOT NULL,
                trim_key TEXT NOT NULL,
                trim_name TEXT NOT NULL,
                brand TEXT,
                model TEXT,
                analysis_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(brand_key, model_key, trim_key)
            );
            CREATE TABLE IF NOT EXISTS listing_trim_assignments (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                trim_name TEXT NOT NULL,
                assigned_at TEXT NOT NULL,
                PRIMARY KEY (source, external_id)
            );
            CREATE TABLE IF NOT EXISTS listing_vehicle_assessments (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                assessment_json TEXT NOT NULL,
                description_snapshot TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, external_id)
            );
            CREATE TABLE IF NOT EXISTS search_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL DEFAULT 'avito',
                query TEXT NOT NULL,
                region TEXT NOT NULL DEFAULT 'all',
                radius INTEGER,
                min_price INTEGER,
                max_price INTEGER,
                interval_minutes INTEGER NOT NULL DEFAULT 60,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                next_run_at TEXT,
                last_status TEXT,
                last_result_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(source, query, region, radius, min_price, max_price)
            );
            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS car_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                car_source TEXT NOT NULL,
                car_external_id TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Прочее',
                part_number TEXT,
                price INTEGER,
                quantity INTEGER NOT NULL DEFAULT 1,
                labor_cost INTEGER NOT NULL DEFAULT 0,
                seller TEXT,
                purchase_url TEXT,
                description TEXT,
                replacement_term TEXT NOT NULL DEFAULT 'Позже',
                selected_for_replacement INTEGER NOT NULL DEFAULT 0,
                estimated INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(car_source, car_external_id, name, seller)
            );
            CREATE TABLE IF NOT EXISTS spare_part_offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_list_url TEXT,
                brand TEXT NOT NULL,
                model TEXT NOT NULL,
                brand_key TEXT NOT NULL,
                model_key TEXT NOT NULL,
                generation INTEGER,
                year_from INTEGER,
                year_to INTEGER,
                fuel TEXT,
                engine_volume_cc INTEGER,
                category TEXT,
                subcategory TEXT,
                name TEXT NOT NULL,
                description TEXT,
                price INTEGER,
                image_url TEXT,
                seller TEXT,
                location TEXT,
                observed_at TEXT NOT NULL,
                UNIQUE(source, external_id)
            );
            CREATE TABLE IF NOT EXISTS garage_cars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_source TEXT,
                listing_external_id TEXT,
                name TEXT NOT NULL,
                brand TEXT,
                model TEXT,
                year INTEGER,
                mileage_km INTEGER,
                vin TEXT,
                plate_number TEXT,
                purchase_date TEXT,
                purchase_price INTEGER,
                attributes_json TEXT NOT NULL DEFAULT '{}',
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(listing_source, listing_external_id)
            );
            CREATE TABLE IF NOT EXISTS garage_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                garage_id INTEGER NOT NULL,
                entry_type TEXT NOT NULL DEFAULT 'journal',
                title TEXT NOT NULL,
                description TEXT,
                category TEXT,
                occurred_at TEXT NOT NULL,
                mileage_km INTEGER,
                cost INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(garage_id) REFERENCES garage_cars(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS mail_imports (
                message_id TEXT PRIMARY KEY,
                sender TEXT,
                subject TEXT,
                received_at TEXT,
                processed_at TEXT NOT NULL,
                listing_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS notification_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                query TEXT,
                brands_json TEXT NOT NULL DEFAULT '[]',
                models_json TEXT NOT NULL DEFAULT '[]',
                colors_json TEXT NOT NULL DEFAULT '[]',
                engines_json TEXT NOT NULL DEFAULT '[]',
                min_price INTEGER,
                max_price INTEGER,
                max_mileage INTEGER,
                min_year INTEGER,
                max_year INTEGER,
                min_power INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT,
                UNIQUE(rule_id, source, external_id),
                FOREIGN KEY(rule_id) REFERENCES notification_rules(id)
                    ON DELETE CASCADE
            );
            """
        )
        part_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(car_parts)")
        }
        if "garage_id" not in part_columns:
            self.connection.execute(
                "ALTER TABLE car_parts ADD COLUMN garage_id INTEGER"
            )
        analysis_schema_row = self.connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'vehicle_analyses'"
        ).fetchone()
        analysis_schema = "".join(
            str(
                analysis_schema_row["sql"]
                if analysis_schema_row
                else ""
            ).split()
        ).lower()
        if "unique(brand_key,model_key,trim_key)" not in analysis_schema:
            self.connection.executescript(
                """
                CREATE TABLE vehicle_analyses_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_key TEXT NOT NULL,
                    model_key TEXT NOT NULL,
                    trim_key TEXT NOT NULL,
                    trim_name TEXT NOT NULL,
                    brand TEXT,
                    model TEXT,
                    analysis_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(brand_key, model_key, trim_key)
                );
                INSERT OR IGNORE INTO vehicle_analyses_v2 (
                    id, brand_key, model_key, trim_key, trim_name,
                    brand, model, analysis_json, updated_at
                )
                SELECT id, brand_key, model_key, '', '',
                       brand, model, analysis_json, updated_at
                FROM vehicle_analyses;
                DROP TABLE vehicle_analyses;
                ALTER TABLE vehicle_analyses_v2 RENAME TO vehicle_analyses;
                """
            )
        profile_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(search_profiles)"
            )
        }
        if "source" not in profile_columns:
            self.connection.execute(
                "ALTER TABLE search_profiles "
                "ADD COLUMN source TEXT NOT NULL DEFAULT 'avito'"
            )
        profile_schema_row = self.connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'search_profiles'"
        ).fetchone()
        profile_schema = "".join(
            str(profile_schema_row["sql"] if profile_schema_row else "").split()
        ).lower()
        if (
            "unique(query,region,radius)" in profile_schema
            or "unique(source,query,region,radius)" in profile_schema
            or "min_price" not in profile_columns
            or "max_price" not in profile_columns
        ):
            self.connection.executescript(
                """
                CREATE TABLE search_profiles_v2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL DEFAULT 'avito',
                    query TEXT NOT NULL,
                    region TEXT NOT NULL DEFAULT 'all',
                    radius INTEGER,
                    min_price INTEGER,
                    max_price INTEGER,
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at TEXT,
                    next_run_at TEXT,
                    last_status TEXT,
                    last_result_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(
                        source, query, region, radius,
                        min_price, max_price
                    )
                );
                INSERT INTO search_profiles_v2 (
                    id, source, query, region, radius,
                    min_price, max_price, interval_minutes,
                    enabled, last_run_at, next_run_at, last_status,
                    last_result_count, created_at
                )
                SELECT
                    id, source, query, region, radius,
                    NULL, NULL, interval_minutes,
                    enabled, last_run_at, next_run_at, last_status,
                    last_result_count, created_at
                FROM search_profiles;
                DROP TABLE search_profiles;
                ALTER TABLE search_profiles_v2 RENAME TO search_profiles;
                """
            )
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS search_profile_listings (
                profile_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (profile_id, source, external_id)
            );
            CREATE INDEX IF NOT EXISTS search_profile_listings_listing_idx
                ON search_profile_listings(source, external_id, profile_id);
            """
        )
        price_schema_row = self.connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'price_history'"
        ).fetchone()
        price_schema = "".join(
            str(price_schema_row["sql"] if price_schema_row else "").split()
        ).lower()
        if "unique(source,external_id,price,currency)" in price_schema:
            self.connection.executescript(
                """
                CREATE TABLE price_history_v2 (
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    price INTEGER,
                    currency TEXT,
                    observed_at TEXT NOT NULL,
                    UNIQUE(source, external_id, observed_at)
                );
                INSERT OR IGNORE INTO price_history_v2 (
                    source, external_id, price, currency, observed_at
                )
                SELECT source, external_id, price, currency, observed_at
                FROM price_history;
                DROP TABLE price_history;
                ALTER TABLE price_history_v2 RENAME TO price_history;
                """
            )
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(listings)")
        }
        if "description" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN description TEXT"
            )
        if "mileage_km" not in columns:
            self.connection.execute(
                "ALTER TABLE listings ADD COLUMN mileage_km INTEGER"
            )
        migrations = {
            "brand": "ALTER TABLE listings ADD COLUMN brand TEXT",
            "model": "ALTER TABLE listings ADD COLUMN model TEXT",
            "views_count": "ALTER TABLE listings ADD COLUMN views_count INTEGER",
            "attributes_json": (
                "ALTER TABLE listings ADD COLUMN "
                "attributes_json TEXT NOT NULL DEFAULT '{}'"
            ),
            "status": (
                "ALTER TABLE listings ADD COLUMN "
                "status TEXT NOT NULL DEFAULT 'active'"
            ),
            "sold_price": (
                "ALTER TABLE listings ADD COLUMN sold_price INTEGER"
            ),
            "sold_at": "ALTER TABLE listings ADD COLUMN sold_at TEXT",
            "hidden": (
                "ALTER TABLE listings ADD COLUMN "
                "hidden INTEGER NOT NULL DEFAULT 0"
            ),
            "last_validated_at": (
                "ALTER TABLE listings ADD COLUMN last_validated_at TEXT"
            ),
            "last_detail_attempt_at": (
                "ALTER TABLE listings ADD COLUMN last_detail_attempt_at TEXT"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                self.connection.execute(statement)
        self.connection.execute(
            """
            UPDATE listings
            SET status = 'sold',
                sold_price = COALESCE(sold_price, price),
                sold_at = COALESCE(sold_at, last_validated_at, last_seen_at)
            WHERE status = 'inactive'
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS listings_brand_idx ON listings(brand)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS garage_cars_updated_idx "
            "ON garage_cars(updated_at DESC)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS garage_entries_car_idx "
            "ON garage_entries(garage_id, occurred_at DESC)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS car_parts_garage_idx "
            "ON car_parts(garage_id, selected_for_replacement)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS listings_status_idx ON listings(status)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS listings_hidden_idx ON listings(hidden)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS notifications_unread_idx "
            "ON notifications(read_at, created_at DESC)"
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS listings_recent_idx
            ON listings(hidden, last_seen_at DESC)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS listings_added_idx
            ON listings(hidden, first_seen_at DESC)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS listings_detail_refresh_idx
            ON listings(source, status, last_detail_attempt_at)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS car_parts_car_idx
            ON car_parts(car_source, car_external_id)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS car_parts_plan_idx
            ON car_parts(
                car_source, car_external_id,
                selected_for_replacement, replacement_term
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS spare_part_offers_vehicle_idx "
            "ON spare_part_offers(brand_key, model_key, year_from, year_to)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS spare_part_offers_price_idx "
            "ON spare_part_offers(brand_key, model_key, price)"
        )
        spare_offer_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(spare_part_offers)")
        }
        if "category" not in spare_offer_columns:
            self.connection.execute(
                "ALTER TABLE spare_part_offers ADD COLUMN category TEXT"
            )
        if "subcategory" not in spare_offer_columns:
            self.connection.execute(
                "ALTER TABLE spare_part_offers ADD COLUMN subcategory TEXT"
            )
        self.connection.execute(
            """
            UPDATE listings
            SET brand = CASE
                WHEN INSTR(title, ' ') > 0
                THEN SUBSTR(title, 1, INSTR(title, ' ') - 1)
                ELSE title
            END
            WHERE brand IS NULL OR brand = ''
            """
        )
        self._migrate_duplicate_image_variants()
        self._migrate_vehicle_analysis_repair_links()
        self.connection.commit()

    def _migrate_vehicle_analysis_repair_links(self) -> None:
        from auto_parser.spare_parts import normalize_vehicle_analysis_parts

        rows = self.connection.execute(
            "SELECT id, analysis_json FROM vehicle_analyses"
        ).fetchall()
        for row in rows:
            try:
                analysis = json.loads(row["analysis_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(analysis, dict):
                continue
            normalized = normalize_vehicle_analysis_parts(analysis)
            encoded = json.dumps(normalized, ensure_ascii=False)
            if encoded != row["analysis_json"]:
                self.connection.execute(
                    "UPDATE vehicle_analyses SET analysis_json = ? WHERE id = ?",
                    (encoded, row["id"]),
                )

    def _migrate_duplicate_image_variants(self) -> None:
        migration_key = "deduplicate_avito_image_variants_v2"
        completed = self.connection.execute(
            "SELECT 1 FROM app_metadata WHERE key = ?",
            (migration_key,),
        ).fetchone()
        if completed:
            return

        rows = self.connection.execute(
            """
            SELECT source, external_id, image_url, local_path, cached_at
            FROM listing_images
            WHERE source = 'avito'
            ORDER BY source, external_id, rowid
            """
        ).fetchall()
        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            key = (row["source"], row["external_id"])
            grouped.setdefault(key, []).append(row)

        for (source, external_id), image_rows in grouped.items():
            urls = [row["image_url"] for row in image_rows]
            selected = deduplicate_avito_image_urls(urls)
            cached = remap_cached_avito_images(
                selected,
                {
                    row["image_url"]: row["local_path"]
                    for row in image_rows
                    if row["local_path"]
                },
            )
            cached_at = max(row["cached_at"] for row in image_rows)
            self.connection.execute(
                """
                DELETE FROM listing_images
                WHERE source = ? AND external_id = ?
                """,
                (source, external_id),
            )
            for image_url in selected:
                self.connection.execute(
                    """
                    INSERT INTO listing_images (
                        source, external_id, image_url, local_path, cached_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source,
                        external_id,
                        image_url,
                        cached.get(image_url),
                        cached_at,
                    ),
                )

        self.connection.execute(
            """
            INSERT OR REPLACE INTO app_metadata (key, value)
            VALUES (?, 'complete')
            """,
            (migration_key,),
        )

    def upsert_many(self, listings: Iterable[Listing]) -> int:
        count = 0
        for listing in listings:
            listing.image_urls = deduplicate_image_urls(listing.image_urls)
            self.connection.execute(
                """
                INSERT INTO listings (
                    source, external_id, url, title, price, currency, description,
                    mileage_km, brand, model, views_count, attributes_json,
                    location, published_at, status, last_validated_at,
                    sold_price, sold_at, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, external_id) DO UPDATE SET
                    url = excluded.url,
                    title = excluded.title,
                    price = COALESCE(excluded.price, listings.price),
                    currency = COALESCE(excluded.currency, listings.currency),
                    description = COALESCE(
                        excluded.description, listings.description
                    ),
                    mileage_km = COALESCE(
                        excluded.mileage_km, listings.mileage_km
                    ),
                    brand = COALESCE(excluded.brand, listings.brand),
                    model = COALESCE(excluded.model, listings.model),
                    views_count = COALESCE(
                        excluded.views_count, listings.views_count
                    ),
                    attributes_json = CASE
                        WHEN excluded.attributes_json = '{}'
                        THEN listings.attributes_json
                        ELSE excluded.attributes_json
                    END,
                    location = COALESCE(excluded.location, listings.location),
                    published_at = COALESCE(
                        excluded.published_at, listings.published_at
                    ),
                    status = excluded.status,
                    sold_price = CASE
                        WHEN excluded.status = 'sold'
                        THEN COALESCE(excluded.sold_price, listings.sold_price,
                                      excluded.price, listings.price)
                        ELSE NULL
                    END,
                    sold_at = CASE
                        WHEN excluded.status = 'sold'
                        THEN COALESCE(excluded.sold_at, listings.sold_at,
                                      excluded.last_validated_at,
                                      excluded.last_seen_at)
                        ELSE NULL
                    END,
                    last_validated_at = COALESCE(
                        excluded.last_validated_at, listings.last_validated_at
                    ),
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    listing.source,
                    listing.external_id,
                    listing.url,
                    listing.title,
                    listing.price,
                    listing.currency,
                    listing.description,
                    listing.mileage_km,
                    listing.brand,
                    listing.model,
                    listing.views_count,
                    json.dumps(listing.attributes, ensure_ascii=False),
                    listing.location,
                    listing.published_at,
                    listing.status,
                    listing.last_validated_at,
                    listing.sold_price,
                    listing.sold_at,
                    listing.collected_at,
                    listing.collected_at,
                ),
            )
            self._remember_drom_trim(listing)
            latest_price = self.connection.execute(
                """
                SELECT price, currency FROM price_history
                WHERE source = ? AND external_id = ?
                ORDER BY observed_at DESC LIMIT 1
                """,
                (listing.source, listing.external_id),
            ).fetchone()
            if latest_price is None or (
                latest_price["price"], latest_price["currency"]
            ) != (listing.price, listing.currency):
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO price_history (
                        source, external_id, price, currency, observed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        listing.source,
                        listing.external_id,
                        listing.price,
                        listing.currency,
                        listing.collected_at,
                    ),
                )
            existing_by_identity: dict[str, list[sqlite3.Row]] = {}
            if listing.image_urls:
                existing_rows = self.connection.execute(
                    """
                    SELECT image_url, local_path FROM listing_images
                    WHERE source = ? AND external_id = ?
                    """,
                    (listing.source, listing.external_id),
                ).fetchall()
                for existing in existing_rows:
                    existing_by_identity.setdefault(
                        image_identity(existing["image_url"]),
                        [],
                    ).append(existing)
            for image_url in listing.image_urls:
                local_path = listing.cached_images.get(image_url)
                identity = image_identity(image_url)
                if identity:
                    for existing in existing_by_identity.get(
                        identity,
                        [],
                    ):
                        local_path = local_path or existing["local_path"]
                        if existing["image_url"] != image_url:
                            self.connection.execute(
                                """
                                DELETE FROM listing_images
                                WHERE source = ? AND external_id = ?
                                  AND image_url = ?
                                """,
                                (
                                    listing.source,
                                    listing.external_id,
                                    existing["image_url"],
                                ),
                            )
                self.connection.execute(
                    """
                    INSERT INTO listing_images (
                        source, external_id, image_url, local_path, cached_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source, external_id, image_url) DO UPDATE SET
                        local_path = COALESCE(
                            excluded.local_path, listing_images.local_path
                        ),
                        cached_at = excluded.cached_at
                    """,
                    (
                        listing.source,
                        listing.external_id,
                        image_url,
                        local_path,
                        listing.collected_at,
                    ),
                )
            self._create_listing_notifications(listing)
            count += 1
        self.connection.commit()
        return count

    def _remember_drom_trim(self, listing: Listing) -> None:
        trim_name = listing.attributes.get("Комплектация", "").strip()
        year = _listing_year(listing)
        brand_key = _vehicle_key(listing.brand)
        model_key = _vehicle_key(listing.model)
        if (
            listing.source != "drom"
            or not trim_name
            or not brand_key
            or not model_key
            or year is None
        ):
            return
        ignored = {"Пробег", "Год выпуска", "Статус на площадке"}
        attributes = {
            key: value
            for key, value in listing.attributes.items()
            if key not in ignored
        }
        self.connection.execute(
            """
            INSERT INTO vehicle_trims (
                brand_key, model_key, year, name, attributes_json,
                source_url, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(brand_key, model_key, year, name) DO UPDATE SET
                attributes_json = excluded.attributes_json,
                source_url = excluded.source_url,
                observed_at = excluded.observed_at
            """,
            (
                brand_key,
                model_key,
                year,
                trim_name,
                json.dumps(attributes, ensure_ascii=False),
                listing.url,
                listing.collected_at,
            ),
        )

    def matching_trims(
        self,
        brand: str | None,
        model: str | None,
        year: int | None,
    ) -> list[dict[str, Any]]:
        if not brand or not model or year is None:
            return []
        rows = self.connection.execute(
            """
            SELECT name, attributes_json, source_url, observed_at
            FROM vehicle_trims
            WHERE brand_key = ? AND model_key = ? AND year = ?
            ORDER BY name COLLATE NOCASE
            """,
            (_vehicle_key(brand), _vehicle_key(model), year),
        ).fetchall()
        result = []
        for row in rows:
            try:
                attributes = json.loads(row["attributes_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                attributes = {}
            result.append(
                {
                    "name": row["name"],
                    "attributes": attributes,
                    "source_url": row["source_url"],
                    "observed_at": row["observed_at"],
                }
            )
        return result

    def vehicle_analysis(
        self,
        brand: str | None,
        model: str | None,
        *,
        trim_name: str | None,
    ) -> dict[str, Any] | None:
        if not brand or not model or not trim_name:
            return None
        row = self.connection.execute(
            """
            SELECT analysis_json, updated_at, trim_name
            FROM vehicle_analyses
            WHERE brand_key = ? AND model_key = ? AND trim_key = ?
            """,
            (
                _vehicle_key(brand),
                _vehicle_key(model),
                _vehicle_key(trim_name),
            ),
        ).fetchone()
        if row is None:
            return None
        try:
            analysis = json.loads(row["analysis_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        return {
            "data": analysis,
            "updated_at": row["updated_at"],
            "trim_name": row["trim_name"],
            "match_kind": "exact_trim",
        }

    def save_vehicle_analysis(
        self,
        brand: str,
        model: str,
        trim_name: str,
        analysis: dict[str, Any],
        *,
        updated_at: str,
    ) -> None:
        from auto_parser.spare_parts import normalize_vehicle_analysis_parts

        analysis = normalize_vehicle_analysis_parts(analysis)
        brand_key = _vehicle_key(brand)
        model_key = _vehicle_key(model)
        trim_key = _vehicle_key(trim_name)
        if not brand_key or not model_key or not trim_key:
            raise ValueError("Для анализа нужны марка, модель и комплектация")
        self.connection.execute(
            """
            INSERT INTO vehicle_analyses (
                brand_key, model_key, trim_key, trim_name, brand, model,
                analysis_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(brand_key, model_key, trim_key) DO UPDATE SET
                trim_name = excluded.trim_name,
                brand = excluded.brand,
                model = excluded.model,
                analysis_json = excluded.analysis_json,
                updated_at = excluded.updated_at
            """,
            (
                brand_key,
                model_key,
                trim_key,
                trim_name,
                brand,
                model,
                json.dumps(analysis, ensure_ascii=False),
                updated_at,
            ),
        )
        self.connection.commit()

    def upsert_spare_part_offers(
        self,
        offers: Iterable[Any],
        *,
        source_list_url: str,
        observed_at: str,
    ) -> int:
        saved = 0
        for offer in offers:
            values = {
                name: getattr(offer, name)
                for name in (
                    "source", "external_id", "source_url", "brand", "model",
                    "generation", "year_from", "year_to", "fuel",
                    "engine_volume_cc", "category", "subcategory", "name", "description", "price",
                    "image_url", "seller", "location",
                )
            }
            self.connection.execute(
                """
                INSERT INTO spare_part_offers (
                    source, external_id, source_url, source_list_url,
                    brand, model, brand_key, model_key, generation,
                    year_from, year_to, fuel, engine_volume_cc, category, subcategory, name,
                    description, price, image_url, seller, location, observed_at
                ) VALUES (
                    :source, :external_id, :source_url, :source_list_url,
                    :brand, :model, :brand_key, :model_key, :generation,
                    :year_from, :year_to, :fuel, :engine_volume_cc, :category, :subcategory, :name,
                    :description, :price, :image_url, :seller, :location, :observed_at
                )
                ON CONFLICT(source, external_id) DO UPDATE SET
                    source_url = excluded.source_url,
                    source_list_url = excluded.source_list_url,
                    brand = excluded.brand,
                    model = excluded.model,
                    brand_key = excluded.brand_key,
                    model_key = excluded.model_key,
                    generation = excluded.generation,
                    year_from = excluded.year_from,
                    year_to = excluded.year_to,
                    fuel = excluded.fuel,
                    engine_volume_cc = excluded.engine_volume_cc,
                    category = COALESCE(excluded.category, spare_part_offers.category),
                    subcategory = COALESCE(excluded.subcategory, spare_part_offers.subcategory),
                    name = excluded.name,
                    description = COALESCE(excluded.description, spare_part_offers.description),
                    price = excluded.price,
                    image_url = COALESCE(excluded.image_url, spare_part_offers.image_url),
                    seller = excluded.seller,
                    location = excluded.location,
                    observed_at = excluded.observed_at
                """,
                {
                    **values,
                    "source_list_url": source_list_url,
                    "brand_key": _vehicle_key(values["brand"]),
                    "model_key": _vehicle_key(values["model"]),
                    "observed_at": observed_at,
                },
            )
            saved += 1
        self.connection.commit()
        return saved

    def spare_part_offers(
        self,
        *,
        brand: str | None = None,
        model: str | None = None,
        category: str | None = None,
        search: str | None = None,
        priced_only: bool = False,
        generation: int | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        offset: int = 0,
        sort: str = "price_asc",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        conditions, values = self._spare_part_filters(
            brand=brand, model=model, category=category, search=search,
            priced_only=priced_only, generation=generation,
            min_price=min_price, max_price=max_price,
        )
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        order_by = {
            "price_desc": "price IS NULL, price DESC, observed_at DESC",
            "newest": "observed_at DESC, price IS NULL, price",
            "name": "name COLLATE NOCASE, price IS NULL, price",
        }.get(sort, "price IS NULL, price, observed_at DESC")
        rows = self.connection.execute(
            f"""
            SELECT * FROM spare_part_offers
            {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            [*values, max(1, min(limit, 500)), max(0, offset)],
        ).fetchall()
        return [dict(row) for row in rows]

    def spare_part_offer_summary(
        self,
        *,
        brand: str | None = None,
        model: str | None = None,
        category: str | None = None,
        search: str | None = None,
        priced_only: bool = False,
        generation: int | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
    ) -> dict[str, Any]:
        conditions, values = self._spare_part_filters(
            brand=brand, model=model, category=category, search=search,
            priced_only=priced_only, generation=generation,
            min_price=min_price, max_price=max_price,
        )
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        row = self.connection.execute(
            f"""
            SELECT COUNT(*) AS offers_count,
                   SUM(price IS NOT NULL) AS priced_count,
                   MIN(price) AS min_price,
                   MAX(price) AS max_price
            FROM spare_part_offers {where}
            """,
            values,
        ).fetchone()
        return dict(row) if row else {
            "offers_count": 0, "priced_count": 0,
            "min_price": None, "max_price": None,
        }

    def spare_part_facets(self) -> dict[str, list[dict[str, Any]]]:
        categories = self.connection.execute(
            """
            SELECT COALESCE(category, 'Без категории') AS value, COUNT(*) AS count
            FROM spare_part_offers
            GROUP BY COALESCE(category, 'Без категории')
            ORDER BY count DESC, value COLLATE NOCASE
            """
        ).fetchall()
        generations = self.connection.execute(
            """
            SELECT generation AS value, COUNT(*) AS count
            FROM spare_part_offers WHERE generation IS NOT NULL
            GROUP BY generation ORDER BY generation
            """
        ).fetchall()
        return {
            "categories": [dict(row) for row in categories],
            "generations": [dict(row) for row in generations],
        }

    @staticmethod
    def _spare_part_filters(
        *,
        brand: str | None,
        model: str | None,
        category: str | None,
        search: str | None,
        priced_only: bool,
        generation: int | None,
        min_price: int | None,
        max_price: int | None,
    ) -> tuple[list[str], list[Any]]:
        conditions: list[str] = []
        values: list[Any] = []
        if brand:
            conditions.append("brand_key = ?")
            values.append(_vehicle_key(brand))
        if model:
            conditions.append("model_key = ?")
            values.append(_vehicle_key(model))
        if category:
            if category == "Без категории":
                conditions.append("category IS NULL")
            else:
                conditions.append("category = ?")
                values.append(category)
        if search:
            conditions.append(
                "(name LIKE ? OR description LIKE ? OR subcategory LIKE ? "
                "OR seller LIKE ? OR location LIKE ?)"
            )
            pattern = f"%{search}%"
            values.extend([pattern] * 5)
        if priced_only:
            conditions.append("price IS NOT NULL")
        if generation is not None:
            conditions.append("generation = ?")
            values.append(generation)
        if min_price is not None:
            conditions.append("price >= ?")
            values.append(min_price)
        if max_price is not None:
            conditions.append("price <= ?")
            values.append(max_price)
        return conditions, values

    def matching_spare_part_offers(
        self,
        brand: str | None,
        model: str | None,
        *,
        year: int | None = None,
        fuel: str | None = None,
        engine_volume: str | None = None,
        generation: int | None = None,
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        if not brand or not model:
            return []
        conditions = ["brand_key = ?", "model_key = ?"]
        values: list[Any] = [_vehicle_key(brand), _vehicle_key(model)]
        if generation is not None:
            conditions.append("(generation IS NULL OR generation = ?)")
            values.append(generation)
        if year:
            conditions.append("(year_from IS NULL OR year_from <= ?)")
            conditions.append("(year_to IS NULL OR year_to >= ?)")
            values.extend((year, year))
        normalized_fuel = str(fuel or "").casefold()
        if normalized_fuel:
            fuel_key = "diesel" if "диз" in normalized_fuel else "gasoline" if "бенз" in normalized_fuel else None
            if fuel_key:
                conditions.append("(fuel IS NULL OR fuel = ?)")
                values.append(fuel_key)
        volume_match = re.search(r"(\d+(?:[.,]\d+)?)", str(engine_volume or ""))
        if volume_match:
            volume_cc = round(float(volume_match.group(1).replace(",", ".")) * 1000)
            conditions.append("(engine_volume_cc IS NULL OR ABS(engine_volume_cc - ?) <= 100)")
            values.append(volume_cc)
        rows = self.connection.execute(
            f"""
            SELECT * FROM spare_part_offers
            WHERE {' AND '.join(conditions)}
            ORDER BY price IS NULL, price, observed_at DESC
            LIMIT ?
            """,
            [*values, max(1, min(limit, 500))],
        ).fetchall()
        return [dict(row) for row in rows]

    def vehicle_analyses(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT brand, model, trim_name,
                   analysis_json, updated_at
            FROM vehicle_analyses
            WHERE trim_key != ''
            ORDER BY brand COLLATE NOCASE, model COLLATE NOCASE,
                     trim_name COLLATE NOCASE
            """
        ).fetchall()
        result = []
        for row in rows:
            try:
                analysis = json.loads(row["analysis_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            result.append(
                {
                    "brand": row["brand"],
                    "model": row["model"],
                    "trim_name": row["trim_name"],
                    "analysis": analysis,
                    "updated_at": row["updated_at"],
                }
            )
        return result

    def listing_trim_assignment(
        self,
        source: str,
        external_id: str,
    ) -> str | None:
        row = self.connection.execute(
            """
            SELECT trim_name FROM listing_trim_assignments
            WHERE source = ? AND external_id = ?
            """,
            (source, external_id),
        ).fetchone()
        return str(row["trim_name"]) if row else None

    def set_listing_trim_assignment(
        self,
        source: str,
        external_id: str,
        trim_name: str,
        *,
        assigned_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO listing_trim_assignments (
                source, external_id, trim_name, assigned_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(source, external_id) DO UPDATE SET
                trim_name = excluded.trim_name,
                assigned_at = excluded.assigned_at
            """,
            (source, external_id, trim_name, assigned_at),
        )
        self.connection.commit()

    def listing_vehicle_assessment(
        self,
        source: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT assessment_json, description_snapshot, updated_at
            FROM listing_vehicle_assessments
            WHERE source = ? AND external_id = ?
            """,
            (source, external_id),
        ).fetchone()
        if row is None:
            return None
        try:
            assessment = json.loads(row["assessment_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        return {
            "data": assessment,
            "description_snapshot": row["description_snapshot"],
            "updated_at": row["updated_at"],
        }

    def save_listing_vehicle_assessment(
        self,
        source: str,
        external_id: str,
        assessment: dict[str, Any],
        *,
        description_snapshot: str | None,
        updated_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO listing_vehicle_assessments (
                source, external_id, assessment_json,
                description_snapshot, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, external_id) DO UPDATE SET
                assessment_json = excluded.assessment_json,
                description_snapshot = excluded.description_snapshot,
                updated_at = excluded.updated_at
            """,
            (
                source,
                external_id,
                json.dumps(assessment, ensure_ascii=False),
                description_snapshot,
                updated_at,
            ),
        )
        self.connection.commit()

    def _create_listing_notifications(self, listing: Listing) -> int:
        rules = self.connection.execute(
            "SELECT * FROM notification_rules WHERE enabled = 1 ORDER BY id"
        ).fetchall()
        created = 0
        for rule in rules:
            if not _listing_matches_notification_rule(listing, rule):
                continue
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO notifications (
                    rule_id, source, external_id, title, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    rule["id"],
                    listing.source,
                    listing.external_id,
                    listing.title,
                    _notification_message(listing, str(rule["name"])),
                    listing.collected_at,
                ),
            )
            created += max(0, cursor.rowcount)
        return created

    def mail_import_processed(self, message_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM mail_imports WHERE message_id = ?",
            (message_id,),
        ).fetchone() is not None

    def record_mail_import(
        self,
        *,
        message_id: str,
        sender: str | None,
        subject: str | None,
        received_at: str | None,
        processed_at: str,
        listing_count: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO mail_imports (
                message_id, sender, subject, received_at,
                processed_at, listing_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                sender,
                subject,
                received_at,
                processed_at,
                listing_count,
            ),
        )
        self.connection.commit()

    def notification_rules(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM notification_rules ORDER BY created_at DESC"
        ).fetchall()

    def add_notification_rule(self, fields: dict[str, Any], *, now: str) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO notification_rules (
                name, enabled, query, brands_json, models_json,
                colors_json, engines_json, min_price, max_price,
                max_mileage, min_year, max_year, min_power,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _notification_rule_values(fields, now=now),
        )
        rule_id = int(cursor.lastrowid)
        self.connection.commit()
        self.evaluate_notification_rule(rule_id)
        return rule_id

    def update_notification_rule(
        self,
        rule_id: int,
        fields: dict[str, Any],
        *,
        now: str,
    ) -> bool:
        existing = self.connection.execute(
            "SELECT * FROM notification_rules WHERE id = ?",
            (rule_id,),
        ).fetchone()
        if existing is None:
            return False
        merged = dict(existing)
        merged.update(fields)
        values = _notification_rule_values(merged, now=now)
        cursor = self.connection.execute(
            """
            UPDATE notification_rules SET
                name = ?, enabled = ?, query = ?, brands_json = ?,
                models_json = ?, colors_json = ?, engines_json = ?,
                min_price = ?, max_price = ?, max_mileage = ?,
                min_year = ?, max_year = ?, min_power = ?, updated_at = ?
            WHERE id = ?
            """,
            (*values[:13], now, rule_id),
        )
        self.connection.commit()
        if cursor.rowcount:
            self.evaluate_notification_rule(rule_id)
        return cursor.rowcount > 0

    def delete_notification_rule(self, rule_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM notification_rules WHERE id = ?",
            (rule_id,),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def evaluate_notification_rule(self, rule_id: int) -> int:
        rule = self.connection.execute(
            "SELECT * FROM notification_rules WHERE id = ? AND enabled = 1",
            (rule_id,),
        ).fetchone()
        if rule is None:
            return 0
        rows = self.connection.execute(
            "SELECT * FROM listings WHERE status = 'active'"
        ).fetchall()
        created = 0
        for row in rows:
            listing = self._row_to_listing(row)
            if not _listing_matches_notification_rule(listing, rule):
                continue
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO notifications (
                    rule_id, source, external_id, title, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    listing.source,
                    listing.external_id,
                    listing.title,
                    _notification_message(listing, str(rule["name"])),
                    listing.collected_at,
                ),
            )
            created += max(0, cursor.rowcount)
        self.connection.commit()
        return created

    def notifications(self, *, unread_only: bool = False) -> list[sqlite3.Row]:
        where = "WHERE n.read_at IS NULL" if unread_only else ""
        return self.connection.execute(
            f"""
            SELECT n.*, r.name AS rule_name, l.url, l.price, l.mileage_km,
                   l.brand, l.model
            FROM notifications n
            JOIN notification_rules r ON r.id = n.rule_id
            LEFT JOIN listings l
              ON l.source = n.source AND l.external_id = n.external_id
            {where}
            ORDER BY n.created_at DESC, n.id DESC
            LIMIT 200
            """
        ).fetchall()

    def mark_notification_read(self, notification_id: int, *, read_at: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE notifications SET read_at = COALESCE(read_at, ?) WHERE id = ?",
            (read_at, notification_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def mark_all_notifications_read(self, *, read_at: str) -> int:
        cursor = self.connection.execute(
            "UPDATE notifications SET read_at = ? WHERE read_at IS NULL",
            (read_at,),
        )
        self.connection.commit()
        return max(0, cursor.rowcount)

    def list_for_validation(
        self,
        *,
        due_before: str | None = None,
        limit: int = 100,
        source: str | None = None,
        displayed_since: str | None = None,
    ) -> list[Listing]:
        conditions = ["status IN ('active', 'hidden')"]
        values: list[Any] = []
        if source is not None:
            conditions.append("source = ?")
            values.append(source)
        if due_before is not None:
            conditions.append(
                "(last_validated_at IS NULL OR last_validated_at <= ?)"
            )
            values.append(due_before)
        displayed_order = (
            """
            CASE WHEN EXISTS (
                SELECT 1 FROM displayed_listings d
                WHERE d.source = listings.source
                  AND d.external_id = listings.external_id
                  AND d.seen_at >= ?
            ) THEN 0 ELSE 1 END,
            """
            if displayed_since is not None
            else ""
        )
        order_values = [displayed_since] if displayed_since is not None else []
        rows = self.connection.execute(
            f"""
            SELECT * FROM listings
            WHERE {" AND ".join(conditions)}
            ORDER BY
                {displayed_order}
                COALESCE(last_validated_at, first_seen_at)
            LIMIT ?
            """,
            (*values, *order_values, max(1, limit)),
        ).fetchall()
        return [self._row_to_listing(row) for row in rows]

    def get_listing(self, source: str, external_id: str) -> Listing | None:
        row = self.connection.execute(
            """
            SELECT * FROM listings
            WHERE source = ? AND external_id = ?
            """,
            (source, external_id),
        ).fetchone()
        if row is None:
            return None
        listing = self._row_to_listing(row)
        images = self.connection.execute(
            """
            SELECT image_url, local_path FROM listing_images
            WHERE source = ? AND external_id = ?
            ORDER BY rowid
            """,
            (source, external_id),
        ).fetchall()
        listing.image_urls = [image["image_url"] for image in images]
        raw_cached_images = {
            image["image_url"]: image["local_path"]
            for image in images
            if image["local_path"]
        }
        listing.image_urls = deduplicate_image_urls(listing.image_urls)
        listing.cached_images = remap_cached_images(
            listing.image_urls,
            raw_cached_images,
        )
        return listing

    def replace_listing_images(self, listing: Listing) -> None:
        image_urls = deduplicate_image_urls(listing.image_urls)
        cached_images = remap_cached_images(image_urls, listing.cached_images)
        self.connection.execute(
            """
            DELETE FROM listing_images
            WHERE source = ? AND external_id = ?
            """,
            (listing.source, listing.external_id),
        )
        for image_url in image_urls:
            self.connection.execute(
                """
                INSERT INTO listing_images (
                    source, external_id, image_url, local_path, cached_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    listing.source,
                    listing.external_id,
                    image_url,
                    cached_images.get(image_url),
                    listing.collected_at,
                ),
            )
        self.connection.commit()

    def list_for_enrichment(
        self,
        source: str,
        external_ids: list[str],
    ) -> list[Listing]:
        if not external_ids:
            return []
        placeholders = ", ".join("?" for _ in external_ids)
        rows = self.connection.execute(
            f"""
            SELECT source, external_id FROM listings
            WHERE source = ?
              AND external_id IN ({placeholders})
              AND (
                description IS NULL
                OR TRIM(description) = ''
                OR NOT EXISTS (
                    SELECT 1 FROM listing_images i
                    WHERE i.source = listings.source
                      AND i.external_id = listings.external_id
                      AND i.image_url LIKE 'https://%.img.avito.st/%'
                )
                OR EXISTS (
                    SELECT 1 FROM listing_images i
                    WHERE i.source = listings.source
                      AND i.external_id = listings.external_id
                      AND i.image_url GLOB
                          '*/image/1/1.??????a3*'
                )
              )
            ORDER BY COALESCE(last_detail_attempt_at, first_seen_at)
            """,
            (source, *external_ids),
        ).fetchall()
        listings = []
        for row in rows:
            listing = self.get_listing(row["source"], row["external_id"])
            if listing is not None:
                listings.append(listing)
        return listings

    def list_for_detail_refresh(
        self,
        source: str,
        *,
        incomplete_due_before: str,
        stale_due_before: str,
        limit: int = 10,
        displayed_since: str | None = None,
    ) -> list[Listing]:
        incomplete = """
            description IS NULL
            OR TRIM(description) = ''
            OR attributes_json IS NULL
            OR attributes_json = '{}'
            OR NOT EXISTS (
                SELECT 1 FROM listing_images i
                WHERE i.source = listings.source
                  AND i.external_id = listings.external_id
                  AND i.image_url LIKE 'https://%'
            )
        """
        displayed_order = (
            """
            CASE WHEN EXISTS (
                SELECT 1 FROM displayed_listings d
                WHERE d.source = listings.source
                  AND d.external_id = listings.external_id
                  AND d.seen_at >= ?
            ) THEN 0 ELSE 1 END,
            """
            if displayed_since is not None
            else ""
        )
        order_values = [displayed_since] if displayed_since is not None else []
        rows = self.connection.execute(
            f"""
            SELECT source, external_id FROM listings
            WHERE source = ?
              AND status = 'active'
              AND (
                (({incomplete}) AND (
                    last_detail_attempt_at IS NULL
                    OR last_detail_attempt_at <= ?
                ))
                OR last_detail_attempt_at IS NULL
                OR last_detail_attempt_at <= ?
              )
            ORDER BY
                {displayed_order}
                CASE WHEN ({incomplete}) THEN 0 ELSE 1 END,
                COALESCE(last_detail_attempt_at, first_seen_at)
            LIMIT ?
            """,
            (
                source,
                incomplete_due_before,
                stale_due_before,
                *order_values,
                max(1, limit),
            ),
        ).fetchall()
        listings = []
        for row in rows:
            listing = self.get_listing(row["source"], row["external_id"])
            if listing is not None:
                listings.append(listing)
        return listings

    def set_displayed_listings(
        self,
        client_id: str,
        listings: list[tuple[str, str]],
        *,
        seen_at: str,
        stale_before: str | None = None,
    ) -> None:
        if stale_before is not None:
            self.connection.execute(
                "DELETE FROM displayed_listings WHERE seen_at < ?",
                (stale_before,),
            )
        self.connection.execute(
            "DELETE FROM displayed_listings WHERE client_id = ?",
            (client_id,),
        )
        self.connection.executemany(
            """
            INSERT INTO displayed_listings (
                client_id, source, external_id, seen_at
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (client_id, source, external_id, seen_at)
                for source, external_id in listings
            ],
        )
        self.connection.commit()

    def mark_detail_attempted(
        self,
        source: str,
        external_id: str,
        attempted_at: str,
    ) -> None:
        self.connection.execute(
            """
            UPDATE listings SET last_detail_attempt_at = ?
            WHERE source = ? AND external_id = ?
            """,
            (attempted_at, source, external_id),
        )
        self.connection.commit()

    def set_listing_hidden(
        self,
        source: str,
        external_id: str,
        hidden: bool,
    ) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE listings SET hidden = ?
            WHERE source = ? AND external_id = ?
            """,
            (1 if hidden else 0, source, external_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def mark_sold(
        self, source: str, external_id: str, validated_at: str
    ) -> None:
        self.connection.execute(
            """
            UPDATE listings
            SET status = 'sold', sold_price = COALESCE(sold_price, price),
                sold_at = COALESCE(sold_at, ?), last_validated_at = ?,
                last_seen_at = ?
            WHERE source = ? AND external_id = ?
            """,
            (validated_at, validated_at, validated_at, source, external_id),
        )
        self.connection.commit()

    def mark_inactive(
        self, source: str, external_id: str, validated_at: str
    ) -> None:
        self.mark_sold(source, external_id, validated_at)

    def set_sold_price(
        self, source: str, external_id: str, sold_price: int
    ) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE listings
            SET sold_price = ?, status = 'sold',
                sold_at = COALESCE(sold_at, last_validated_at, last_seen_at)
            WHERE source = ? AND external_id = ?
            """,
            (sold_price, source, external_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def search_profiles(self, *, due_before: str | None = None) -> list[sqlite3.Row]:
        if due_before is None:
            return self.connection.execute(
                "SELECT * FROM search_profiles ORDER BY created_at"
            ).fetchall()
        return self.connection.execute(
            """
            SELECT * FROM search_profiles
            WHERE enabled = 1
              AND (next_run_at IS NULL OR next_run_at <= ?)
            ORDER BY COALESCE(next_run_at, created_at)
            """,
            (due_before,),
        ).fetchall()

    def add_search_profile(
        self,
        *,
        source: str = "avito",
        query: str,
        region: str,
        radius: int | None,
        min_price: int | None = None,
        max_price: int | None = None,
        interval_minutes: int,
        created_at: str,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO search_profiles (
                source, query, region, radius, min_price, max_price,
                interval_minutes,
                created_at, next_run_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source, query, region, radius, min_price, max_price,
                interval_minutes,
                created_at, created_at,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def remember_search_profile_listings(
        self,
        profile_id: int,
        listings: Iterable[Listing],
    ) -> int:
        count = 0
        for listing in listings:
            self.connection.execute(
                """
                INSERT INTO search_profile_listings (
                    profile_id, source, external_id,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, source, external_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    profile_id,
                    listing.source,
                    listing.external_id,
                    listing.collected_at,
                    listing.collected_at,
                ),
            )
            count += 1
        self.connection.commit()
        return count

    def update_search_profile(
        self, profile_id: int, fields: dict[str, Any]
    ) -> bool:
        allowed = {
            "source",
            "query",
            "region",
            "radius",
            "min_price",
            "max_price",
            "interval_minutes",
            "enabled",
            "next_run_at",
        }
        selected = {key: value for key, value in fields.items() if key in allowed}
        if not selected:
            return False
        assignments = ", ".join(f"{key} = ?" for key in selected)
        cursor = self.connection.execute(
            f"UPDATE search_profiles SET {assignments} WHERE id = ?",
            [*selected.values(), profile_id],
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def delete_search_profile(self, profile_id: int) -> bool:
        self.connection.execute(
            "DELETE FROM search_profile_listings WHERE profile_id = ?",
            (profile_id,),
        )
        cursor = self.connection.execute(
            "DELETE FROM search_profiles WHERE id = ?", (profile_id,)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def finish_search_profile(
        self,
        profile_id: int,
        *,
        last_run_at: str,
        next_run_at: str,
        status: str,
        result_count: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE search_profiles
            SET last_run_at = ?, next_run_at = ?, last_status = ?,
                last_result_count = ?
            WHERE id = ?
            """,
            (last_run_at, next_run_at, status, result_count, profile_id),
        )
        self.connection.commit()

    def begin_search_profile(self, profile_id: int, *, started_at: str) -> None:
        self.connection.execute(
            """
            UPDATE search_profiles
            SET last_run_at = ?, last_status = 'running'
            WHERE id = ?
            """,
            (started_at, profile_id),
        )
        self.connection.commit()

    @staticmethod
    def _row_to_listing(row: sqlite3.Row) -> Listing:
        try:
            attributes = json.loads(row["attributes_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            attributes = {}
        return Listing(
            source=row["source"],
            external_id=row["external_id"],
            url=row["url"],
            title=row["title"],
            price=row["price"],
            currency=row["currency"],
            description=row["description"],
            mileage_km=row["mileage_km"],
            brand=row["brand"],
            model=row["model"],
            views_count=row["views_count"],
            attributes=attributes,
            location=row["location"],
            published_at=row["published_at"],
            status=row["status"],
            sold_price=row["sold_price"],
            sold_at=row["sold_at"],
            last_validated_at=row["last_validated_at"],
            collected_at=row["last_seen_at"],
        )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ListingRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _json_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = [item.strip() for item in value.split(",")]
    else:
        decoded = value
    if not isinstance(decoded, list):
        return []
    return list(dict.fromkeys(
        " ".join(str(item).split())
        for item in decoded
        if str(item).strip()
    ))


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _notification_rule_values(fields: dict[str, Any], *, now: str) -> tuple[Any, ...]:
    return (
        " ".join(str(fields.get("name") or "Новая подборка").split()),
        1 if fields.get("enabled", True) else 0,
        " ".join(str(fields.get("query") or "").split()) or None,
        json.dumps(_json_string_list(fields.get("brands", fields.get("brands_json", []))), ensure_ascii=False),
        json.dumps(_json_string_list(fields.get("models", fields.get("models_json", []))), ensure_ascii=False),
        json.dumps(_json_string_list(fields.get("colors", fields.get("colors_json", []))), ensure_ascii=False),
        json.dumps(_json_string_list(fields.get("engines", fields.get("engines_json", []))), ensure_ascii=False),
        _optional_int(fields.get("min_price")),
        _optional_int(fields.get("max_price")),
        _optional_int(fields.get("max_mileage")),
        _optional_int(fields.get("min_year")),
        _optional_int(fields.get("max_year")),
        _optional_int(fields.get("min_power")),
        str(fields.get("created_at") or now),
        now,
    )


def _attribute_number(listing: Listing, *names: str) -> int | None:
    for name in names:
        value = listing.attributes.get(name)
        if value:
            digits = "".join(character for character in value if character.isdigit())
            if digits:
                return int(digits)
    return None


def _matches_selected(value: str | None, selected: list[str]) -> bool:
    if not selected:
        return True
    normalized = (value or "").casefold()
    return any(item.casefold() in normalized for item in selected)


def _listing_matches_notification_rule(
    listing: Listing,
    rule: sqlite3.Row,
) -> bool:
    query = str(rule["query"] or "").casefold()
    searchable = " ".join(
        filter(None, [listing.title, listing.brand, listing.model, listing.description])
    ).casefold()
    if query and not all(token in searchable for token in query.split()):
        return False
    if not _matches_selected(listing.brand, _json_string_list(rule["brands_json"])):
        return False
    if not _matches_selected(listing.model, _json_string_list(rule["models_json"])):
        return False
    color = listing.attributes.get("Цвет")
    if not _matches_selected(color, _json_string_list(rule["colors_json"])):
        return False
    engine = listing.attributes.get("Тип двигателя") or listing.attributes.get("Двигатель")
    if not _matches_selected(engine, _json_string_list(rule["engines_json"])):
        return False
    minimum_price = _optional_int(rule["min_price"])
    maximum_price = _optional_int(rule["max_price"])
    if minimum_price is not None and (listing.price is None or listing.price < minimum_price):
        return False
    if maximum_price is not None and (listing.price is None or listing.price > maximum_price):
        return False
    maximum_mileage = _optional_int(rule["max_mileage"])
    if maximum_mileage is not None and (
        listing.mileage_km is None or listing.mileage_km > maximum_mileage
    ):
        return False
    year = _attribute_number(listing, "Год выпуска", "Год")
    minimum_year = _optional_int(rule["min_year"])
    maximum_year = _optional_int(rule["max_year"])
    if minimum_year is not None and (year is None or year < minimum_year):
        return False
    if maximum_year is not None and (year is None or year > maximum_year):
        return False
    minimum_power = _optional_int(rule["min_power"])
    power = _attribute_number(listing, "Мощность", "Мощность двигателя")
    if minimum_power is not None and (power is None or power < minimum_power):
        return False
    return True


def _notification_message(listing: Listing, rule_name: str) -> str:
    details: list[str] = []
    if listing.price is not None:
        details.append(f"{listing.price:,} ₽".replace(",", " "))
    if listing.mileage_km is not None:
        details.append(f"{listing.mileage_km:,} км".replace(",", " "))
    suffix = f": {', '.join(details)}" if details else ""
    return f"Подходит под критерий «{rule_name}»{suffix}"

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1

logger = logging.getLogger(__name__)
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[Path] = set()


def configure_sqlite_connection(connection: sqlite3.Connection) -> None:
    """Apply settings which SQLite stores per connection."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")


@contextmanager
def schema_initialization(path: str | Path) -> Iterator[bool]:
    """Serialize schema setup and report whether this process must run it."""
    schema_key = Path(path).resolve()
    with _SCHEMA_LOCK:
        if schema_key in _SCHEMA_READY:
            yield False
            return
        try:
            yield True
        except Exception:
            raise
        else:
            _SCHEMA_READY.add(schema_key)


def prepare_schema_migration(
    connection: sqlite3.Connection,
    path: str | Path,
) -> Path | None:
    """Validate schema version and back up a populated legacy database."""
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Версия базы {current_version} новее поддерживаемой {SCHEMA_VERSION}"
        )
    if current_version == SCHEMA_VERSION or not _has_user_tables(connection):
        return None

    database_path = Path(path).resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = database_path.with_name(
        f"{database_path.stem}.backup-v{current_version}-to-v{SCHEMA_VERSION}-"
        f"{timestamp}.sqlite3"
    )
    suffix = 1
    while backup_path.exists():
        backup_path = database_path.with_name(
            f"{database_path.stem}.backup-v{current_version}-to-v{SCHEMA_VERSION}-"
            f"{timestamp}-{suffix}.sqlite3"
        )
        suffix += 1

    backup_connection = sqlite3.connect(backup_path)
    try:
        connection.backup(backup_connection)
    finally:
        backup_connection.close()
    logger.warning("Перед миграцией создана резервная копия SQLite: %s", backup_path)
    return backup_path


def finish_schema_migration(connection: sqlite3.Connection) -> None:
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


def _has_user_tables(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None

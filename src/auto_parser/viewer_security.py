from __future__ import annotations

import hmac
import hashlib
import json
import secrets
import sqlite3
import time
from http.cookies import SimpleCookie
from pathlib import Path


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
SESSION_COOKIE = "autoscope_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30


def is_local_host(host: str) -> bool:
    return host.strip().casefold() in LOCAL_HOSTS


def validate_remote_access(
    host: str,
    *,
    allow_remote: bool,
) -> None:
    if is_local_host(host):
        return
    if not allow_remote:
        raise ValueError(
            "Нелокальный адрес панели требует явного флага "
            "--allow-remote-viewer"
        )


def ensure_user_schema(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS viewer_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


def user_count(database: Path) -> int:
    ensure_user_schema(database)
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT COUNT(*) FROM viewer_users").fetchone()
    finally:
        connection.close()
    return int(row[0])


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return f"pbkdf2_sha256$240000${salt.hex()}${digest.hex()}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256" or int(rounds) != 240_000:
            return False
        actual = _password_hash(password, bytes.fromhex(salt)).rsplit("$", 1)[-1]
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def create_user(database: Path, name: str, email: str, password: str) -> dict[str, object]:
    ensure_user_schema(database)
    connection = sqlite3.connect(database)
    try:
        cursor = connection.execute(
            "INSERT INTO viewer_users (name, email, password_hash) VALUES (?, ?, ?)",
            (name.strip(), email.strip().casefold(), _password_hash(password)),
        )
        connection.commit()
        user_id = int(cursor.lastrowid)
    finally:
        connection.close()
    return {"id": user_id, "name": name.strip(), "email": email.strip().casefold()}


def authenticate_user(database: Path, email: str, password: str) -> dict[str, object] | None:
    ensure_user_schema(database)
    connection = sqlite3.connect(database)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, name, email, password_hash FROM viewer_users WHERE email = ? COLLATE NOCASE",
            (email.strip(),),
        ).fetchone()
    finally:
        connection.close()
    if row is None or not _password_matches(password, row["password_hash"]):
        return None
    return {"id": int(row["id"]), "name": row["name"], "email": row["email"]}


def create_session(user: dict[str, object], secret: bytes) -> str:
    payload = json.dumps(
        {"id": user["id"], "name": user["name"], "email": user["email"], "exp": int(time.time()) + SESSION_TTL_SECONDS},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def session_user(cookie_header: str | None, secret: bytes) -> dict[str, object] | None:
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header or "")
        token = cookie[SESSION_COOKIE].value
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if int(payload["exp"]) < int(time.time()):
            return None
        return {"id": int(payload["id"]), "name": str(payload["name"]), "email": str(payload["email"])}
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None

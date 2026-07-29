from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import Any

from auto_parser.mail_ingest import MailImportConfig
from auto_parser.models import utc_now_iso
from auto_parser.storage import ListingRepository


def _identifier(value: str) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _rule_payload(row: Any) -> dict[str, Any]:
    payload = dict(row)
    for key in ("brands", "models", "colors", "engines"):
        try:
            payload[key] = json.loads(payload.pop(f"{key}_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            payload[key] = []
    payload["enabled"] = bool(payload.get("enabled"))
    return payload


class NotificationApiMixin:
    database: Path

    def _notification_get_route(self, path: str) -> bool:
        if path == "/api/notifications":
            self._notifications()
            return True
        if path == "/api/notification-rules":
            self._notification_rules()
            return True
        if path == "/api/mail-import/status":
            self._mail_import_status()
            return True
        return False

    def _notification_post_route(self, path: str) -> bool:
        if path == "/api/notification-rules":
            self._create_notification_rule()
            return True
        if path == "/api/notifications/read-all":
            with ListingRepository(self.database) as repository:
                updated = repository.mark_all_notifications_read(
                    read_at=utc_now_iso()
                )
            self._json({"updated": updated})
            return True
        return False

    def _notification_patch_route(self, path: str) -> bool:
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "notification-rules"]:
            self._update_notification_rule(_identifier(parts[2]))
            return True
        if (
            len(parts) == 4
            and parts[:2] == ["api", "notifications"]
            and parts[3] == "read"
        ):
            notification_id = _identifier(parts[2])
            if notification_id is None:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return True
            with ListingRepository(self.database) as repository:
                updated = repository.mark_notification_read(
                    notification_id,
                    read_at=utc_now_iso(),
                )
            self._json({"updated": updated})
            return True
        return False

    def _notification_delete_route(self, path: str) -> bool:
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "notification-rules"]:
            return False
        rule_id = _identifier(parts[2])
        if rule_id is None:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return True
        with ListingRepository(self.database) as repository:
            deleted = repository.delete_notification_rule(rule_id)
        self._json({"deleted": deleted})
        return True

    def _notifications(self) -> None:
        with ListingRepository(self.database) as repository:
            items = [dict(row) for row in repository.notifications()]
        self._json(
            {
                "items": items,
                "unread_count": sum(1 for item in items if not item["read_at"]),
            }
        )

    def _notification_rules(self) -> None:
        with ListingRepository(self.database) as repository:
            items = [_rule_payload(row) for row in repository.notification_rules()]
        self._json({"items": items})

    def _create_notification_rule(self) -> None:
        payload = self._read_json()
        if payload is None:
            return
        if not str(payload.get("name") or "").strip():
            self._json(
                {"error": "Укажите название критерия"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        with ListingRepository(self.database) as repository:
            rule_id = repository.add_notification_rule(
                payload,
                now=utc_now_iso(),
            )
        self._json({"id": rule_id}, HTTPStatus.CREATED)

    def _update_notification_rule(self, rule_id: int | None) -> None:
        if rule_id is None:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        payload = self._read_json()
        if payload is None:
            return
        with ListingRepository(self.database) as repository:
            updated = repository.update_notification_rule(
                rule_id,
                payload,
                now=utc_now_iso(),
            )
        self._json({"updated": updated})

    def _mail_import_status(self) -> None:
        config = MailImportConfig.from_env()
        with ListingRepository(self.database) as repository:
            recent = [
                dict(row)
                for row in repository.connection.execute(
                    """
                    SELECT sender, subject, received_at, processed_at,
                           listing_count
                    FROM mail_imports
                    ORDER BY processed_at DESC
                    LIMIT 10
                    """
                ).fetchall()
            ]
        self._json(
            {
                "configured": config is not None,
                "host": config.host if config else None,
                "username": config.username if config else None,
                "mailbox": config.mailbox if config else None,
                "recent": recent,
            }
        )

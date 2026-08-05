from __future__ import annotations

import base64
import binascii
import hmac
import os
from http import HTTPStatus
from typing import Protocol


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
PASSWORD_ENVIRONMENT_VARIABLE = "AUTOSCOPE_VIEWER_PASSWORD"


class ResponseHandler(Protocol):
    headers: object

    def send_response(self, code: int) -> None: ...
    def send_header(self, keyword: str, value: str) -> None: ...
    def end_headers(self) -> None: ...


def is_local_host(host: str) -> bool:
    return host.strip().casefold() in LOCAL_HOSTS


def resolve_password(explicit_password: str | None) -> str | None:
    password = explicit_password
    if password is None:
        password = os.getenv(PASSWORD_ENVIRONMENT_VARIABLE)
    password = str(password or "").strip()
    return password or None


def validate_remote_access(
    host: str,
    *,
    allow_remote: bool,
    password: str | None,
) -> None:
    if is_local_host(host):
        return
    if not allow_remote:
        raise ValueError(
            "Нелокальный адрес панели требует явного флага "
            "--allow-remote-viewer"
        )
    if not password:
        raise ValueError(
            "Для удалённого доступа задайте пароль через "
            "AUTOSCOPE_VIEWER_PASSWORD или --viewer-password"
        )


def request_is_authorized(
    authorization: str | None,
    password: str | None,
) -> bool:
    if not password:
        return True
    scheme, _, credentials = str(authorization or "").partition(" ")
    candidate = ""
    if scheme.casefold() == "bearer":
        candidate = credentials.strip()
    elif scheme.casefold() == "basic":
        try:
            decoded = base64.b64decode(
                credentials.strip(), validate=True
            ).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return False
        _, separator, candidate = decoded.partition(":")
        if not separator:
            return False
    return hmac.compare_digest(candidate, password)


def authorize_request(handler: ResponseHandler, password: str | None) -> bool:
    authorization = getattr(handler.headers, "get")("Authorization")
    if request_is_authorized(authorization, password):
        return True
    handler.send_response(HTTPStatus.UNAUTHORIZED)
    handler.send_header(
        "WWW-Authenticate",
        'Basic realm="AutoScope", charset="UTF-8"',
    )
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return False

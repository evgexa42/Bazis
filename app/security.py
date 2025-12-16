import hmac
import logging
import secrets
from typing import Tuple

from flask import jsonify, request, session

logger = logging.getLogger("bazis")


def ensure_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _extract_submitted_token() -> str | None:
    header_token = request.headers.get("X-CSRFToken") or request.headers.get("X-CSRF-Token")
    if header_token:
        return str(header_token)

    form_token = request.form.get("csrf_token")
    if form_token:
        return str(form_token)

    try:
        if request.is_json:
            body = request.get_json(silent=True) or {}
            json_token = body.get("csrf_token")
            if json_token:
                return str(json_token)
    except Exception as exc:  # pragma: no cover - защитное логирование
        logger.warning("[security] Ошибка разбора JSON для CSRF", exc_info=exc)

    return None


def validate_csrf_token() -> Tuple[bool, str]:
    session_token = session.get("csrf_token") or ""
    submitted_token = _extract_submitted_token()

    if not session_token or not submitted_token:
        return False, "CSRF token missing or invalid"

    if not hmac.compare_digest(str(session_token), str(submitted_token)):
        return False, "CSRF token mismatch"

    return True, ""


def csrf_error_response(message: str):
    payload = {"status": "error", "message": message or "CSRF validation failed"}
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(payload), 400
    return jsonify(payload), 400


__all__ = [
    "ensure_csrf_token",
    "validate_csrf_token",
    "csrf_error_response",
]
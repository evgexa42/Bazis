"""Простая обёртка для записи событий аудита в таблицу OrderEvent."""

from __future__ import annotations

import logging
from typing import List, Optional

from flask import g, has_request_context, request, session
from sqlalchemy import select

from app.dal.db import OrderEvent, SessionLocal

logger = logging.getLogger("bazis")


def _resolve_actor(default: str = "system") -> str:
    if not has_request_context():
        return default

    user = getattr(g, "current_user", None) or session.get("user")
    return user or default


def _resolve_ip() -> Optional[str]:
    if not has_request_context():
        return None
    return request.remote_addr


def log_order_event(
    action: str,
    *,
    order_name: Optional[str] = None,
    manager: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    user: Optional[str] = None,
    user_ip: Optional[str] = None,
) -> None:
    """Записывает событие без изменения схемы и внешнего API."""

    actor = user or _resolve_actor()
    ip = user_ip or _resolve_ip()

    try:
        with SessionLocal.begin() as db:
            db.add(
                OrderEvent(
                    order_name=order_name or "",
                    manager=manager or "",
                    action=action,
                    old_value=old_value or "",
                    new_value=new_value or "",
                    user=actor,
                    user_ip=ip,
                )
            )
    except Exception as exc:  # pragma: no cover - защитное логирование
        logger.warning(
            "[audit] Не удалось записать событие %s для %s", action, order_name or "<unknown>", exc_info=exc
        )


def fetch_events(limit: int = 200, offset: int = 0) -> List[OrderEvent]:
    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, offset)

    with SessionLocal.begin() as db:
        result = db.execute(
            select(OrderEvent)
            .order_by(OrderEvent.ts.desc())
            .offset(safe_offset)
            .limit(safe_limit)
        ).scalars()
        return list(result)


__all__ = ["fetch_events", "log_order_event"]
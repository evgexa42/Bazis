from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import app.config as app_config
from app.dal.db import SessionLocal
from app.dal.order_timestamps import OrderTimestamp
from app.services.order_numbers import extract_order_number_from_folder, normalize_plus_suffix

logger = logging.getLogger("bazis")

READY_MARKER_RE = re.compile(r"^\s*\[\$\]\s*", re.IGNORECASE)
ANULAT_RE = re.compile(r"\[\s*anulat\s*\]", re.IGNORECASE)


def _utc_from_ts(ts: float | None) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except Exception:
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _clean_name(name: str) -> str:
    # убираем служебные маркеры и лишние пробелы
    cleaned = READY_MARKER_RE.sub("", name or "")
    cleaned = ANULAT_RE.sub("", cleaned)
    cleaned = normalize_plus_suffix(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def build_stable_key(order_name: str) -> str:
    """Инвариантный ключ: номер заказа, иначе имя без служебных меток."""

    order_number = extract_order_number_from_folder(order_name) or ""
    if order_number:
        return order_number.lower()
    return _clean_name(order_name).lower()


def _load_existing(stable_keys: Iterable[str]) -> Dict[str, OrderTimestamp]:
    keys = list({k for k in stable_keys if k})
    if not keys:
        return {}
    with SessionLocal.begin() as session:
        rows = (
            session.query(OrderTimestamp)
            .filter(OrderTimestamp.stable_key.in_(keys))
            .all()
        )
        return {row.stable_key: row for row in rows}


def ensure_records(
    entries: List[dict],
    source: str,
) -> Dict[str, OrderTimestamp]:
    """
    Гарантирует наличие записей created/processed для переданных заказов.
    Возвращает карту stable_key -> запись (после фиксации).
    """

    stable_keys = [build_stable_key(entry.get("name", "")) for entry in entries]
    existing = _load_existing(stable_keys)

    to_update: List[OrderTimestamp] = []
    now = _now_utc()

    for entry, stable_key in zip(entries, stable_keys):
        entry["stable_key"] = stable_key
        name = entry.get("name") or stable_key
        created_candidate = _utc_from_ts(entry.get("ctime_ts")) or _utc_from_ts(entry.get("mtime_ts"))
        processed_candidate = _utc_from_ts(entry.get("mtime_ts")) or now
        is_processed = entry.get("status") in {"Готов", "Подтвержден"} or entry.get("has_technologist")

        record = existing.get(stable_key)
        if not record:
            record = OrderTimestamp(
                stable_key=stable_key,
                order_name=name,
                created_at=created_candidate,
                processed_at=processed_candidate if is_processed else None,
                created_source=source if created_candidate else None,
                processed_source=source if is_processed else None,
                last_seen_at=now,
            )
            existing[stable_key] = record
            to_update.append(record)
            logger.info(
                "[order_ts] created_at зафиксирован %s (%s) source=%s ctime=%s mtime=%s",
                stable_key,
                name,
                source,
                created_candidate,
                entry.get("mtime_ts"),
            )
            if is_processed:
                logger.info(
                    "[order_ts] processed_at зафиксирован сразу при первом обнаружении %s source=%s ts=%s",
                    stable_key,
                    source,
                    processed_candidate,
                )
        else:
            # обновляем динамические поля без перезаписи времён
            if record.order_name != name:
                record.order_name = name
            record.last_seen_at = now
            if record.created_at is None and created_candidate:
                record.created_at = created_candidate
                record.created_source = record.created_source or source
                logger.info(
                    "[order_ts] created_at заполнен задним числом %s source=%s ts=%s",
                    stable_key,
                    source,
                    created_candidate,
                )
            if is_processed and record.processed_at is None:
                record.processed_at = processed_candidate
                record.processed_source = source
                logger.info(
                    "[order_ts] processed_at зафиксирован %s source=%s ts=%s",
                    stable_key,
                    source,
                    processed_candidate,
                )
            to_update.append(record)

    if to_update:
        with SessionLocal.begin() as session:
            for record in to_update:
                session.merge(record)
    return existing


def format_elapsed(created: Optional[datetime], processed: Optional[datetime]) -> str | None:
    if not created or not processed:
        return None
    delta = processed - created
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        logger.warning(
            "[order_ts] Вычислена отрицательная дельта processed-created: %s секунд (c=%s, p=%s)",
            total_seconds,
            created,
            processed,
        )
        return "—"
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}ч {minutes}м"
    if minutes:
        return f"{minutes}м {seconds}с"
    return f"{seconds}с"


def stat_ctime(path: str) -> Optional[float]:
    try:
        return os.path.getctime(path)
    except OSError:
        return None


__all__ = ["build_stable_key", "ensure_records", "format_elapsed", "stat_ctime"]

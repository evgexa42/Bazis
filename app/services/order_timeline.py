from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Dict, Optional

from app.dal.order_timeline import load_timelines, upsert_created, upsert_processed

logger = logging.getLogger("bazis")
_lock = threading.RLock()
_timeline_cache: Dict[str, Dict[str, float | None]] = {}


def _order_key_from_name(name: str | None) -> str:
    if not name:
        return ""
    return (name.split()[0] or "").lower()


def _normalize_ts(value: float | int | None) -> float | None:
    if value is None:
        return None
    try:
        ts_val = float(value)
        return ts_val if ts_val > 0 else None
    except (TypeError, ValueError):
        return None


def _format_ts(ts: float | None) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def load_cache() -> None:
    """Полностью перечитывает кеш из БД."""

    with _lock:
        _timeline_cache.clear()
        try:
            source = load_timelines()
        except Exception as exc:
            logger.warning("[timeline] Не удалось загрузить кеш временных меток", exc_info=exc)
            return

        for key, (created_ts, processed_ts) in source.items():
            _timeline_cache[key] = {
                "created_ts": _normalize_ts(created_ts),
                "processed_ts": _normalize_ts(processed_ts),
            }


def get_timeline(order_name: str | None) -> Dict[str, float | None]:
    key = _order_key_from_name(order_name)
    with _lock:
        stored = _timeline_cache.get(key, {}).copy()
    stored.setdefault("created_ts", None)
    stored.setdefault("processed_ts", None)
    stored["order_key"] = key
    return stored


def mark_created(order_name: str, created_ts: float | int | None = None) -> Optional[float]:
    """Фиксирует время создания, не затирая более ранние значения."""

    key = _order_key_from_name(order_name)
    ts_val = _normalize_ts(created_ts) or time.time()
    if not key:
        return None

    with _lock:
        existing = _timeline_cache.get(key, {})
        if existing.get("created_ts") and existing["created_ts"] <= ts_val:
            return existing["created_ts"]

    saved = upsert_created(key, ts_val)
    with _lock:
        entry = _timeline_cache.setdefault(key, {"created_ts": None, "processed_ts": None})
        if saved is not None:
            entry["created_ts"] = saved
        entry.setdefault("processed_ts", existing.get("processed_ts"))
    return saved


def mark_processed(order_name: str, processed_ts: float | int | None = None) -> Optional[float]:
    """Фиксирует время обработки технологом при первом появлении маркера."""

    key = _order_key_from_name(order_name)
    ts_val = _normalize_ts(processed_ts) or time.time()
    if not key:
        return None

    with _lock:
        existing = _timeline_cache.get(key, {})
        if existing.get("processed_ts"):
            return existing["processed_ts"]

    saved = upsert_processed(key, ts_val)
    with _lock:
        entry = _timeline_cache.setdefault(key, {"created_ts": None, "processed_ts": None})
        if saved is not None:
            entry["processed_ts"] = saved
        entry.setdefault("created_ts", existing.get("created_ts"))
    return saved


def _calc_delta(created_ts: float | None, processed_ts: float | None) -> Optional[int]:
    if created_ts is None or processed_ts is None:
        return None
    return max(0, int(processed_ts - created_ts))


def enrich_entry(entry: dict, *, fallback_created: float | None = None, fallback_processed: float | None = None) -> dict:
    """Заполняет временные поля заказа и при необходимости сохраняет их в БД."""

    if not isinstance(entry, dict):
        return entry

    name = entry.get("name") or ""
    key = entry.get("order_key") or _order_key_from_name(name)
    created_ts = _normalize_ts(entry.get("created_ts")) or _normalize_ts(fallback_created)
    processed_ts = _normalize_ts(entry.get("processed_ts")) or _normalize_ts(fallback_processed)

    current = get_timeline(name)

    if current.get("created_ts") is None and created_ts is not None:
        created_ts = mark_created(name, created_ts) or created_ts
    else:
        created_ts = current.get("created_ts") or created_ts

    if current.get("processed_ts") is None and processed_ts is not None:
        processed_ts = mark_processed(name, processed_ts) or processed_ts
    else:
        processed_ts = current.get("processed_ts") or processed_ts

    entry["order_key"] = key
    entry["created_ts"] = created_ts
    entry["processed_ts"] = processed_ts
    entry["created"] = _format_ts(created_ts) if created_ts else ""
    entry["processed"] = _format_ts(processed_ts) if processed_ts else ""
    entry["processing_seconds"] = _calc_delta(created_ts, processed_ts)

    return entry


# Первичная загрузка кеша сразу после импорта модуля.
load_cache()

__all__ = [
    "enrich_entry",
    "get_timeline",
    "load_cache",
    "mark_created",
    "mark_processed",
]

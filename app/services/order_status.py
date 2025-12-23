import logging
import threading
from copy import deepcopy
from datetime import datetime
from typing import Dict, Iterable, List

import app.config as app_config
from app.dal.external_orders import ExternalOrderRecord, load_status_map, replace_statuses
from app.services.order_numbers import (
    ensure_plus_suffix,
    extract_order_number_from_folder,
    normalize_plus_suffix,
)
from app.services.telegram import folder_has_ready_marker

logger = logging.getLogger("bazis")

_status_cache: Dict[str, ExternalOrderRecord] = {}
_cache_lock = threading.RLock()


def load_cache_from_db() -> None:
    replace_cache(load_status_map().values())


def replace_cache(records: Iterable[ExternalOrderRecord]) -> None:
    with _cache_lock:
        _status_cache.clear()
        for record in records:
            _status_cache[record.order_number] = record


def cache_snapshot() -> Dict[str, ExternalOrderRecord]:
    with _cache_lock:
        return deepcopy(_status_cache)


def save_sync_result(records: List[ExternalOrderRecord]) -> None:
    replace_statuses(records)
    replace_cache(records)


def _apply_cancel_marker(name: str) -> str:
    if "[ANULAT]" in name:
        return name
    if "[$]" in name and app_config.ORDERS_ANULAT_RENAME_TECH_MARKER:
        return name.replace("[$]", "[ANULAT]")
    return f"{name} [ANULAT]"


def enrich_orders(entries: List[dict]) -> List[dict]:
    status_map = cache_snapshot()
    enriched: List[dict] = []

    for entry in entries:
        item = dict(entry)
        folder_name = item.get("name") or ""
        order_number = extract_order_number_from_folder(folder_name) or ""
        status = status_map.get(order_number)

        has_tech_marker = folder_has_ready_marker(folder_name)
        is_cancelled = bool(status and status.cancelled)
        is_approved = bool(status and status.approved and not is_cancelled)
        is_priced = bool(status) and not is_cancelled

        if is_cancelled:
            item["status"] = "ANULAT"
            item["display_name"] = _apply_cancel_marker(folder_name)
        elif is_approved:
            item["status"] = "Подтвержден"
            item["display_name"] = folder_name
        elif has_tech_marker:
            item["status"] = "Готов"
            item["display_name"] = folder_name
        else:
            item["status"] = "Новый"
            item["display_name"] = normalize_plus_suffix(folder_name)

        item["order_number"] = order_number or item.get("order_number", "")
        item["is_cancelled"] = is_cancelled
        item["is_approved"] = is_approved
        item["is_priced"] = is_priced
        item["has_technologist"] = has_tech_marker
        item["confirmed"] = is_approved

        enriched.append(item)

    return enriched


def plan_folder_target_name(
    folder_name: str,
    approved: bool,
    cancelled: bool,
) -> str | None:
    """Возвращает целевое имя папки, если нужно переименовать."""
    target_name = folder_name

    if cancelled and app_config.ORDERS_ANULAT_RENAME_TECH_MARKER and "[$]" in target_name:
        target_name = target_name.replace("[$]", "[ANULAT]")

    plus_target = ensure_plus_suffix(
        target_name,
        approved=approved and not cancelled,
        rename_enabled=app_config.ORDERS_APPROVED_PLUS_RENAME,
    )
    if plus_target:
        target_name = plus_target

    if target_name != folder_name:
        return target_name
    return None


__all__ = [
    "cache_snapshot",
    "enrich_orders",
    "load_cache_from_db",
    "plan_folder_target_name",
    "save_sync_result",
]
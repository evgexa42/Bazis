import os
import re
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import app
import app.config as app_config
from app.dal.json_store import load_json_file, save_json_atomic
from app.paths import resolve_path
from app.services.audit import log_order_event

CPU_STATE_FILE = resolve_path("cpu_monitor_state.json")

MONTH_NAMES_RO = {
    1: "Ianuarie",
    2: "Februarie",
    3: "Martie",
    4: "Aprilie",
    5: "Mai",
    6: "Iunie",
    7: "Iulie",
    8: "August",
    9: "Septembrie",
    10: "Octombrie",
    11: "Noiembrie",
    12: "Decembrie",
}

_state_lock = threading.RLock()
_scan_lock = threading.Lock()
_state: Dict = {"orders": {}, "manager_overrides": {}, "pdf_cache": {}, "updated_at": 0}
_last_scan_ts = 0.0
_SCAN_TTL = 10.0


def _normalize_spaces(value: str) -> str:
    return " ".join((value or "").split())


def _strip_manager_suffix(folder_name: str) -> Tuple[str, str]:
    normalized = _normalize_spaces(folder_name)
    match = re.search(r"\s*\[([^\]]+)\]\s*$", normalized)
    if not match:
        return normalized, ""
    base = _normalize_spaces(normalized[: match.start()])
    tag = (match.group(1) or "").strip()
    return base, tag


def _extract_order_key(folder_name: str) -> str:
    base, _ = _strip_manager_suffix(folder_name)
    base = _normalize_spaces(base)
    match = re.match(r"^([0-9]+-[0-9]+)\s+(.+)$", base)
    if match:
        return f"{match.group(1).upper()}|{_normalize_spaces(match.group(2)).upper()}"
    return base.upper()


def _build_pdf_candidates(folder_name: str) -> List[str]:
    base, _ = _strip_manager_suffix(folder_name)
    full_norm = _normalize_spaces(folder_name)
    base_norm = _normalize_spaces(base)
    return ["CPU.pdf", f"{base_norm}.pdf", f"{full_norm}.pdf"]


def _load_state() -> None:
    loaded = load_json_file(CPU_STATE_FILE)
    if isinstance(loaded, dict):
        with _state_lock:
            _state.clear()
            _state.update(
                {
                    "orders": loaded.get("orders") or {},
                    "manager_overrides": loaded.get("manager_overrides") or {},
                    "pdf_cache": loaded.get("pdf_cache") or {},
                    "updated_at": loaded.get("updated_at") or 0,
                }
            )


def _save_state() -> None:
    with _state_lock:
        payload = {
            "orders": _state.get("orders") or {},
            "manager_overrides": _state.get("manager_overrides") or {},
            "pdf_cache": _state.get("pdf_cache") or {},
            "updated_at": int(time.time()),
        }
    save_json_atomic(CPU_STATE_FILE, payload)


def _resolve_owner_manager(folder_name: str, order_key: str) -> str:
    with _state_lock:
        manual = (_state.get("manager_overrides") or {}).get(order_key)
    if manual:
        return manual

    base, tag = _strip_manager_suffix(folder_name)
    tag_mapping = (app_config.CONFIG.get("desene_cpu", {}) or {}).get("manager_tags") or {}
    if tag and isinstance(tag_mapping, dict):
        manager_from_tag = str(tag_mapping.get(tag) or "").strip()
        if manager_from_tag:
            return manager_from_tag

    manager = app.get_manager_from_name(base)
    return manager if manager else "Не назначен"


def _month_label(year: int, month: int) -> str:
    return f"{month:02d}. {MONTH_NAMES_RO.get(month, '')} {year}"


def _iter_month_paths(root: str) -> List[Tuple[int, int, str]]:
    cfg = app_config.CONFIG.get("desene_cpu", {}) if isinstance(app_config.CONFIG, dict) else {}
    mode = str(cfg.get("scan_mode") or "recent").strip().lower()
    months_count = max(1, min(12, int(cfg.get("scan_months") or 3)))
    selected = cfg.get("selected_months") or []

    now = datetime.now()
    targets: List[Tuple[int, int]] = []
    if mode == "current":
        targets = [(now.year, now.month)]
    elif mode == "manual" and isinstance(selected, list):
        for raw in selected:
            if not isinstance(raw, str) or "-" not in raw:
                continue
            y, m = raw.split("-", 1)
            try:
                year = int(y)
                month = int(m)
            except ValueError:
                continue
            if 1 <= month <= 12:
                targets.append((year, month))
    else:
        for offset in range(months_count):
            total = now.year * 12 + (now.month - 1) - offset
            targets.append((total // 12, total % 12 + 1))

    uniq = []
    seen = set()
    for year, month in targets:
        key = f"{year}-{month:02d}"
        if key in seen:
            continue
        seen.add(key)
        month_path = os.path.join(root, str(year), _month_label(year, month))
        uniq.append((year, month, month_path))
    return uniq


def _check_pdf_for_folder(order_path: str, folder_name: str) -> Tuple[bool, str]:
    try:
        st = os.stat(order_path)
        dir_mtime = float(st.st_mtime)
    except OSError:
        return False, ""

    candidates = _build_pdf_candidates(folder_name)
    cache_key = os.path.normcase(os.path.abspath(order_path))
    with _state_lock:
        cache = (_state.get("pdf_cache") or {}).get(cache_key)

    if isinstance(cache, dict) and float(cache.get("dir_mtime") or 0.0) == dir_mtime:
        return bool(cache.get("has_pdf")), str(cache.get("pdf_name") or "")

    has_pdf = False
    matched = ""
    candidate_set = {name.casefold() for name in candidates}
    try:
        with os.scandir(order_path) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                if not entry.name.lower().endswith(".pdf"):
                    continue
                if entry.name.casefold() in candidate_set:
                    has_pdf = True
                    matched = entry.name
                    break
    except OSError:
        has_pdf = False

    with _state_lock:
        _state.setdefault("pdf_cache", {})[cache_key] = {
            "dir_mtime": dir_mtime,
            "has_pdf": has_pdf,
            "pdf_name": matched,
            "checked_at": int(time.time()),
        }
    return has_pdf, matched


def _can_mutate(order: dict, role: str, username: str) -> bool:
    if role in {"admin", "technologist"}:
        return True
    if role == "manager":
        return (order.get("owner_manager") or "") == (username or "")
    return False


def scan_cpu_orders(force: bool = False) -> dict:
    global _last_scan_ts
    if not force and (time.time() - _last_scan_ts) < _SCAN_TTL:
        return get_cpu_payload()

    if not _scan_lock.acquire(blocking=False):
        return get_cpu_payload()

    try:
        root = app_config.DESENE_CPU_ROOT or ""
        if not root or not os.path.isdir(root):
            return get_cpu_payload()

        with _state_lock:
            orders = dict(_state.get("orders") or {})

        seen_keys = set()
        for year, month, month_path in _iter_month_paths(root):
            if not os.path.isdir(month_path):
                continue
            try:
                with os.scandir(month_path) as it:
                    for entry in it:
                        if not entry.is_dir():
                            continue
                        folder_name = entry.name
                        order_path = os.path.join(month_path, folder_name)
                        order_key = _extract_order_key(folder_name)
                        has_pdf, pdf_name = _check_pdf_for_folder(order_path, folder_name)
                        existing = orders.get(order_key) or {}
                        status = existing.get("status") or "новый"
                        if status == "подтвержден" and not has_pdf:
                            # архив подтверждений не теряем
                            pass
                        elif not has_pdf and status == "новый":
                            # новый без PDF не показываем
                            continue

                        if existing and existing.get("path") and existing.get("path") != order_path:
                            log_order_event(
                                "cpu_path_changed",
                                order_name=folder_name,
                                old_value=existing.get("path") or "",
                                new_value=order_path,
                            )

                        updated = {
                            "order_key": order_key,
                            "folder_name": folder_name,
                            "path": order_path,
                            "year": year,
                            "month": month,
                            "month_label": _month_label(year, month),
                            "status": status,
                            "owner_manager": _resolve_owner_manager(folder_name, order_key),
                            "has_pdf": has_pdf,
                            "pdf_name": pdf_name,
                            "updated_at": int(time.time()),
                            "last_action_ts": int(existing.get("last_action_ts") or 0),
                            "send_count": int(existing.get("send_count") or 0),
                        }
                        orders[order_key] = updated
                        seen_keys.add(order_key)
            except OSError:
                continue

        with _state_lock:
            # не удаляем архивные/проверяемые записи из state
            for key, item in list(orders.items()):
                if key in seen_keys:
                    continue
                if (item.get("status") or "") == "новый":
                    item["has_pdf"] = False
                    orders[key] = item
            _state["orders"] = orders
            _state["updated_at"] = int(time.time())

        _save_state()
        _last_scan_ts = time.time()
        return get_cpu_payload()
    finally:
        _scan_lock.release()


def get_cpu_payload() -> dict:
    with _state_lock:
        orders = list((_state.get("orders") or {}).values())

    active = [
        item
        for item in orders
        if (item.get("status") in {"новый", "на проверке"}) and (item.get("has_pdf") or item.get("status") != "новый")
    ]
    active.sort(key=lambda x: ((x.get("status") != "новый"), -(x.get("updated_at") or 0)))

    archive = [
        item
        for item in orders
        if item.get("status") in {"на проверке", "подтвержден"}
    ]
    archive.sort(key=lambda x: -(x.get("last_action_ts") or x.get("updated_at") or 0))

    grouped: Dict[str, Dict[str, List[dict]]] = {}
    for item in archive:
        year_key = str(item.get("year") or "—")
        month_key = item.get("month_label") or "—"
        grouped.setdefault(year_key, {})
        grouped[year_key].setdefault(month_key, [])
        grouped[year_key][month_key].append(item)

    return {"active": active, "archive": archive, "archive_grouped": grouped, "updated_at": int(time.time())}


def assign_manager(order_key: str, manager_name: str, user: str = "") -> Optional[dict]:
    with _state_lock:
        orders = _state.setdefault("orders", {})
        item = orders.get(order_key)
        if not item:
            return None
        _state.setdefault("manager_overrides", {})[order_key] = manager_name
        item["owner_manager"] = manager_name
        item["updated_at"] = int(time.time())
        orders[order_key] = item
    _save_state()
    log_order_event(
        "cpu_manager_override",
        order_name=item.get("folder_name") or order_key,
        old_value="",
        new_value=manager_name,
        user=user or None,
    )
    return item


def send_to_customer(order_key: str, role: str, username: str) -> Tuple[bool, str, Optional[dict]]:
    with _state_lock:
        item = (_state.get("orders") or {}).get(order_key)
        if not item:
            return False, "Заказ не найден.", None
        if not _can_mutate(item, role, username):
            return False, "Недостаточно прав для этого заказа.", None
        before = item.get("status") or "новый"
        if before == "новый":
            item["status"] = "на проверке"
        item["send_count"] = int(item.get("send_count") or 0) + 1
        item["last_action_ts"] = int(time.time())
        item["updated_at"] = int(time.time())
        _state["orders"][order_key] = item
    _save_state()
    log_order_event(
        "cpu_send",
        order_name=item.get("folder_name") or order_key,
        manager=item.get("owner_manager") or "",
        old_value=before,
        new_value=item.get("status") or "",
        user=username or None,
    )
    return True, "", item


def confirm_order(order_key: str, role: str, username: str) -> Tuple[bool, str, Optional[dict]]:
    with _state_lock:
        item = (_state.get("orders") or {}).get(order_key)
        if not item:
            return False, "Заказ не найден.", None
        if not _can_mutate(item, role, username):
            return False, "Недостаточно прав для этого заказа.", None
        before = item.get("status") or "новый"
        item["status"] = "подтвержден"
        item["last_action_ts"] = int(time.time())
        item["updated_at"] = int(time.time())
        _state["orders"][order_key] = item
    _save_state()
    log_order_event(
        "cpu_confirm",
        order_name=item.get("folder_name") or order_key,
        manager=item.get("owner_manager") or "",
        old_value=before,
        new_value="подтвержден",
        user=username or None,
    )
    return True, "", item


_load_state()

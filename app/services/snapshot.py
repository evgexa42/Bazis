import calendar
import hashlib
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from time import perf_counter
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import app as bazis_app
import app.config as app_config
from app import get_manager_from_name, heartbeat, logger, measure_time, sse_broadcast
from app.services import order_status
from app.services.order_times import (
    cleanup_expired_records,
    fetch_order_times,
    normalize_order_key,
    update_processed_for_names,
    upsert_orders_seen,
)
from app.services.order_numbers import extract_order_number_from_folder
from app.services.audit import log_order_event
from app.dal.db import DB_PATH
from app.dal.order_manager_override import get_overrides_map
from app.services import telegram as telegram_service
from app.services.telegram import folder_has_ready_marker, technologist_from_folder

MONTHS_RO = {
    1: "01. Ianuarie",
    2: "02. Februarie",
    3: "03. Martie",
    4: "04. Aprilie",
    5: "05. Mai",
    6: "06. Iunie",
    7: "07. Iulie",
    8: "08. August",
    9: "09. Septembrie",
    10: "10. Octombrie",
    11: "11. Noiembrie",
    12: "12. Decembrie",
}

CHISINAU_TZ = ZoneInfo("Europe/Chisinau")


def _format_modified(ts_value: float) -> str:
    try:
        return datetime.fromtimestamp(ts_value).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def _shift_months(dt: datetime, months_back: int) -> datetime:
    """Сдвиг даты на указанное число календарных месяцев назад."""
    assert months_back > 0, "period must be positive"
    year = dt.year
    month = dt.month - months_back
    while month <= 0:
        month += 12
        year -= 1

    last_day = calendar.monthrange(year, month)[1]
    safe_day = min(dt.day, last_day)
    return dt.replace(year=year, month=month, day=safe_day)


def calculate_period_cutoff(months_back: int) -> float:
    """Возвращает timestamp начала периода поиска с учётом часового пояса."""
    months = max(1, months_back)
    now = datetime.now(CHISINAU_TZ)
    start_dt = _shift_months(now, months)
    cutoff_ts = start_dt.timestamp()
    logger.debug(
        "[search] period cutoff %s months => %s", months, start_dt.isoformat()
    )
    return cutoff_ts


def parse_folder_entry(
    folder_name: str, stat_mtime: Optional[float] = None, now_ts: Optional[float] = None
):
    if telegram_service.is_folder_ignored(folder_name):
        return None

    folder_path = os.path.join(app_config.FOLDER_PATH or "", folder_name)
    try:
        mtime_ts = stat_mtime if stat_mtime is not None else os.path.getmtime(folder_path)
    except OSError:
        return None

    now_ts = now_ts or time.time()
    manager = get_manager_from_name(folder_name)
    technologist = technologist_from_folder(folder_name) or "Неизвестно"

    confirmed = folder_name.endswith("+")
    status = "Подтвержден" if confirmed else ("Готов" if folder_has_ready_marker(folder_name) else "Новый")
    order_number = extract_order_number_from_folder(folder_name) or ""
    order_key = normalize_order_key(folder_name)

    days_ago = int((now_ts - mtime_ts) // 86400)

    return {
        "name": folder_name,
        "status": status,
        "manager": manager,
        "technologist": technologist,
        "mtime_ts": mtime_ts,
        "modified": "",
        "days": days_ago,
        "confirmed": confirmed,
        "order_number": order_number,
        "order_key": order_key,
}


def _apply_manager_overrides(entries: List[Dict]) -> None:
    if not entries:
        return

    order_keys: list[str] = []
    for entry in entries:
        order_key = entry.get("order_key") or normalize_order_key(entry.get("name") or "")
        if order_key:
            entry["order_key"] = order_key
            order_keys.append(order_key)

    if not order_keys:
        return

    overrides = get_overrides_map(order_keys)
    for entry in entries:
        order_key = entry.get("order_key") or ""
        record = overrides.get(order_key)
        if not record:
            continue
        entry["manager"] = record.manager_name
        entry["manager_override"] = True
        entry["manager_override_by"] = record.updated_by or ""
        entry["manager_override_at"] = (
            record.updated_at.isoformat() if record.updated_at else ""
        )

INDEX_TTL = 300.0
_index_lock = threading.Lock()
_index_access_lock = threading.RLock()


def _apply_snapshot(new_snapshot: List[Dict], *, lock_held: bool = False) -> tuple[bool, List[Dict], int, float]:
    now = time.time()
    new_snapshot = list(new_snapshot)
    new_snapshot.sort(key=lambda x: x.get("mtime_ts", 0.0), reverse=True)
    for item in new_snapshot:
        item["modified"] = _format_modified(item.get("mtime_ts", 0.0))

    def _apply_locked():
        if new_snapshot == bazis_app.orders_snapshot:
            bazis_app.last_snapshot_update = now
            bazis_app.last_snapshot_ts = now
            version_val = bazis_app.orders_version
            last_ts_val = bazis_app.last_snapshot_ts
            applied = list(bazis_app.orders_snapshot)
            return False, applied, version_val, last_ts_val
        bazis_app.orders_snapshot = new_snapshot
        bazis_app.orders_version += 1
        bazis_app.last_snapshot_update = now
        bazis_app.last_snapshot_ts = now
        version_val = bazis_app.orders_version
        last_ts_val = bazis_app.last_snapshot_ts
        applied = list(bazis_app.orders_snapshot)
        return True, applied, version_val, last_ts_val

    if lock_held:
        changed, applied_snapshot, version, last_ts = _apply_locked()
    else:
        with bazis_app.orders_snapshot_lock:
            changed, applied_snapshot, version, last_ts = _apply_locked()

    with bazis_app.metrics_lock:
        snap = bazis_app.metrics["snapshot"]
        snap["version"] = bazis_app.orders_version
        snap["last_update_ts"] = now
        snap["orders_count"] = len(new_snapshot)

    return changed, applied_snapshot, version, last_ts


@measure_time("build_orders_snapshot")
def build_orders_snapshot():
    t0 = perf_counter()
    if not app_config.FOLDER_PATH or not os.path.isdir(app_config.FOLDER_PATH):
        return []

    now_ts = time.time()
    folder_data: List[Dict] = []

    try:
        with os.scandir(app_config.FOLDER_PATH) as it:
            for entry in it:
                if not entry.is_dir():
                    continue

                parsed = parse_folder_entry(entry.name, stat_mtime=entry.stat().st_mtime, now_ts=now_ts)
                if parsed:
                    folder_data.append(parsed)
    except FileNotFoundError:
        folder_data = []

    try:
        folder_data = order_status.enrich_orders(folder_data)
    except Exception as exc:  # pragma: no cover - защитная логика
        logger.warning("[snapshot] enrich_orders failed, fallback to raw data", exc_info=exc)
    _apply_manager_overrides(folder_data)

    dt = (perf_counter() - t0) * 1000.0
    with bazis_app.metrics_lock:
        snap = bazis_app.metrics["snapshot"]
        snap["last_update_ts"] = time.time()
        snap["last_build_ms"] = dt
        snap["build_count"] += 1
        snap["avg_build_ms"] += (dt - snap["avg_build_ms"]) / snap["build_count"]
        snap["orders_count"] = len(folder_data)

    return folder_data


@measure_time("refresh_orders_snapshot")
def refresh_orders_snapshot(force: bool = False):
    now = time.time()
    if not force:
        with bazis_app.orders_snapshot_lock:
            if bazis_app.orders_snapshot and now - bazis_app.last_snapshot_update < bazis_app.SNAPSHOT_TTL:
                return list(bazis_app.orders_snapshot)

    snapshot = build_orders_snapshot()
    changed, applied_snapshot, version, last_snapshot_ts = _apply_snapshot(snapshot)

    try:
        folder_names = [item.get("name") or "" for item in applied_snapshot]
        upsert_orders_seen(folder_names)
        update_processed_for_names(folder_names)
        cleanup_expired_records(app_config.ORDER_TIMES_TTL_DAYS)
    except Exception as exc:  # pragma: no cover - защитная логика
        logger.warning("[order_times] Обновление временных меток не удалось", exc_info=exc)

    _attach_order_times(applied_snapshot)

    if changed:
        manager_stats = {name: 0 for name in app_config.MANAGER_NAMES}
        manager_stats["Неизвестно"] = manager_stats.get("Неизвестно", 0)
        tech_stats = {name: 0 for name in app_config.TECHNOLOGIST_MARKERS.values()}
        tech_stats["Неизвестно"] = tech_stats.get("Неизвестно", 0)

        for folder in applied_snapshot:
            manager_name = folder.get("manager") or "Неизвестно"
            manager_stats.setdefault(manager_name, 0)
            manager_stats[manager_name] += 1

            technologist_name = folder.get("technologist") or "Неизвестно"
            tech_stats.setdefault(technologist_name, 0)
            tech_stats[technologist_name] += 1

        sse_broadcast({
            "type": "orders_snapshot",
            "orders": applied_snapshot,
            "folders": applied_snapshot,
            "total": len(applied_snapshot),
            "managers": manager_stats,
            "technologists": tech_stats,
            "version": version,
            "last_snapshot_ts": last_snapshot_ts,
        })

    return applied_snapshot


def get_orders_snapshot(ttl: float = bazis_app.SNAPSHOT_TTL):
    now = time.time()
    with bazis_app.orders_snapshot_lock:
        if bazis_app.orders_snapshot and now - bazis_app.last_snapshot_update < ttl:
            return list(bazis_app.orders_snapshot)

    return refresh_orders_snapshot(force=True)


def _merge_order(entry: dict) -> tuple[bool, List[Dict], int, float, bool]:
    # атомарно читаем+меняем snapshot под одним локом
    with bazis_app.orders_snapshot_lock:
        snapshot = list(bazis_app.orders_snapshot)
        existed = False

        for idx, item in enumerate(snapshot):
            if item.get("name") == entry.get("name"):
                existed = True
                if item == entry:
                    return (
                        False,
                        list(bazis_app.orders_snapshot),
                        bazis_app.orders_version,
                        bazis_app.last_snapshot_ts,
                        existed,
                    )
                snapshot[idx] = entry
                break
        else:
            snapshot.append(entry)

        changed, applied_snapshot, version, last_snapshot_ts = _apply_snapshot(snapshot, lock_held=True)
    return changed, applied_snapshot, version, last_snapshot_ts, existed


def upsert_order(folder_name: str) -> bool:
    parsed = parse_folder_entry(folder_name)
    if not parsed:
        return remove_order(folder_name)

    try:
        upsert_orders_seen([folder_name])
        update_processed_for_names([folder_name])
    except Exception as exc:  # pragma: no cover - защитная логика
        logger.warning("[order_times] Не удалось обновить время для %s", folder_name, exc_info=exc)

    parsed = order_status.enrich_orders([parsed])[0]
    _apply_manager_overrides([parsed])
    changed, applied_snapshot, version, last_snapshot_ts, existed = _merge_order(parsed)
    if changed:
        payload = build_orders_payload(
            folders=applied_snapshot,
            version=version,
            last_snapshot_ts=last_snapshot_ts,
        )
        sse_broadcast(payload)
        if not existed:
            log_order_event(
                "create",
                order_name=folder_name,
                manager=parsed.get("manager"),
                new_value=parsed.get("status"),
            )
    return changed


def remove_order(folder_name: str) -> bool:
    # тоже атомарно формируем новый snapshot под локом
    with bazis_app.orders_snapshot_lock:
        snapshot = [
            item for item in bazis_app.orders_snapshot
            if item.get("name") != folder_name
        ]
        removed_item = next(
            (item for item in bazis_app.orders_snapshot if item.get("name") == folder_name),
            None,
        )

        changed, applied_snapshot, version, last_snapshot_ts = _apply_snapshot(snapshot, lock_held=True)
    if changed:
        payload = build_orders_payload(
            folders=applied_snapshot,
            version=version,
            last_snapshot_ts=last_snapshot_ts,
        )
        sse_broadcast(payload)
        log_order_event(
            "delete",
            order_name=folder_name,
            manager=(removed_item or {}).get("manager"),
            old_value=(removed_item or {}).get("status"),
        )
    return changed


def _apply_manager_filter(folders, visible_manager, requested_manager):
    applied_manager_filter = visible_manager
    if applied_manager_filter is None and requested_manager not in {"", "Все"}:
        applied_manager_filter = requested_manager

    if applied_manager_filter:
        folders = [
            folder
            for folder in folders
            if (folder.get("manager") or "Неизвестно") == applied_manager_filter
        ]

    return folders, applied_manager_filter


def _parse_iso(ts_value: Optional[str]) -> Optional[datetime]:
    if not ts_value:
        return None
    try:
        return datetime.fromisoformat(ts_value)
    except ValueError:
        return None


def _attach_order_times(folders: List[Dict]) -> None:
    if not folders:
        return

    order_keys = []
    for folder in folders:
        order_key = folder.get("order_key") or normalize_order_key(folder.get("name") or "")
        if order_key:
            folder["order_key"] = order_key
            order_keys.append(order_key)

    if not order_keys:
        return

    times_map = fetch_order_times(order_keys)
    now = datetime.now(timezone.utc)

    for folder in folders:
        order_key = folder.get("order_key") or ""
        record = times_map.get(order_key)
        if not record:
            continue
        folder["created_at"] = record.created_at
        folder["processed_at"] = record.processed_at
        if record.processed_at:
            folder["is_processed"] = True

        created_dt = _parse_iso(record.created_at)
        processed_dt = _parse_iso(record.processed_at) if record.processed_at else None
        if created_dt:
            end_dt = processed_dt or now
            elapsed = int(max(0, (end_dt - created_dt).total_seconds()))
            folder["elapsed_seconds"] = elapsed
            if processed_dt:
                folder["processing_seconds"] = int(max(0, (processed_dt - created_dt).total_seconds()))


def build_orders_payload(visible_manager=None, requested_manager="Все", folders=None, version=None, last_snapshot_ts=None):
    if folders is None:
        folders = get_orders_snapshot(ttl=bazis_app.SNAPSHOT_TTL)
    if version is None or last_snapshot_ts is None:
        with bazis_app.orders_snapshot_lock:
            version = bazis_app.orders_version if version is None else version
            last_snapshot_ts = bazis_app.last_snapshot_ts if last_snapshot_ts is None else last_snapshot_ts
    folders, _ = _apply_manager_filter(folders, visible_manager, requested_manager)
    _attach_order_times(folders)

    total_orders = len(folders)
    manager_stats = {name: 0 for name in app_config.MANAGER_NAMES}
    manager_stats["Неизвестно"] = manager_stats.get("Неизвестно", 0)
    for folder in folders:
        manager_name = folder.get("manager") or "Неизвестно"
        manager_stats.setdefault(manager_name, 0)
        manager_stats[manager_name] += 1

    tech_stats = {name: 0 for name in app_config.TECHNOLOGIST_MARKERS.values()}
    tech_stats["Неизвестно"] = tech_stats.get("Неизвестно", 0)
    for folder in folders:
        technologist_name = folder.get("technologist") or "Неизвестно"
        tech_stats.setdefault(technologist_name, 0)
        tech_stats[technologist_name] += 1

    return {
        "type": "orders_snapshot",
        "orders": folders,
        "folders": folders,
        "total": total_orders,
        "managers": manager_stats,
        "technologists": tech_stats,
        "version": version,
        "last_snapshot_ts": last_snapshot_ts,
    }


def apply_manager_override_to_snapshot(order_key: str, manager_name: str, updated_by: str = "", updated_at: str = "") -> Optional[Dict]:
    if not order_key or not manager_name:
        return None

    normalized_key = normalize_order_key(order_key)
    updated_item = None

    with bazis_app.orders_snapshot_lock:
        snapshot = list(bazis_app.orders_snapshot)
        for idx, item in enumerate(snapshot):
            item_key = item.get("order_key") or normalize_order_key(item.get("name") or "")
            if item_key != normalized_key and (item.get("name") or "") != order_key:
                continue
            updated = dict(item)
            updated["manager"] = manager_name
            updated["manager_override"] = True
            updated["manager_override_by"] = updated_by
            updated["manager_override_at"] = updated_at
            snapshot[idx] = updated
            updated_item = updated
            break

        if updated_item is None:
            return None

        changed, applied_snapshot, version, last_snapshot_ts = _apply_snapshot(snapshot, lock_held=True)

    if changed:
        payload = build_orders_payload(
            folders=applied_snapshot,
            version=version,
            last_snapshot_ts=last_snapshot_ts,
        )
        sse_broadcast(payload)

    return updated_item


def get_sqlite_connection(retries: int = 3, retry_delay: float = 0.25):
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        conn: Optional[sqlite3.Connection] = None  # гарантируем определение на случай исключений
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
            cur = conn.cursor()
            cur.execute("PRAGMA busy_timeout=30000;")
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA temp_store=MEMORY;")
            cur.execute("PRAGMA cache_size=-20000;")
            return conn
        except sqlite3.OperationalError as exc:
            last_error = exc
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
            time.sleep(retry_delay)
    if last_error:
        raise last_error
    raise sqlite3.OperationalError("Unable to open database connection")


def ensure_search_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS search_index (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          base_key TEXT NOT NULL,
          month TEXT NOT NULL,
          name TEXT NOT NULL,
          name_lc TEXT NOT NULL,
          manager TEXT,
          path TEXT NOT NULL UNIQUE,
          mtime_ts REAL NOT NULL
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_search_name_lc ON search_index(name_lc);")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_search_manager ON search_index(manager);")


def collect_month_scope(months_back: Optional[int]) -> List[str]:
    if months_back is None:
        return get_recent_months()

    raw_limit: Optional[int]
    if months_back == 0:
        raw_limit = None
    else:
        try:
            raw_limit = max(1, min(24, int(months_back)))
        except (TypeError, ValueError):
            raw_limit = None

    month_candidates: list[tuple[float, str]] = []
    seen: set[str] = set()

    for base_folder in app_config.SEARCH_FOLDERS.values():
        if not base_folder or not os.path.isdir(base_folder):
            continue
        try:
            with os.scandir(base_folder) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    if entry.name in seen:
                        continue
                    month_candidates.append((entry.stat().st_mtime, entry.name))
                    seen.add(entry.name)
        except FileNotFoundError:
            continue

    month_candidates.sort(key=lambda item: item[0], reverse=True)
    ordered_months = [name for _, name in month_candidates]
    effective_limit = None if raw_limit is None else min(24, raw_limit + 1)
    if effective_limit:
        ordered_months = ordered_months[:effective_limit]

    if not ordered_months:
        fallback = raw_limit if raw_limit is not None else months_back
        if fallback:
            fallback = min(24, fallback + 1)
        return get_recent_months(fallback if fallback else None)
    return ordered_months


def refresh_search_index(full: bool = False, months_back: Optional[int] = None, month_scope: Optional[List[str]] = None):
    now = time.time()
    if not full:
        with bazis_app.order_index_lock:
            if bazis_app.order_index_updated_at and now - bazis_app.order_index_updated_at < INDEX_TTL:
                return False

    if not _index_lock.acquire(blocking=False):
        return False

    conn = None
    try:
        with _index_access_lock:
            if not full:
                with bazis_app.order_index_lock:
                    if bazis_app.order_index_updated_at and time.time() - bazis_app.order_index_updated_at < INDEX_TTL:
                        return False

            conn = get_sqlite_connection()
            ensure_search_table(conn)
            cur = conn.cursor()

            search_months = month_scope or collect_month_scope(months_back)
            all_rows: List[Tuple] = []
            cleanup_batches: List[Tuple[str, str, Set[str]]] = []

            for base_key, base_folder in app_config.SEARCH_FOLDERS.items():
                if not base_folder:
                    continue
                for month in search_months:
                    month_path = os.path.join(base_folder, month)
                    if not os.path.isdir(month_path):
                        continue

                    paths_set: set[str] = set()
                    try:
                        with os.scandir(month_path) as it:
                            for entry in it:
                                if not entry.is_dir():
                                    continue
                                name = entry.name
                                full_path = os.path.join(month_path, name)
                                paths_set.add(full_path)
                                mtime_ts = entry.stat().st_mtime
                                manager = get_manager_from_name(name)
                                all_rows.append(
                                    (
                                        base_key,
                                        month,
                                        name,
                                        name.lower(),
                                        manager,
                                        full_path,
                                        mtime_ts,
                                    )
                                )
                    except FileNotFoundError:
                        paths_set = set()

                    cleanup_batches.append((base_key, month, paths_set))

            if all_rows:
                cur.execute("BEGIN")
                cur.executemany(
                    """
                    INSERT INTO search_index(base_key, month, name, name_lc, manager, path, mtime_ts)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        name=excluded.name,
                        name_lc=excluded.name_lc,
                        manager=excluded.manager,
                        mtime_ts=excluded.mtime_ts
                    """,
                    all_rows,
                )
                cur.execute("COMMIT")

            for base_key, month, paths_set in cleanup_batches:
                if paths_set:
                    placeholders = ",".join("?" for _ in paths_set)
                    cur.execute(
                        f"DELETE FROM search_index WHERE base_key=? AND month=? AND path NOT IN ({placeholders})",
                        (base_key, month, *paths_set),
                    )
                else:
                    cur.execute(
                        "DELETE FROM search_index WHERE base_key=? AND month=?",
                        (base_key, month),
                    )

            conn.commit()

            with bazis_app.order_index_lock:
                bazis_app.order_index_updated_at = time.time()
        heartbeat("indexer")
        return True
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        _index_lock.release()


@measure_time("search")
def search_in_index(
    query: str, months_scope: Optional[List[str]] = None, cutoff_ts: Optional[float] = None
):
    cleaned_query, month_filters = _extract_month_filters(query)
    normalized_query = (cleaned_query or "").strip().lower()
    keys = list(app_config.SEARCH_FOLDERS.keys())
    results = {key: [] for key in keys}
    fallback_key = "Результаты" if not results else "Прочее"
    results.setdefault(fallback_key, [])

    if not normalized_query:
        return results

    with _index_access_lock:
        conn = get_sqlite_connection()
        ensure_search_table(conn)
        cur = conn.cursor()
        sql = (
            "SELECT base_key, name, manager, path FROM search_index "
            "WHERE name_lc LIKE ?"
        )
        params: List[object] = [f"%{normalized_query}%"]

        merged_filters = list(month_filters)
        if months_scope:
            scope_set = set(months_scope)
            if merged_filters:
                merged_filters = [m for m in merged_filters if m in scope_set]
            else:
                merged_filters = list(scope_set)

        if merged_filters:
            placeholders = ",".join("?" for _ in merged_filters)
            sql += f" AND month IN ({placeholders})"
            params.extend(merged_filters)

        if cutoff_ts:
            sql += " AND mtime_ts >= ?"
            params.append(float(cutoff_ts))

        sql += " ORDER BY mtime_ts DESC"

        rows = cur.execute(sql, params).fetchall()
        conn.close()

    for base_key, name, manager, path in rows:
        target_key = base_key if base_key in results else fallback_key
        results.setdefault(target_key, [])
        results[target_key].append({"name": name, "manager": manager or "", "path": path})

    return results


def background_snapshot_updater(interval: float = 900.0):
    while True:
        try:
            refresh_orders_snapshot(force=False)
            refresh_search_index()
        except Exception as exc:
            logger.exception("[snapshot] Ошибка фонового обновления", exc_info=exc)
        heartbeat("snapshot_updater")
        time.sleep(interval)


def _month_label(month_num: int, year: int) -> str:
    normalized_month = month_num % 12 or 12
    if month_num <= 0:
        year -= 1
    return f"{MONTHS_RO[normalized_month]} {year}"


def get_recent_months(months_back: Optional[int] = None) -> List[str]:
    now = datetime.now()
    months: List[str] = []

    months_count = months_back if months_back is not None else app_config.SEARCH_MONTHS
    try:
        months_count = max(1, min(12, int(months_count)))
    except (TypeError, ValueError):
        months_count = 2

    base_index = now.year * 12 + (now.month - 1)

    for offset in range(months_count):
        idx = base_index - offset
        month_num = idx % 12 + 1
        months.append(_month_label(month_num, idx // 12))

    return months


def _extract_month_filters(query: str) -> Tuple[str, List[str]]:
    if not query:
        return "", []

    month_filters: List[str] = []
    normalized_query = query.strip()
    now = datetime.now()

    for match in re.findall(r"-(\d{1,2})\b", normalized_query):
        try:
            month_num = int(match)
        except (TypeError, ValueError):
            continue
        if month_num not in MONTHS_RO:
            continue
        year = now.year if month_num <= now.month else now.year - 1
        month_filters.append(f"{MONTHS_RO[month_num]} {year}")

    cleaned_query = re.sub(r"-(\d{1,2})\b", "", normalized_query).strip()

    unique_filters = list(dict.fromkeys(month_filters))
    return cleaned_query, unique_filters


import hashlib
import hashlib
import os
import sqlite3
import time
from datetime import datetime
from time import perf_counter
from typing import Dict, List, Optional, Set, Tuple

import app as bazis_app
from app import get_manager_from_name, heartbeat, logger, measure_time, sse_broadcast
from app.dal.db import DB_PATH
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


def _format_modified(ts_value: float) -> str:
    try:
        return datetime.fromtimestamp(ts_value).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def parse_folder_entry(
    folder_name: str, stat_mtime: Optional[float] = None, now_ts: Optional[float] = None
):
    if folder_name in telegram_service.IGNORED_FOLDERS:
        return None

    folder_path = os.path.join(bazis_app.FOLDER_PATH or "", folder_name)
    try:
        mtime_ts = stat_mtime if stat_mtime is not None else os.path.getmtime(folder_path)
    except OSError:
        return None

    now_ts = now_ts or time.time()
    manager = get_manager_from_name(folder_name)
    technologist = technologist_from_folder(folder_name) or "Неизвестно"

    confirmed = folder_name.endswith("+")
    status = "Подтвержден" if confirmed else ("Готов" if folder_has_ready_marker(folder_name) else "Новый")
    order_number = folder_name.split()[0] if folder_name else ""
    if confirmed and order_number.endswith("+"):
        order_number = order_number.rstrip("+")

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
    }


def _apply_snapshot(new_snapshot: List[Dict]) -> bool:
    now = time.time()
    new_snapshot = list(new_snapshot)
    new_snapshot.sort(key=lambda x: x.get("mtime_ts", 0.0), reverse=True)
    for item in new_snapshot:
        item["modified"] = _format_modified(item.get("mtime_ts", 0.0))

    with bazis_app.orders_snapshot_lock:
        if new_snapshot == bazis_app.orders_snapshot:
            bazis_app.last_snapshot_update = now
            bazis_app.last_snapshot_ts = now
            return False
        bazis_app.orders_snapshot = new_snapshot
        bazis_app.orders_version += 1
        bazis_app.last_snapshot_update = now
        bazis_app.last_snapshot_ts = now

    with bazis_app.metrics_lock:
        snap = bazis_app.metrics["snapshot"]
        snap["version"] = bazis_app.orders_version
        snap["last_update_ts"] = now
        snap["orders_count"] = len(new_snapshot)

    return True


@measure_time("build_orders_snapshot")
def build_orders_snapshot():
    t0 = perf_counter()
    if not bazis_app.FOLDER_PATH or not os.path.isdir(bazis_app.FOLDER_PATH):
        return []

    now_ts = time.time()
    folder_data: List[Dict] = []

    try:
        with os.scandir(bazis_app.FOLDER_PATH) as it:
            for entry in it:
                if not entry.is_dir():
                    continue

                parsed = parse_folder_entry(entry.name, stat_mtime=entry.stat().st_mtime, now_ts=now_ts)
                if parsed:
                    folder_data.append(parsed)
    except FileNotFoundError:
        folder_data = []

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
    changed = _apply_snapshot(snapshot)
    if changed:
        sse_broadcast(build_orders_payload())
    refresh_search_index()
    return list(snapshot)


def get_orders_snapshot(ttl: float = bazis_app.SNAPSHOT_TTL):
    now = time.time()
    with bazis_app.orders_snapshot_lock:
        if bazis_app.orders_snapshot and now - bazis_app.last_snapshot_update < ttl:
            return list(bazis_app.orders_snapshot)

    return refresh_orders_snapshot(force=True)


def _merge_order(entry: dict) -> bool:
    with bazis_app.orders_snapshot_lock:
        snapshot = list(bazis_app.orders_snapshot)

    replaced = False
    for idx, item in enumerate(snapshot):
        if item.get("name") == entry.get("name"):
            if item == entry:
                return False
            snapshot[idx] = entry
            replaced = True
            break
    if not replaced:
        snapshot.append(entry)

    return _apply_snapshot(snapshot)


def upsert_order(folder_name: str) -> bool:
    parsed = parse_folder_entry(folder_name)
    if not parsed:
        return remove_order(folder_name)

    changed = _merge_order(parsed)
    if changed:
        sse_broadcast(build_orders_payload())
    return changed


def remove_order(folder_name: str) -> bool:
    with bazis_app.orders_snapshot_lock:
        snapshot = [item for item in bazis_app.orders_snapshot if item.get("name") != folder_name]

    changed = _apply_snapshot(snapshot)
    if changed:
        sse_broadcast(build_orders_payload())
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


def build_orders_payload(visible_manager=None, requested_manager="Все"):
    folders = get_orders_snapshot(ttl=bazis_app.SNAPSHOT_TTL)
    folders, _ = _apply_manager_filter(folders, visible_manager, requested_manager)

    total_orders = len(folders)
    manager_stats = {name: 0 for name in bazis_app.MANAGER_NAMES}
    manager_stats["Неизвестно"] = manager_stats.get("Неизвестно", 0)
    for folder in folders:
        manager_name = folder.get("manager") or "Неизвестно"
        manager_stats.setdefault(manager_name, 0)
        manager_stats[manager_name] += 1

    tech_stats = {name: 0 for name in bazis_app.TECHNOLOGIST_MARKERS.values()}
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
        "version": bazis_app.orders_version,
        "last_snapshot_ts": bazis_app.last_snapshot_ts,
    }


def get_sqlite_connection(retries: int = 3, retry_delay: float = 0.25):
    last_error: Optional[Exception] = None
    for attempt in range(retries):
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


def refresh_search_index(full: bool = False):
    conn = get_sqlite_connection()
    ensure_search_table(conn)
    cur = conn.cursor()

    search_months = get_recent_months()
    all_rows: List[Tuple] = []
    cleanup_batches: List[Tuple[str, str, Set[str]]] = []

    for base_key, base_folder in bazis_app.SEARCH_FOLDERS.items():
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
    conn.close()

    with bazis_app.order_index_lock:
        bazis_app.order_index_updated_at = time.time()
    heartbeat("indexer")


@measure_time("search")
def search_in_index(query: str):
    normalized_query = (query or "").strip().lower()
    keys = list(bazis_app.SEARCH_FOLDERS.keys())
    results = {key: [] for key in keys}
    fallback_key = "Результаты" if not results else "Прочее"
    results.setdefault(fallback_key, [])

    if not normalized_query:
        return results

    conn = get_sqlite_connection()
    ensure_search_table(conn)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT base_key, name, manager, path FROM search_index WHERE name_lc LIKE ? ORDER BY mtime_ts DESC",
        (f"%{normalized_query}%",),
    ).fetchall()
    conn.close()

    for base_key, name, manager, path in rows:
        target_key = base_key if base_key in results else fallback_key
        results.setdefault(target_key, [])
        results[target_key].append({"name": name, "manager": manager or "", "path": path})

    return results


def background_snapshot_updater(interval: float = 180.0):
    last_hash = None
    while True:
        try:
            snapshot = build_orders_snapshot()
            changed = _apply_snapshot(snapshot)
            current_hash = hashlib.sha256(str(snapshot).encode("utf-8", errors="ignore")).hexdigest()
            if changed or last_hash != current_hash:
                sse_broadcast(build_orders_payload())
                last_hash = current_hash
            refresh_search_index()
        except Exception as exc:
            logger.exception("[snapshot] Ошибка фонового обновления", exc_info=exc)
        heartbeat("snapshot_updater")
        time.sleep(interval)


def get_recent_months():
    now = datetime.now()
    year = now.year
    months = []

    for i in range(2):
        month_num = now.month - i
        if month_num <= 0:
            month_num += 12
            year -= 1
        months.append(f"{MONTHS_RO[month_num]} {year}")

    return months
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from app.dal.db import DB_PATH
from app.services.telegram import folder_has_ready_marker

BRACKET_MARK_RE = re.compile(r"\[[^\]]*?\]")
ORDER_NUMBER_RE = re.compile(r"^(\d+-\d+)\b")


@dataclass
class OrderTimesRecord:
    order_key: str
    created_at: str
    processed_at: Optional[str]
    last_seen_at: str
    last_display_name: Optional[str]
    updated_at: str


def normalize_order_key(folder_name: str) -> str:
    """Стабильный ключ для заказа: убираем маркеры, +, пробелы и косметику."""
    if not folder_name:
        return ""

    base = os.path.basename(folder_name)
    cleaned = BRACKET_MARK_RE.sub("", base)
    cleaned = re.sub(r"\s*\+\s*$", "", cleaned)
    cleaned = " ".join(cleaned.strip().split())

    match = ORDER_NUMBER_RE.match(cleaned)
    if match:
        return match.group(1)
    return cleaned


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_sqlite_connection(retries: int = 3, retry_delay: float = 0.25):
    last_error: Optional[Exception] = None
    for _ in range(retries):
        conn: Optional[sqlite3.Connection] = None
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


def ensure_order_times_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_times (
          order_key TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          processed_at TEXT NULL,
          last_seen_at TEXT NOT NULL,
          last_display_name TEXT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )


def upsert_orders_seen(folder_names: Iterable[str]) -> None:
    now = now_iso()
    rows = []
    for folder_name in folder_names:
        order_key = normalize_order_key(folder_name)
        if not order_key:
            continue
        rows.append((order_key, now, now, folder_name, now))

    if not rows:
        return

    conn = get_sqlite_connection()
    try:
        ensure_order_times_table(conn)
        conn.executemany(
            """
            INSERT INTO order_times(order_key, created_at, last_seen_at, last_display_name, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(order_key) DO UPDATE SET
              last_seen_at=excluded.last_seen_at,
              last_display_name=excluded.last_display_name,
              updated_at=excluded.updated_at
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def mark_processed(order_key: str) -> Optional[str]:
    if not order_key:
        return None
    now = now_iso()
    conn = get_sqlite_connection()
    try:
        ensure_order_times_table(conn)
        conn.execute(
            """
            INSERT INTO order_times(order_key, created_at, processed_at, last_seen_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(order_key) DO UPDATE SET
              processed_at=CASE
                WHEN order_times.processed_at IS NULL THEN excluded.processed_at
                ELSE order_times.processed_at
              END,
              updated_at=excluded.updated_at
            """,
            (order_key, now, now, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return now


def update_processed_from_name(folder_name: str) -> Optional[str]:
    if not folder_name or not folder_has_ready_marker(folder_name):
        return None
    order_key = normalize_order_key(folder_name)
    return mark_processed(order_key)


def update_processed_for_names(folder_names: Iterable[str]) -> None:
    names = [name for name in folder_names if folder_has_ready_marker(name)]
    if not names:
        return

    now = now_iso()
    rows = []
    for folder_name in names:
        order_key = normalize_order_key(folder_name)
        if not order_key:
            continue
        rows.append((order_key, now, now, now, now))

    if not rows:
        return

    conn = get_sqlite_connection()
    try:
        ensure_order_times_table(conn)
        conn.executemany(
            """
            INSERT INTO order_times(order_key, created_at, processed_at, last_seen_at, updated_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(order_key) DO UPDATE SET
              processed_at=CASE
                WHEN order_times.processed_at IS NULL THEN excluded.processed_at
                ELSE order_times.processed_at
              END,
              last_seen_at=excluded.last_seen_at,
              updated_at=excluded.updated_at
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_expired_records(ttl_days: int) -> int:
    ttl_days = max(1, int(ttl_days))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)
    cutoff_iso = cutoff.replace(microsecond=0).isoformat()

    conn = get_sqlite_connection()
    try:
        ensure_order_times_table(conn)
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM order_times WHERE last_seen_at < ?",
            (cutoff_iso,),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


def fetch_order_times(order_keys: Iterable[str]) -> dict[str, OrderTimesRecord]:
    keys = [key for key in order_keys if key]
    if not keys:
        return {}

    conn = get_sqlite_connection()
    try:
        ensure_order_times_table(conn)
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"""
            SELECT order_key, created_at, processed_at, last_seen_at, last_display_name, updated_at
            FROM order_times
            WHERE order_key IN ({placeholders})
            """,
            keys,
        ).fetchall()
    finally:
        conn.close()

    result: dict[str, OrderTimesRecord] = {}
    for row in rows:
        record = OrderTimesRecord(
            order_key=row[0],
            created_at=row[1],
            processed_at=row[2],
            last_seen_at=row[3],
            last_display_name=row[4],
            updated_at=row[5],
        )
        result[record.order_key] = record
    return result


__all__ = [
    "normalize_order_key",
    "upsert_orders_seen",
    "update_processed_from_name",
    "update_processed_for_names",
    "cleanup_expired_records",
    "fetch_order_times",
]
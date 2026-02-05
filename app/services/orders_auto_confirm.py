import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

import app.config as app_config
from app import heartbeat
from app.services.monitor import discard_programmatic_move, register_programmatic_move
from app.services.order_numbers import PLUS_SUFFIX_RE, extract_order_number_from_folder

logger = logging.getLogger("bazis")

_engine_lock = threading.Lock()
_pg_engine: Engine | None = None
_worker_started = False
_recent_renames: dict[str, float] = {}
_recent_renames_lock = threading.Lock()

# CONFIGURE QUERY HERE: адаптируйте таблицу/поле под вашу БД Orders.
DEFAULT_QUERY = """
SELECT approved
FROM public.orders
WHERE number = :order_number
ORDER BY created_at DESC
LIMIT 1
"""


def _safe_dsn(dsn: str) -> str:
    if not dsn:
        return ""
    if "@" not in dsn:
        return dsn
    before, after = dsn.rsplit("@", 1)
    scheme = before.split("://", 1)[0] if "://" in before else "postgres"
    return f"{scheme}://***@{after}"


def _get_engine() -> Engine:
    global _pg_engine
    if _pg_engine:
        return _pg_engine

    with _engine_lock:
        if _pg_engine:
            return _pg_engine

        dsn = app_config.ORDERS_PG_URL
        if not dsn:
            raise RuntimeError("ORDERS_PG_URL is not configured")

        _pg_engine = create_engine(
            dsn,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
            future=True,
        )
        return _pg_engine


def _cleanup_recent_renames(now_ts: float) -> None:
    with _recent_renames_lock:
        expired = [path for path, exp in _recent_renames.items() if exp < now_ts]
        for path in expired:
            _recent_renames.pop(path, None)


def _mark_recent_rename(path: str, ttl: float = 10.0) -> None:
    with _recent_renames_lock:
        _recent_renames[path] = time.monotonic() + max(1.0, ttl)


def _is_recent_rename(path: str) -> bool:
    now_ts = time.monotonic()
    _cleanup_recent_renames(now_ts)
    with _recent_renames_lock:
        return _recent_renames.get(path, 0) >= now_ts


def is_order_given_to_work(order_no: str) -> bool:
    """Возвращает True, если заказ «дали в работу» в Orders."""
    if not order_no:
        return False

    dsn = app_config.ORDERS_PG_URL
    if not dsn:
        return False
    query = DEFAULT_QUERY

    try:
        engine = _get_engine()
    except Exception as exc:
        logger.warning(
            "[orders_auto_confirm] PostgreSQL недоступен (%s): %s",
            _safe_dsn(dsn),
            exc,
        )
        return False

    try:
        with engine.connect() as conn:
            try:
                conn.execute(text("SET statement_timeout TO 5000"))
            except Exception:
                pass
            row = conn.execute(text(query), {"order_number": order_no}).fetchone()
    except Exception as exc:
        logger.warning(
            "[orders_auto_confirm] Ошибка запроса PostgreSQL (%s): %s",
            _safe_dsn(dsn),
            exc,
        )
        return False

    if not row:
        return False
    return bool(row[0])


def _iter_order_folders(base_path: str, max_depth: int = 3) -> Iterable[tuple[str, str]]:
    if not base_path or not os.path.isdir(base_path):
        return []

    stack = [(base_path, 1)]
    results: list[tuple[str, str]] = []

    while stack:
        current_path, depth = stack.pop()
        try:
            with os.scandir(current_path) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    results.append((entry.path, entry.name))
                    if depth < max_depth:
                        stack.append((entry.path, depth + 1))
        except OSError as exc:
            logger.warning(
                "[orders_auto_confirm] Не удалось прочитать каталог %s: %s",
                current_path,
                exc,
            )

    return results


def _should_skip_folder(folder_name: str, folder_path: str) -> bool:
    if not folder_name or PLUS_SUFFIX_RE.search(folder_name):
        return True
    if _is_recent_rename(folder_path):
        return True
    return False


def _rename_folder(folder_path: str, folder_name: str, order_no: str) -> None:
    parent_dir = os.path.dirname(folder_path)
    target_name = f"{folder_name} +"
    target_path = os.path.join(parent_dir, target_name)

    if os.path.exists(target_path):
        logger.warning(
            "[orders_auto_confirm] Пропуск переименования %s -> %s: цель уже существует",
            folder_name,
            target_name,
        )
        return

    register_programmatic_move(folder_path, target_path)
    try:
        os.rename(folder_path, target_path)
    except OSError as exc:
        logger.warning(
            "[orders_auto_confirm] Не удалось переименовать %s -> %s: %s",
            folder_name,
            target_name,
            exc,
        )
        discard_programmatic_move(folder_path, target_path)
        return

    _mark_recent_rename(target_path)
    logger.info(
        "[orders_auto_confirm] Автоподтверждение: %s -> %s (order=%s, ts=%s)",
        folder_name,
        target_name,
        order_no,
        datetime.now(timezone.utc).isoformat(),
    )


def scan_not_given_folder_once() -> None:
    base_path = app_config.ORDERS_AUTO_CONFIRM_PATH
    if not base_path:
        return

    folders = _iter_order_folders(base_path, max_depth=3)
    for folder_path, folder_name in folders:
        if _should_skip_folder(folder_name, folder_path):
            continue

        order_no = extract_order_number_from_folder(folder_name) or ""
        if not order_no:
            continue

        if not is_order_given_to_work(order_no):
            continue

        _rename_folder(folder_path, folder_name, order_no)


def start_auto_confirm_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def worker() -> None:
        delay = 90
        while True:
            try:
                scan_not_given_folder_once()
            except Exception as exc:
                logger.warning("[orders_auto_confirm] Ошибка автоподтверждения: %s", exc, exc_info=exc)
            heartbeat("orders_auto_confirm")
            time.sleep(delay)

    threading.Thread(target=worker, daemon=True).start()


__all__ = ["is_order_given_to_work", "scan_not_given_folder_once", "start_auto_confirm_worker"]

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import List

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

import app.config as app_config
from app import heartbeat
from app.dal.external_orders import ExternalOrderRecord
from app.services import order_status
from app.services.monitor import discard_programmatic_move, move_known_folder, register_programmatic_move
from app.services.order_numbers import (
    created_at_cutoff,
    extract_order_info_from_sql,
    extract_order_number_from_folder,
)
from app.services.snapshot import refresh_orders_snapshot, remove_order, upsert_order
from app.services.telegram import update_message_for_folder

logger = logging.getLogger("bazis")

_engine_lock = threading.Lock()
_pg_engine: Engine | None = None
_sync_started = False


def _safe_dsn(dsn: str) -> str:
    if not dsn:
        return ""
    if "@" not in dsn:
        return dsn
    before, after = dsn.rsplit("@", 1)
    # пример before: postgresql+psycopg2://user:pass
    scheme = before.split("://", 1)[0] if "://" in before else "postgres"
    return f"{scheme}://***@{after}"


def _get_engine() -> Engine:
    global _pg_engine
    if _pg_engine:
        return _pg_engine

    with _engine_lock:
        if _pg_engine:
            return _pg_engine

        if not app_config.ORDERS_PG_URL:
            raise RuntimeError("ORDERS_PG_URL is not configured")

        _pg_engine = create_engine(
            app_config.ORDERS_PG_URL,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
            future=True,
        )
        return _pg_engine


def _max_dt(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _fetch_external_orders(now: datetime) -> tuple[List[ExternalOrderRecord], bool]:
    """
    Читаем Orders из Postgres, режем по окну created_at и дедупим по order_number.
    В реальной базе часто бывает, что:
      - есть строка "864-12"
      - есть строка "864-12 anulat"
    После нормализации это один order_number => надо мержить, иначе SQLite PK падает.
    """
    cutoff = created_at_cutoff(now, app_config.ORDERS_PG_TAIL_DAYS)

    try:
        engine = _get_engine()
    except Exception as exc:
        safe_dsn = _safe_dsn(app_config.ORDERS_PG_URL or "")
        logger.warning(
            "[orders_sync] Инициализация подключения PostgreSQL не удалась (%s): %s",
            safe_dsn,
            exc,
        )
        return [], False

    try:
        with engine.connect() as conn:
            # statement_timeout на уровне запроса (может требовать прав; если не даёт — просто уберём позже)
            try:
                conn.execute(text("SET statement_timeout TO 5000"))
            except Exception:
                pass

            rows = conn.execute(
                text(
                    """
                    SELECT number, approved, created_at
                    FROM public.orders
                    WHERE created_at >= :cutoff
                      AND number ~ '^[0-9]+-[0-9]+'
                    """
                ),
                {"cutoff": cutoff},
            ).fetchall()
    except Exception as exc:
        safe_dsn = _safe_dsn(app_config.ORDERS_PG_URL)
        logger.warning(
            "[orders_sync] Не удалось получить данные из PostgreSQL (%s): %s",
            safe_dsn,
            exc,
        )
        return [], False

    merged: dict[str, ExternalOrderRecord] = {}

    for raw_number, approved, created_at in rows:
        parsed = extract_order_info_from_sql(raw_number or "")
        if not parsed:
            continue

        num = parsed.number
        cancelled = bool(parsed.cancelled)
        approved_bool = bool(approved)

        prev = merged.get(num)
        if prev is None:
            merged[num] = ExternalOrderRecord(
                order_number=num,
                approved=approved_bool,
                cancelled=cancelled,
                source_created_at=created_at,
                last_seen_at=now,
            )
        else:
            merged[num] = ExternalOrderRecord(
                order_number=num,
                approved=bool(prev.approved or approved_bool),
                cancelled=bool(prev.cancelled or cancelled),
                source_created_at=_max_dt(prev.source_created_at, created_at),
                last_seen_at=now,
            )

    records = list(merged.values())
    return records, True


def _rename_folder_if_needed(folder_name: str, target_name: str) -> None:
    if not app_config.FOLDER_PATH or not folder_name or not target_name:
        return
    if folder_name == target_name:
        return

    src_path = os.path.join(app_config.FOLDER_PATH, folder_name)
    dest_path = os.path.join(app_config.FOLDER_PATH, target_name)

    if not os.path.exists(src_path):
        return

    if os.path.exists(dest_path):
        logger.warning(
            "[orders_sync] Пропуск переименования %s -> %s: целевая папка уже существует",
            folder_name,
            target_name,
        )
        return

    register_programmatic_move(src_path, dest_path)
    try:
        os.rename(src_path, dest_path)
    except OSError as exc:
        logger.warning("[orders_sync] Не удалось переименовать %s -> %s: %s", folder_name, target_name, exc)
        discard_programmatic_move(src_path, dest_path)
        return

    move_known_folder(folder_name, target_name)
    update_message_for_folder(folder_name, target_name)
    remove_order(folder_name)
    upsert_order(target_name)
    logger.info("[orders_sync] Папка переименована: %s -> %s", folder_name, target_name)


def reconcile_folder_names(statuses: List[ExternalOrderRecord]) -> None:
    if not app_config.FOLDER_PATH or not os.path.isdir(app_config.FOLDER_PATH):
        return

    status_map = {item.order_number: item for item in statuses}

    try:
        with os.scandir(app_config.FOLDER_PATH) as it:
            for entry in it:
                if not entry.is_dir():
                    continue

                folder_name = entry.name
                folder_number = extract_order_number_from_folder(folder_name) or ""
                record = status_map.get(folder_number)

                if record:
                    target = order_status.plan_folder_target_name(
                        folder_name,
                        approved=record.approved,
                        cancelled=record.cancelled,
                    )
                else:
                    target = order_status.plan_folder_target_name(
                        folder_name,
                        approved=False,
                        cancelled=False,
                    )

                if target:
                    _rename_folder_if_needed(folder_name, target)
    except Exception as exc:
        logger.warning("[orders_sync] Ошибка проверки переименований: %s", exc)


def sync_once() -> None:
    if not app_config.ORDERS_SYNC_ENABLED or not app_config.ORDERS_PG_URL:
        return

    now = datetime.now(timezone.utc)
    records, ok = _fetch_external_orders(now)

    if ok:
        order_status.save_sync_result(records)
        reconcile_folder_names(records)
        logger.info("[orders_sync] Синхронизация: %d записей из Orders", len(records))
    else:
        logger.warning("[orders_sync] Используем локальный кеш статусов из-за недоступности PostgreSQL")

    # даже при отсутствии внешних данных продолжаем обновлять снимок, чтобы мониторинг работал
    refresh_orders_snapshot(force=True)


def start_sync_worker() -> None:
    global _sync_started
    if _sync_started:
        return
    _sync_started = True

    def worker():
        backoff = max(5, app_config.ORDERS_PG_POLL_SECONDS)
        while True:
            try:
                sync_once()
                backoff = max(5, app_config.ORDERS_PG_POLL_SECONDS)
            except Exception as exc:
                logger.warning("[orders_sync] Ошибка синхронизации: %s", exc, exc_info=exc)
                backoff = min(300, backoff * 2)

            heartbeat("orders_sync")
            time.sleep(backoff)

    threading.Thread(target=worker, daemon=True).start()


__all__ = ["start_sync_worker", "sync_once", "reconcile_folder_names"]

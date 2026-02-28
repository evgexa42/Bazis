from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import app.config as app_config
from app import get_manager_from_name, logger
from app.dal.db import SessionLocal
from app.dal.desene_cpu import (
    CPU_STATUS_NEW,
    DeseneCpuOrder,
    log_action,
    normalize_path,
    upsert_order,
)

_MONTHS_RO = {
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

_code_re = re.compile(r"^(\d+-\d+)")
_suffix_re = re.compile(r"\[([A-Za-zА-Яа-я])\]")

_started = False
_lock = threading.Lock()
_stop_event = threading.Event()
_dirty_event = threading.Event()
_observer: Observer | None = None
PDF_RECHECK_SECONDS = 60


def _dt_to_ts(value: datetime | None) -> float | None:
    """Безопасно приводит datetime из БД (aware/naive) к unix-ts."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).timestamp()
    return value.timestamp()


def _month_folder(year: int, month: int) -> str:
    return f"{month:02d}. {_MONTHS_RO[month]} {year}"


def _shift_months(year: int, month: int, delta: int) -> tuple[int, int]:
    for _ in range(delta):
        month -= 1
        if month <= 0:
            month = 12
            year -= 1
    return year, month


def build_target_months(now: datetime | None = None) -> list[tuple[int, str]]:
    now = now or datetime.now()
    mode = app_config.DESENE_CPU_SCAN_MODE
    if mode == "manual":
        result: list[tuple[int, str]] = []
        for item in app_config.DESENE_CPU_MANUAL_MONTHS:
            text = (item or "").strip()
            match = re.search(r"(\d{4})$", text)
            if not text or not match:
                continue
            result.append((int(match.group(1)), text))
        return result

    if mode == "current":
        return [(now.year, _month_folder(now.year, now.month))]

    months_back = max(1, int(app_config.DESENE_CPU_MONTHS_BACK or 2))
    values: list[tuple[int, str]] = []
    for i in range(months_back):
        y, m = _shift_months(now.year, now.month, i)
        values.append((y, _month_folder(y, m)))
    return values


def _extract_order_code(name: str) -> str:
    match = _code_re.match(name or "")
    return match.group(1) if match else ""


def _extract_manager(folder_name: str) -> str:
    suffix_match = _suffix_re.search(folder_name or "")
    if suffix_match:
        marker = suffix_match.group(1).lower()
        for manager in app_config.MANAGER_NAMES:
            if manager and manager[0].lower() == marker:
                return manager
    return get_manager_from_name(folder_name)


def _month_path_candidates(root: str, year: int, month_folder: str) -> list[str]:
    """Строит кандидаты пути месяца с поддержкой root=...\DESENE CPU и root=...\DESENE CPU\<year>."""

    root_clean = (root or "").strip()
    if not root_clean:
        return []

    candidates = [
        os.path.join(root_clean, str(year), month_folder),
        os.path.join(root_clean, month_folder),
    ]

    base_name = os.path.basename(os.path.normpath(root_clean))
    if base_name.isdigit() and len(base_name) == 4:
        # Если root указывает на конкретный год, расширяем до родительского каталога.
        parent = os.path.dirname(os.path.normpath(root_clean))
        candidates.append(os.path.join(parent, str(year), month_folder))

    # Дедупликация с сохранением порядка для предсказуемых логов.
    unique: list[str] = []
    seen = set()
    for item in candidates:
        norm = os.path.normcase(os.path.normpath(item))
        if norm in seen:
            continue
        seen.add(norm)
        unique.append(item)
    return unique


def _resolve_month_path(root: str, year: int, month_folder: str) -> str:
    for candidate in _month_path_candidates(root, year, month_folder):
        if os.path.isdir(candidate):
            return candidate
    return ""


def _walk_pdf_candidates(folder_path: str, max_depth: int = 2):
    stack = [(folder_path, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_file() and entry.name.lower().endswith(".pdf"):
                        yield entry.path
                    elif entry.is_dir() and depth < max_depth:
                        stack.append((entry.path, depth + 1))
        except OSError:
            continue


def _find_matching_pdf(folder_path: str, folder_name: str) -> tuple[bool, str, datetime | None]:
    folder_pdf = f"{folder_name}.pdf".lower()
    order_code = _extract_order_code(folder_name).lower()
    for pdf_path in _walk_pdf_candidates(folder_path, max_depth=2):
        base = os.path.basename(pdf_path).lower()
        if base == "cpu.pdf" or base == folder_pdf or (order_code and base.startswith(order_code)):
            try:
                ts = os.path.getmtime(pdf_path)
                return True, pdf_path, datetime.fromtimestamp(ts, tz=timezone.utc)
            except OSError:
                return True, pdf_path, None
    return False, "", None


def _scan_folder(year: int, month_folder: str, folder_path: str) -> None:
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    folder_mtime = datetime.fromtimestamp(os.path.getmtime(folder_path), tz=timezone.utc)
    folder_mtime_ts = folder_mtime.timestamp()

    existing = None
    norm = normalize_path(folder_path)
    with SessionLocal.begin() as session:
        existing = session.execute(
            select(DeseneCpuOrder).where(DeseneCpuOrder.normalized_path == norm)
        ).scalar_one_or_none()

    should_check_pdf = True
    if existing and existing.folder_last_mtime_seen and existing.last_pdf_check_ts:
        # В SMB/UNC mtime папки может не меняться при добавлении файла вглубь,
        # поэтому для непройденных папок добавляем периодическую перепроверку.
        existing_folder_mtime_ts = _dt_to_ts(existing.folder_last_mtime_seen)
        should_check_pdf = (
            existing_folder_mtime_ts is None or folder_mtime_ts > existing_folder_mtime_ts
        )
        if not should_check_pdf:
            last_check_ts = _dt_to_ts(existing.last_pdf_check_ts)
            last_check_age = (now_ts - last_check_ts) if last_check_ts is not None else PDF_RECHECK_SECONDS
            if not existing.pdf_found and last_check_age >= PDF_RECHECK_SECONDS:
                should_check_pdf = True

    pdf_found = bool(existing.pdf_found) if existing else False
    pdf_path = existing.pdf_path if existing else ""
    pdf_mtime = existing.pdf_mtime if existing else None

    if should_check_pdf:
        pdf_found, pdf_path, pdf_mtime = _find_matching_pdf(folder_path, os.path.basename(folder_path))

    payload = {
        "year": year,
        "month_folder": month_folder,
        "month_key": f"{year}-{month_folder}",
        "order_folder_name": os.path.basename(folder_path),
        "order_code": _extract_order_code(os.path.basename(folder_path)),
        "manager_name": (existing.manager_name if existing and existing.manager_name else _extract_manager(os.path.basename(folder_path))) or "Неизвестно",
        "status": existing.status if existing and existing.status else CPU_STATUS_NEW,
        "folder_path": folder_path,
        "pdf_found": bool(pdf_found),
        "pdf_path": pdf_path,
        "pdf_mtime": pdf_mtime,
        "folder_last_mtime_seen": folder_mtime,
        "last_seen_ts": now,
        "last_pdf_check_ts": now if should_check_pdf else (existing.last_pdf_check_ts if existing else now),
    }
    row = upsert_order(payload)
    if pdf_found and (not existing or not existing.pdf_found):
        log_action(row.id, "PDF_DETECTED", "system")


def scan_once() -> None:
    root = (app_config.DESENE_CPU_ROOT or "").strip()
    if not root:
        logger.warning("[desene_cpu] ROOT не настроен")
        return

    targets = build_target_months()
    seen: set[str] = set()

    for year, month_folder in targets:
        month_path = _resolve_month_path(root, year, month_folder)
        if not month_path:
            logger.warning(
                "[desene_cpu] Папка месяца недоступна: root=%s, year=%s, month=%s",
                root,
                year,
                month_folder,
            )
            continue
        try:
            with os.scandir(month_path) as it:
                for entry in it:
                    if not entry.is_dir():
                        continue
                    seen.add(normalize_path(entry.path))
                    try:
                        _scan_folder(year, month_folder, entry.path)
                    except Exception as exc:
                        # Не прерываем весь проход из-за одной проблемной папки.
                        logger.warning("[desene_cpu] Ошибка обработки папки %s", entry.path, exc_info=exc)
        except OSError as exc:
            logger.warning("[desene_cpu] Ошибка чтения %s", month_path, exc_info=exc)

    target_keys = {f"{year}-{month}" for year, month in targets}
    with SessionLocal.begin() as session:
        rows = session.execute(select(DeseneCpuOrder).where(DeseneCpuOrder.month_key.in_(target_keys))).scalars().all()
        for row in rows:
            if row.normalized_path not in seen and not os.path.exists(row.folder_path or ""):
                session.delete(row)


def _rescan_loop() -> None:
    while not _stop_event.is_set():
        try:
            scan_once()
        except Exception as exc:
            logger.warning("[desene_cpu] Ошибка rescan", exc_info=exc)
        _dirty_event.clear()
        _stop_event.wait(45)


class _CpuEventHandler(FileSystemEventHandler):
    def on_any_event(self, event):
        _dirty_event.set()


def _watchdog_loop() -> None:
    global _observer
    root = (app_config.DESENE_CPU_ROOT or "").strip()
    if not root or not os.path.isdir(root):
        logger.warning("[desene_cpu] Watchdog не запущен: root недоступен: %s", root)
        return

    try:
        obs = Observer()
        obs.schedule(_CpuEventHandler(), root, recursive=True)
        obs.start()
        _observer = obs
        logger.info("[desene_cpu] Watchdog запущен: %s", root)
        while not _stop_event.is_set():
            _dirty_event.wait(10)
            if _dirty_event.is_set():
                try:
                    scan_once()
                except Exception as exc:
                    logger.warning("[desene_cpu] Ошибка watchdog-scan", exc_info=exc)
                _dirty_event.clear()
    except Exception as exc:
        logger.warning("[desene_cpu] Ошибка watchdog", exc_info=exc)
    finally:
        if _observer:
            _observer.stop()
            _observer.join(timeout=5)
            _observer = None


def start_once() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    _stop_event.clear()
    threading.Thread(target=_rescan_loop, daemon=True).start()
    threading.Thread(target=_watchdog_loop, daemon=True).start()

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import app.config as app_config
from app import get_manager_from_name, logger
from app.dal.cpu_orders import mark_cpu_order_missing, upsert_cpu_order
from app.dal.order_manager_override import get_overrides_map

_SCAN_INTERVAL_SECONDS = 45
_worker_started = False
_worker_lock = threading.Lock()


@dataclass
class CpuPdfMatch:
    visible: bool
    pdf_type: str
    pdf_filename: str


def build_cpu_order_key(full_path: str) -> str:
    normalized = os.path.normcase(os.path.abspath(full_path))
    digest = hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()
    return f"cpu:{digest}"


def _is_year_folder(name: str) -> bool:
    return name.isdigit() and len(name) == 4


def _extract_order_number(folder_name: str) -> str:
    # Берем префикс до первого пробела как номер заказа (если он есть).
    return (folder_name.split(" ", 1)[0] if " " in folder_name else "").strip()


def _parse_year_month_from_rel_path(year_month_path: str) -> tuple[int | None, str]:
    parts = [part for part in (year_month_path or "").split(os.sep) if part]
    if len(parts) < 2:
        return None, ""

    year_raw, month_folder = parts[0].strip(), parts[1].strip()
    if not (_is_year_folder(year_raw) and month_folder):
        return None, ""
    return int(year_raw), month_folder


def _find_pdf_match(order_folder_path: str, folder_name: str) -> CpuPdfMatch:
    try:
        files = [entry.name for entry in os.scandir(order_folder_path) if entry.is_file()]
    except OSError:
        return CpuPdfMatch(visible=False, pdf_type="", pdf_filename="")

    files_map = {file_name.lower(): file_name for file_name in files}

    if "cpu.pdf" in files_map:
        name = files_map["cpu.pdf"]
        return CpuPdfMatch(visible=True, pdf_type="CPU.pdf", pdf_filename=name)

    order_pdf = f"{folder_name}.pdf"
    lowered_order_pdf = order_pdf.lower()
    if lowered_order_pdf in files_map:
        name = files_map[lowered_order_pdf]
        return CpuPdfMatch(visible=True, pdf_type="folder.pdf", pdf_filename=name)

    order_number = _extract_order_number(folder_name)
    if order_number:
        num_pdf = f"{order_number}.pdf".lower()
        if num_pdf in files_map:
            name = files_map[num_pdf]
            return CpuPdfMatch(visible=True, pdf_type="order.pdf", pdf_filename=name)

        # Расширенная проверка: любой PDF, начинающийся с номера заказа.
        for lowered_name, original_name in files_map.items():
            if not lowered_name.endswith(".pdf"):
                continue
            if lowered_name.startswith(order_number.lower()):
                return CpuPdfMatch(visible=True, pdf_type="order-prefix.pdf", pdf_filename=original_name)

    # Доп.проверка: совпадение имени файла с полной папкой (без учета регистра).
    for lowered_name, original_name in files_map.items():
        if lowered_name == lowered_order_pdf:
            return CpuPdfMatch(visible=True, pdf_type="folder.pdf", pdf_filename=original_name)

    return CpuPdfMatch(visible=False, pdf_type="", pdf_filename="")


def _detect_month_folder(root_path: str) -> tuple[str | None, str]:
    """Возвращает путь до папки месяца и относительный year/month путь."""

    norm_root = os.path.normpath(root_path)
    parts = [part for part in norm_root.split(os.sep) if part]
    if len(parts) >= 2 and _is_year_folder(parts[-2]):
        # В settings задан конкретный месяц: .../<year>/<month>
        return norm_root, os.path.join(parts[-2], parts[-1])

    current = datetime.now()
    year_folder = str(current.year)
    year_path = os.path.join(norm_root, year_folder)
    if not os.path.isdir(year_path):
        return None, ""

    month_prefix = f"{current.month:02d}."
    candidates = [
        item
        for item in os.listdir(year_path)
        if os.path.isdir(os.path.join(year_path, item)) and item.startswith(month_prefix)
    ]
    if not candidates:
        return None, ""

    month_name = sorted(candidates)[-1]
    return os.path.join(year_path, month_name), os.path.join(year_folder, month_name)


def _iter_order_folders(root_path: str):
    month_path, year_month_path = _detect_month_folder(root_path)
    if not month_path:
        logger.debug("[cpu-monitor] month folder not detected from root='%s'", root_path)
        return

    logger.debug("[cpu-monitor] scanning month_path='%s' (year_month='%s')", month_path, year_month_path)

    for entry in os.scandir(month_path):
        if not entry.is_dir():
            continue
        yield entry.name, entry.path, year_month_path


def sync_cpu_orders() -> dict[str, int]:
    root_path = app_config.DESENE_CPU_ROOT
    if not app_config.CONFIG.get("features", {}).get("cpu_monitoring_enabled", False):
        return {"processed": 0, "visible": 0, "missing": 0}

    if not root_path:
        logger.debug("[cpu-monitor] root path is empty in settings")
        return {"processed": 0, "visible": 0, "missing": 0}

    path_exists = os.path.isdir(root_path)
    logger.debug("[cpu-monitor] settings path='%s', exists=%s", root_path, path_exists)
    if not path_exists:
        return {"processed": 0, "visible": 0, "missing": 0}

    processed = 0
    visible = 0
    seen_keys: set[str] = set()
    order_folder_count = 0
    pdf_visible_count = 0

    batch: list[dict] = []
    for folder_name, full_path, year_month_path in _iter_order_folders(root_path):
        order_folder_count += 1
        order_key = build_cpu_order_key(full_path)
        seen_keys.add(order_key)

        pdf_match = _find_pdf_match(full_path, folder_name)
        if pdf_match.visible:
            pdf_visible_count += 1
        manager_name = get_manager_from_name(folder_name)
        year, month_folder = _parse_year_month_from_rel_path(year_month_path)

        batch.append(
            {
                "order_key": order_key,
                "folder_name": folder_name,
                "full_path": full_path,
                "year_month_path": year_month_path,
                "pdf_visible": pdf_match.visible,
                "pdf_type_found": pdf_match.pdf_type,
                "pdf_filename": pdf_match.pdf_filename,
                "manager_name": manager_name,
                "year": year,
                "month_folder": month_folder,
            }
        )

    logger.debug(
        "[cpu-monitor] folders_found=%s, pdf_visible=%s",
        order_folder_count,
        pdf_visible_count,
    )

    overrides = get_overrides_map([item["order_key"] for item in batch])

    for item in batch:
        record = overrides.get(item["order_key"])
        if record:
            item["manager_name"] = record.manager_name

        upsert_cpu_order(**item)
        processed += 1
        if item["pdf_visible"]:
            visible += 1

    from app.dal.cpu_orders import CpuOrder
    from app.dal.db import SessionLocal

    with SessionLocal.begin() as session:
        existing = {row[0] for row in session.query(CpuOrder.order_key).all()}

    missing = existing - seen_keys
    mark_cpu_order_missing(missing)

    logger.debug(
        "[cpu-monitor] persisted processed=%s, active_visible=%s, archive_candidates=%s, missing=%s",
        processed,
        visible,
        len(batch) - visible,
        len(missing),
    )

    return {"processed": processed, "visible": visible, "missing": len(missing)}


def start_cpu_monitor_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True

    def _worker() -> None:
        while True:
            try:
                result = sync_cpu_orders()
                logger.debug("[cpu-monitor] sync result: %s", result)
            except Exception:
                logger.exception("[cpu-monitor] sync failed")
            time.sleep(_SCAN_INTERVAL_SECONDS)

    thread = threading.Thread(target=_worker, daemon=True, name="cpu-monitor-worker")
    thread.start()


__all__ = [
    "build_cpu_order_key",
    "sync_cpu_orders",
    "start_cpu_monitor_worker",
]

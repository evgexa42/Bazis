from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass

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

    if " " in folder_name:
        order_number = folder_name.split(" ", 1)[0].strip()
        if order_number:
            num_pdf = f"{order_number}.pdf".lower()
            if num_pdf in files_map:
                name = files_map[num_pdf]
                return CpuPdfMatch(visible=True, pdf_type="order.pdf", pdf_filename=name)

    return CpuPdfMatch(visible=False, pdf_type="", pdf_filename="")


def _iter_order_folders(root_path: str):
    for year in sorted(os.listdir(root_path)):
        year_path = os.path.join(root_path, year)
        if not os.path.isdir(year_path) or not _is_year_folder(year):
            continue

        for month_name in sorted(os.listdir(year_path)):
            month_path = os.path.join(year_path, month_name)
            if not os.path.isdir(month_path):
                continue

            year_month_path = os.path.join(year, month_name)
            for order_name in os.listdir(month_path):
                order_path = os.path.join(month_path, order_name)
                if not os.path.isdir(order_path):
                    continue
                yield order_name, order_path, year_month_path


def sync_cpu_orders() -> dict[str, int]:
    root_path = app_config.DESENE_CPU_ROOT
    if not app_config.CONFIG.get("features", {}).get("cpu_monitoring_enabled", False):
        return {"processed": 0, "visible": 0, "missing": 0}

    if not root_path or not os.path.isdir(root_path):
        return {"processed": 0, "visible": 0, "missing": 0}

    processed = 0
    visible = 0
    seen_keys: set[str] = set()

    batch: list[dict] = []
    for folder_name, full_path, year_month_path in _iter_order_folders(root_path):
        order_key = build_cpu_order_key(full_path)
        seen_keys.add(order_key)

        pdf_match = _find_pdf_match(full_path, folder_name)
        manager_name = get_manager_from_name(folder_name)

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
            }
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

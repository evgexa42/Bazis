
import os
import time
from copy import deepcopy
from datetime import datetime

import app as bazis_app
from app import get_manager_from_name, logger
from app.dal.database import replace_order_index, search_orders
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


def build_orders_snapshot():
    if not bazis_app.FOLDER_PATH or not os.path.isdir(bazis_app.FOLDER_PATH):
        return []

    folder_data = []

    with os.scandir(bazis_app.FOLDER_PATH) as it:
        for entry in it:
            if not entry.is_dir():
                continue

            folder_name = entry.name
            if folder_name in telegram_service.IGNORED_FOLDERS:
                continue

            stat = entry.stat()
            modified_date = datetime.fromtimestamp(stat.st_mtime)
            days_ago = (datetime.now() - modified_date).days
            manager = get_manager_from_name(folder_name)

            technologist = technologist_from_folder(folder_name)

            is_confirmed = folder_name.endswith("+")
            if is_confirmed:
                status = "Подтвержден"
            else:
                status = "Готов" if folder_has_ready_marker(folder_name) else "Новый"
            order_number = folder_name.split()[0] if folder_name else ""
            if is_confirmed and order_number.endswith("+"):
                order_number = order_number.rstrip("+")

            folder_data.append(
                {
                    "name": folder_name,
                    "status": status,
                    "manager": manager,
                    "technologist": technologist or "Неизвестно",
                    "modified": modified_date.strftime("%d.%m.%Y %H:%M"),
                    "days": days_ago,
                    "confirmed": is_confirmed,
                    "order_number": order_number,
                }
            )

    folder_data.sort(key=lambda x: x["modified"], reverse=True)
    return folder_data


def refresh_orders_snapshot(force: bool = False):
    now = time.time()
    if not force:
        with bazis_app.orders_snapshot_lock:
            if bazis_app.orders_snapshot and now - bazis_app.last_snapshot_update < bazis_app.SNAPSHOT_TTL:
                return deepcopy(bazis_app.orders_snapshot)

    snapshot = build_orders_snapshot()
    now = time.time()
    with bazis_app.orders_snapshot_lock:
        bazis_app.orders_snapshot = deepcopy(snapshot)
        bazis_app.last_snapshot_update = now

    refresh_search_index()
    return snapshot


def get_orders_snapshot(ttl: float = bazis_app.SNAPSHOT_TTL):
    now = time.time()
    with bazis_app.orders_snapshot_lock:
        if bazis_app.orders_snapshot and now - bazis_app.last_snapshot_update < ttl:
            return deepcopy(bazis_app.orders_snapshot)

    return refresh_orders_snapshot(force=True)


def build_search_index():
    index = []
    search_months = get_recent_months()

    for _, base_folder in bazis_app.SEARCH_FOLDERS.items():
        if not base_folder:
            continue
        for month_folder in search_months:
            month_path = os.path.join(base_folder, month_folder)
            if not os.path.isdir(month_path):
                continue

            for folder_name in os.listdir(month_path):
                manager = get_manager_from_name(folder_name)
                folder_path = os.path.join(month_path, folder_name)
                try:
                    mtime = os.path.getmtime(folder_path)
                except OSError:
                    mtime = 0.0
                index.append(
                    {
                        "name": folder_name,
                        "manager": manager,
                        "folder_path": folder_path,
                        "mtime": mtime,
                    }
                )

    return index


def refresh_search_index():
    index = build_search_index()
    now = time.time()
    replace_order_index(index)
    with bazis_app.order_index_lock:
        bazis_app.order_index_updated_at = now


def search_in_index(query: str):
    return search_orders(query, bazis_app.SEARCH_FOLDERS)


def background_snapshot_updater(interval: float = 2.5):
    while True:
        try:
            refresh_orders_snapshot(force=True)
        except Exception as exc:
            logger.exception("[snapshot] Ошибка фонового обновления", exc_info=exc)
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
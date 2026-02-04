import logging
import os
import re
from copy import deepcopy

from flask import current_app, has_app_context

from app.dal.json_store import load_json_file, save_json_atomic
from app.paths import resolve_path

CONFIG_FILE = resolve_path("config.json")

# Полный набор настроек по умолчанию (все ключи присутствуют всегда)
DEFAULT_CONFIG = {
    "server": {
        "host": "127.0.0.1",
        "port": 5000,
        "debug": False,
        "secret_key": "dev-secret-key",
    },
    "telegram": {"token": "", "chat_id": "", "chat_ids": [], "ignored_folders": ["Архив", "2025"]},
    "paths": {
        "orders": "",
        "facades_dir": "",
        "prisadka_root": "",
        "desene_cpu_root": "",
        "prisadka_client_root": "",
        "facades_list_dir": "",
        "search": {},
    },
    "managers": [],
    "technologists": {},
    "search": {"months": 6},
    "features": {"order_confirmation": False},
    "retention": {"logs_days": 30, "journal_days": 90},
    "orders_times": {"ttl_days": 30},
    "orders_sync": {
        "pg_url": "",
        "poll_seconds": 30,
        "tail_days": 60,
        "enabled": True,
        "approved_plus_rename": True,
        "anulat_rename_tech_marker": False,
    },
}

logger = logging.getLogger("bazis")

CONFIG: dict = {}
CONFIG_WARNINGS: list[str] = []

ORDER_CONFIRMATION_ENABLED = False

FOLDER_PATH = ""
FACADES_DIR = ""
FACADES_FILE = ""
WATCHED_PATH = ""
WATCHED_PATH_NORM = ""
PRISADKA_ROOT = ""
DESENE_CPU_ROOT = ""
PRISADKA_CLIENT_ROOT = ""
FACADES_LIST_DIR = ""
TELEGRAM_TOKEN = ""
CHAT_ID = ""
TELEGRAM_CHAT_IDS: list[str] = []
TELEGRAM_IGNORED_FOLDERS: list[str] = []
SERVER_HOST = DEFAULT_CONFIG["server"]["host"]
SERVER_PORT = DEFAULT_CONFIG["server"]["port"]
DEBUG_MODE = DEFAULT_CONFIG["server"]["debug"]
SEARCH_FOLDERS: dict = {}
SEARCH_MONTHS = DEFAULT_CONFIG["search"]["months"]
MANAGER_NAMES: list[str] = []
TECHNOLOGIST_MARKERS: dict = {}
LOG_RETENTION_DAYS = DEFAULT_CONFIG["retention"]["logs_days"]
JOURNAL_RETENTION_DAYS = DEFAULT_CONFIG["retention"]["journal_days"]
ORDER_TIMES_TTL_DAYS = DEFAULT_CONFIG["orders_times"]["ttl_days"]
ORDERS_PG_URL = ""
ORDERS_PG_POLL_SECONDS = DEFAULT_CONFIG["orders_sync"]["poll_seconds"]
ORDERS_PG_TAIL_DAYS = DEFAULT_CONFIG["orders_sync"]["tail_days"]
ORDERS_SYNC_ENABLED = DEFAULT_CONFIG["orders_sync"]["enabled"]
ORDERS_APPROVED_PLUS_RENAME = DEFAULT_CONFIG["orders_sync"]["approved_plus_rename"]
ORDERS_ANULAT_RENAME_TECH_MARKER = DEFAULT_CONFIG["orders_sync"]["anulat_rename_tech_marker"]


def deep_merge(base, extra):
    result = deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _parse_str_list(value) -> list[str]:
    """Гарантирует список строк без пустых значений."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _parse_str_dict(value) -> dict:
    """Гарантирует словарь строк -> строк без пустых ключей/значений."""
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): str(val).strip()
        for key, val in value.items()
        if str(key).strip() and str(val).strip()
    }


def _parse_mapping_from_lines(value: str) -> dict:
    """Преобразует многострочный текст формата key=value в словарь."""
    mapping: dict[str, str] = {}
    if not isinstance(value, str):
        return mapping

    for line in value.splitlines():
        if "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key = key.strip()
        raw_val = raw_val.strip()
        if key and raw_val:
            mapping[key] = raw_val
    return mapping


def _parse_bool(value, default=False) -> bool:
    """Безопасное приведение к bool (оставляет дефолт, если тип неочевиден)."""
    if isinstance(value, bool):
        return value
    if value in {"True", "true", "1", 1}:
        return True
    if value in {"False", "false", "0", 0}:
        return False
    return bool(default)


def _parse_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_months(value) -> int:
    months_value = _parse_int(value, DEFAULT_CONFIG["search"]["months"])
    return max(1, min(12, months_value))


def _parse_managers(value) -> list[str]:
    return _parse_str_list(value)


def _parse_technologists(value) -> dict:
    return _parse_str_dict(value)


def _parse_telegram_ignored(value) -> list[str]:
    parsed = _parse_str_list(value)
    if parsed or value is None:
        return parsed or DEFAULT_CONFIG["telegram"]["ignored_folders"]
    return []


def _parse_telegram_chat_ids(value, fallback: str | None = None) -> list[str]:
    """Преобразует chat_id/chat_ids в список строковых ID."""
    raw_items: list[str] = []
    if isinstance(value, list):
        raw_items = [str(item) for item in value]
    elif isinstance(value, str):
        prepared = value.replace(",", "\n")
        raw_items = prepared.splitlines()

    if fallback:
        raw_items.append(str(fallback))

    result: list[str] = []
    for item in raw_items:
        cleaned = str(item).strip()
        if not cleaned:
            continue
        if re.fullmatch(r"-?\d+", cleaned):
            result.append(cleaned)
    return result


def _parse_search_folders(value) -> dict:
    if isinstance(value, dict):
        return _parse_str_dict(value)
    if isinstance(value, str):
        return _parse_mapping_from_lines(value)
    if isinstance(value, list):
        # поддержка форматов ["name=path", {"name": "path"}]
        folders: dict[str, str] = {}
        for item in value:
            if isinstance(item, str):
                folders.update(_parse_mapping_from_lines(item))
            elif isinstance(item, dict):
                folders.update(_parse_str_dict(item))
        return folders
    return {}


def _parse_paths_config(value: dict) -> dict:
    if not isinstance(value, dict):
        value = {}

    orders_path = str(value.get("orders") or "").strip()
    facades_dir = str(value.get("facades_dir") or "").strip()
    prisadka_root = str(value.get("prisadka_root") or "").strip()
    desene_cpu_root = str(value.get("desene_cpu_root") or "").strip()
    prisadka_client_root = str(value.get("prisadka_client_root") or "").strip()
    facades_list_dir = str(value.get("facades_list_dir") or "").strip()
    search_folders = _parse_search_folders(value.get("search"))

    return {
        "orders": orders_path,
        "facades_dir": facades_dir,
        "prisadka_root": prisadka_root,
        "desene_cpu_root": desene_cpu_root,
        "prisadka_client_root": prisadka_client_root,
        "facades_list_dir": facades_list_dir,
        "search": search_folders,
    }


def _ensure_complete_config(raw_config: dict) -> dict:
    """Применяет значения по умолчанию и нормализует типы."""
    merged = deep_merge(deepcopy(DEFAULT_CONFIG), raw_config if isinstance(raw_config, dict) else {})

    merged.setdefault("server", {})
    merged["server"]["host"] = str(merged["server"].get("host") or DEFAULT_CONFIG["server"]["host"]).strip()
    merged["server"]["port"] = _parse_int(merged["server"].get("port"), DEFAULT_CONFIG["server"]["port"])
    merged["server"]["debug"] = _parse_bool(merged["server"].get("debug"), DEFAULT_CONFIG["server"]["debug"])
    merged["server"].setdefault("secret_key", DEFAULT_CONFIG["server"].get("secret_key"))

    merged.setdefault("telegram", {})
    merged["telegram"]["token"] = str(merged["telegram"].get("token") or "").strip()
    merged["telegram"]["chat_id"] = str(merged["telegram"].get("chat_id") or "").strip()
    merged["telegram"]["chat_ids"] = _parse_telegram_chat_ids(
        merged["telegram"].get("chat_ids"),
        merged["telegram"]["chat_id"],
    )
    merged["telegram"]["ignored_folders"] = _parse_telegram_ignored(
        merged["telegram"].get("ignored_folders")
    )

    merged.setdefault("paths", {})
    parsed_paths = _parse_paths_config(merged["paths"])
    merged["paths"].update(parsed_paths)

    merged["managers"] = _parse_managers(merged.get("managers"))
    merged["technologists"] = _parse_technologists(merged.get("technologists"))

    merged.setdefault("search", {})
    merged["search"]["months"] = _parse_months(merged["search"].get("months"))

    merged.setdefault("features", {})
    merged["features"]["order_confirmation"] = _parse_bool(
        merged["features"].get("order_confirmation"), DEFAULT_CONFIG["features"]["order_confirmation"]
    )

    merged.setdefault("retention", {})
    merged["retention"]["logs_days"] = max(
        0, _parse_int(merged["retention"].get("logs_days"), DEFAULT_CONFIG["retention"]["logs_days"])
    )
    merged["retention"]["journal_days"] = max(
        0,
        _parse_int(
            merged["retention"].get("journal_days"),
            DEFAULT_CONFIG["retention"].get("journal_days", 90),
        ),
    )

    merged.setdefault("orders_times", {})
    merged["orders_times"]["ttl_days"] = max(
        1, _parse_int(merged["orders_times"].get("ttl_days"), DEFAULT_CONFIG["orders_times"]["ttl_days"])
    )

    merged.setdefault("orders_sync", {})
    merged["orders_sync"]["pg_url"] = str(merged["orders_sync"].get("pg_url") or "").strip()
    merged["orders_sync"]["poll_seconds"] = max(
        5, _parse_int(merged["orders_sync"].get("poll_seconds"), DEFAULT_CONFIG["orders_sync"]["poll_seconds"])
    )
    merged["orders_sync"]["tail_days"] = max(
        1, _parse_int(merged["orders_sync"].get("tail_days"), DEFAULT_CONFIG["orders_sync"]["tail_days"])
    )
    merged["orders_sync"]["enabled"] = _parse_bool(
        merged["orders_sync"].get("enabled"), DEFAULT_CONFIG["orders_sync"]["enabled"]
    )
    merged["orders_sync"]["approved_plus_rename"] = _parse_bool(
        merged["orders_sync"].get("approved_plus_rename"),
        DEFAULT_CONFIG["orders_sync"]["approved_plus_rename"],
    )
    merged["orders_sync"]["anulat_rename_tech_marker"] = _parse_bool(
        merged["orders_sync"].get("anulat_rename_tech_marker"),
        DEFAULT_CONFIG["orders_sync"]["anulat_rename_tech_marker"],
    )

    return merged


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            file_data = load_json_file(CONFIG_FILE)
            if isinstance(file_data, dict):
                return _ensure_complete_config(file_data)
        except Exception as exc:
            logger.exception("[config] Не удалось прочитать config.json", exc_info=exc)
            return deepcopy(DEFAULT_CONFIG)

    try:
        save_json_atomic(CONFIG_FILE, {})
    except Exception as exc:
        logger.exception("[config] Не удалось создать config.json", exc_info=exc)
    return deepcopy(DEFAULT_CONFIG)


def save_config(config):
    save_json_atomic(CONFIG_FILE, config)


def apply_config(config):
    """Применяет конфигурацию и обновляет производные глобалы."""

    global CONFIG
    global FOLDER_PATH, FACADES_DIR, FACADES_FILE, WATCHED_PATH, WATCHED_PATH_NORM
    global PRISADKA_ROOT, DESENE_CPU_ROOT, PRISADKA_CLIENT_ROOT, FACADES_LIST_DIR
    global TELEGRAM_TOKEN, CHAT_ID, TELEGRAM_CHAT_IDS, SERVER_HOST, SERVER_PORT, DEBUG_MODE
    global SEARCH_FOLDERS, MANAGER_NAMES, TECHNOLOGIST_MARKERS, SEARCH_MONTHS
    global ORDER_CONFIRMATION_ENABLED, CONFIG_WARNINGS, LOG_RETENTION_DAYS, JOURNAL_RETENTION_DAYS
    global ORDERS_PG_URL, ORDERS_PG_POLL_SECONDS, ORDERS_PG_TAIL_DAYS, ORDERS_SYNC_ENABLED
    global ORDERS_APPROVED_PLUS_RENAME, ORDERS_ANULAT_RENAME_TECH_MARKER
    global ORDER_TIMES_TTL_DAYS

    normalized = _ensure_complete_config(config)
    CONFIG.clear()
    CONFIG.update(normalized)

    warnings: list[str] = []

    server = normalized["server"]
    telegram_cfg = normalized["telegram"]
    paths = normalized["paths"]
    retention_cfg = normalized.get("retention", {})

    SERVER_HOST = server["host"] or DEFAULT_CONFIG["server"]["host"]
    SERVER_PORT = _parse_int(server["port"], DEFAULT_CONFIG["server"]["port"])
    DEBUG_MODE = _parse_bool(server.get("debug"), DEFAULT_CONFIG["server"]["debug"])

    TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", telegram_cfg.get("token", ""))
    CHAT_ID = str(telegram_cfg.get("chat_id", "")).strip()
    TELEGRAM_CHAT_IDS = _parse_telegram_chat_ids(telegram_cfg.get("chat_ids"), CHAT_ID)
    TELEGRAM_IGNORED_FOLDERS[:] = telegram_cfg.get("ignored_folders", [])

    FOLDER_PATH = paths.get("orders") or ""
    FACADES_DIR = paths.get("facades_dir") or ""
    PRISADKA_ROOT = paths.get("prisadka_root") or ""
    DESENE_CPU_ROOT = paths.get("desene_cpu_root") or ""
    PRISADKA_CLIENT_ROOT = paths.get("prisadka_client_root") or ""
    FACADES_LIST_DIR = paths.get("facades_list_dir") or FACADES_DIR
    FACADES_FILE = (
        os.path.join(FACADES_DIR, "facades_list.txt") if FACADES_DIR else "facades_list.txt"
    )

    if FOLDER_PATH:
        WATCHED_PATH = os.path.abspath(FOLDER_PATH)
        WATCHED_PATH_NORM = os.path.normcase(WATCHED_PATH)
    else:
        WATCHED_PATH = ""
        WATCHED_PATH_NORM = ""

    SEARCH_FOLDERS = deepcopy(paths.get("search") or {})
    SEARCH_MONTHS = normalized["search"]["months"]

    LOG_RETENTION_DAYS = retention_cfg.get("logs_days", DEFAULT_CONFIG["retention"]["logs_days"])
    JOURNAL_RETENTION_DAYS = retention_cfg.get("journal_days", DEFAULT_CONFIG["retention"]["journal_days"])

    orders_times_cfg = normalized.get("orders_times", {})
    ORDER_TIMES_TTL_DAYS = max(
        1,
        _parse_int(
            orders_times_cfg.get("ttl_days"),
            DEFAULT_CONFIG["orders_times"]["ttl_days"],
        ),
    )

    orders_sync_cfg = normalized.get("orders_sync", {})
    ORDERS_PG_URL = os.environ.get("ORDERS_PG_URL", orders_sync_cfg.get("pg_url", ""))
    ORDERS_PG_POLL_SECONDS = orders_sync_cfg.get("poll_seconds", DEFAULT_CONFIG["orders_sync"]["poll_seconds"])
    ORDERS_PG_TAIL_DAYS = orders_sync_cfg.get("tail_days", DEFAULT_CONFIG["orders_sync"]["tail_days"])
    ORDERS_SYNC_ENABLED = _parse_bool(
        os.environ.get("ORDERS_SYNC_ENABLED", orders_sync_cfg.get("enabled")), DEFAULT_CONFIG["orders_sync"]["enabled"]
    )
    ORDERS_APPROVED_PLUS_RENAME = _parse_bool(
        os.environ.get("ORDERS_APPROVED_PLUS_RENAME", orders_sync_cfg.get("approved_plus_rename")),
        DEFAULT_CONFIG["orders_sync"]["approved_plus_rename"],
    )
    ORDERS_ANULAT_RENAME_TECH_MARKER = _parse_bool(
        os.environ.get("ORDERS_ANULAT_RENAME_TECH_MARKER", orders_sync_cfg.get("anulat_rename_tech_marker")),
        DEFAULT_CONFIG["orders_sync"]["anulat_rename_tech_marker"],
    )

    MANAGER_NAMES[:] = normalized.get("managers", [])
    TECHNOLOGIST_MARKERS.clear()
    TECHNOLOGIST_MARKERS.update(normalized.get("technologists", {}))

    ORDER_CONFIRMATION_ENABLED = bool(normalized.get("features", {}).get("order_confirmation"))

    if FOLDER_PATH and not os.path.exists(FOLDER_PATH):
        warning = f"Путь к заказам '{FOLDER_PATH}' не найден"
        warnings.append(warning)
        logger.warning("[config] %s", warning)
    if FACADES_DIR and not os.path.exists(FACADES_DIR):
        warning = f"Папка для facades_list.txt '{FACADES_DIR}' не найдена"
        warnings.append(warning)
        logger.warning("[config] %s", warning)
    for label, path in [
        ("Путь присадки", PRISADKA_ROOT),
        ("DESENE CPU", DESENE_CPU_ROOT),
        ("Присадка клиента", PRISADKA_CLIENT_ROOT),
        ("Папка facades_list", FACADES_LIST_DIR),
    ]:
        if path and not os.path.exists(path):
            warning = f"{label} '{path}' не найден"
            warnings.append(warning)
            logger.warning("[config] %s", warning)
    for name, folder in SEARCH_FOLDERS.items():
        if folder and not os.path.exists(folder):
            warning = f"Путь поиска '{name}' -> '{folder}' недоступен"
            warnings.append(warning)
            logger.warning("[config] %s", warning)

    if has_app_context():
        try:
            current_app.config.from_mapping(CONFIG)
        except Exception as exc:  # pragma: no cover - защитное логирование
            logger.warning("[config] Не удалось синхронизировать Flask config", exc_info=exc)

    CONFIG_WARNINGS.clear()
    CONFIG_WARNINGS.extend(warnings)

    return warnings


CONFIG = load_config()
apply_config(CONFIG)

__all__ = [
    "CONFIG", 
    "CONFIG_WARNINGS",
    "DEFAULT_CONFIG",
    "CONFIG_FILE",
    "MANAGER_NAMES",
    "TECHNOLOGIST_MARKERS",
    "FOLDER_PATH",
    "WATCHED_PATH",
    "WATCHED_PATH_NORM",
    "FACADES_DIR",
    "FACADES_FILE",
    "FACADES_LIST_DIR",
    "CHAT_ID",
    "TELEGRAM_TOKEN",
    "TELEGRAM_IGNORED_FOLDERS",
    "SERVER_PORT",
    "SERVER_HOST",
    "DEBUG_MODE",
    "SEARCH_FOLDERS",
    "SEARCH_MONTHS",
    "PRISADKA_ROOT",
    "DESENE_CPU_ROOT",
    "PRISADKA_CLIENT_ROOT",
    "ORDER_CONFIRMATION_ENABLED",
    "ORDERS_PG_URL",
    "ORDERS_PG_POLL_SECONDS",
    "ORDERS_PG_TAIL_DAYS",
    "ORDERS_SYNC_ENABLED",
    "ORDERS_APPROVED_PLUS_RENAME",
    "ORDERS_ANULAT_RENAME_TECH_MARKER",
    "LOG_RETENTION_DAYS",
    "JOURNAL_RETENTION_DAYS",
    "ORDER_TIMES_TTL_DAYS",
    "apply_config",
    "save_config",
    "load_config",
]

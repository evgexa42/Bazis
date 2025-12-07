import logging
import os
from copy import deepcopy

from app.dal.json_store import load_json_file, save_json_atomic

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# Полный набор настроек по умолчанию (все ключи присутствуют всегда)
DEFAULT_CONFIG = {
    "server": {
        "host": "127.0.0.1",
        "port": 5000,
        "debug": False,
        "secret_key": "dev-secret-key",
    },
    "telegram": {"token": "", "chat_id": ""},
    "paths": {
        "orders": "",
        "facades_dir": "",
        "search": {},
    },
    "managers": [],
    "technologists": {},
    "search": {"months": 6},
    "features": {"order_confirmation": False},
}

logger = logging.getLogger("bazis")

# Текущее состояние конфигурации (обновляется apply_config)
CONFIG: dict = {}
ORDER_CONFIRMATION_ENABLED = False

FOLDER_PATH = ""
FACADES_DIR = ""
FACADES_FILE = ""
WATCHED_PATH = ""
WATCHED_PATH_NORM = ""
TELEGRAM_TOKEN = ""
CHAT_ID = ""
SERVER_HOST = DEFAULT_CONFIG["server"]["host"]
SERVER_PORT = DEFAULT_CONFIG["server"]["port"]
DEBUG_MODE = DEFAULT_CONFIG["server"]["debug"]
SEARCH_FOLDERS: dict = {}
SEARCH_MONTHS = DEFAULT_CONFIG["search"]["months"]
MANAGER_NAMES: list[str] = []
TECHNOLOGIST_MARKERS: dict = {}


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
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_str_dict(value) -> dict:
    """Гарантирует словарь строк -> строк без пустых ключей/значений."""
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): str(val).strip()
        for key, val in value.items()
        if str(key).strip() and str(val).strip()
    }


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

    merged.setdefault("paths", {})
    merged["paths"]["orders"] = str(merged["paths"].get("orders") or "").strip()
    merged["paths"]["facades_dir"] = str(merged["paths"].get("facades_dir") or "").strip()
    merged["paths"]["search"] = _parse_str_dict(merged["paths"].get("search"))

    merged["managers"] = _parse_str_list(merged.get("managers"))
    merged["technologists"] = _parse_str_dict(merged.get("technologists"))

    merged.setdefault("search", {})
    merged["search"]["months"] = _parse_months(merged["search"].get("months"))

    merged.setdefault("features", {})
    merged["features"]["order_confirmation"] = _parse_bool(
        merged["features"].get("order_confirmation"), DEFAULT_CONFIG["features"]["order_confirmation"]
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
    global TELEGRAM_TOKEN, CHAT_ID, SERVER_HOST, SERVER_PORT, DEBUG_MODE
    global SEARCH_FOLDERS, MANAGER_NAMES, TECHNOLOGIST_MARKERS, SEARCH_MONTHS
    global ORDER_CONFIRMATION_ENABLED

    normalized = _ensure_complete_config(config)
    CONFIG.clear()
    CONFIG.update(normalized)

    server = normalized["server"]
    telegram_cfg = normalized["telegram"]
    paths = normalized["paths"]

    SERVER_HOST = server["host"] or DEFAULT_CONFIG["server"]["host"]
    SERVER_PORT = _parse_int(server["port"], DEFAULT_CONFIG["server"]["port"])
    DEBUG_MODE = _parse_bool(server.get("debug"), DEFAULT_CONFIG["server"]["debug"])

    TELEGRAM_TOKEN = telegram_cfg.get("token", "")
    CHAT_ID = str(telegram_cfg.get("chat_id", "")).strip()

    FOLDER_PATH = paths.get("orders") or ""
    FACADES_DIR = paths.get("facades_dir") or ""
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

    MANAGER_NAMES[:] = normalized.get("managers", [])
    TECHNOLOGIST_MARKERS.clear()
    TECHNOLOGIST_MARKERS.update(normalized.get("technologists", {}))

    ORDER_CONFIRMATION_ENABLED = bool(normalized.get("features", {}).get("order_confirmation"))


CONFIG = load_config()
apply_config(CONFIG)

__all__ = [
    "CONFIG",
    "DEFAULT_CONFIG",
    "CONFIG_FILE",
    "MANAGER_NAMES",
    "TECHNOLOGIST_MARKERS",
    "FOLDER_PATH",
    "WATCHED_PATH",
    "WATCHED_PATH_NORM",
    "FACADES_DIR",
    "FACADES_FILE",
    "CHAT_ID",
    "TELEGRAM_TOKEN",
    "SERVER_PORT",
    "SERVER_HOST",
    "DEBUG_MODE",
    "SEARCH_FOLDERS",
    "SEARCH_MONTHS",
    "ORDER_CONFIRMATION_ENABLED",
    "apply_config",
    "save_config",
    "load_config",
]
import logging
import os
from copy import deepcopy

from app.dal.json_store import load_json_file, save_json_atomic

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

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
    "features": {
        "order_confirmation": False,
    },
}

logger = logging.getLogger("bazis")

CONFIG = {}
ORDER_CONFIRMATION_ENABLED = False

FOLDER_PATH = ""
FACADES_DIR = ""
FACADES_FILE = ""
WATCHED_PATH = ""
WATCHED_PATH_NORM = ""
TELEGRAM_TOKEN = ""
CHAT_ID = ""
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000
DEBUG_MODE = False
SEARCH_FOLDERS = {}
SEARCH_MONTHS = DEFAULT_CONFIG["search"]["months"]
MANAGER_NAMES = []
TECHNOLOGIST_MARKERS = {}


def deep_merge(base, extra):
    result = deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            file_data = load_json_file(CONFIG_FILE)
            if isinstance(file_data, dict):
                return deep_merge(deepcopy(DEFAULT_CONFIG), file_data)
        except Exception as exc:
            logger.exception("[config] Не удалось прочитать config.json", exc_info=exc)
            return {}

    try:
        save_json_atomic(CONFIG_FILE, {})
    except Exception as exc:
        logger.exception("[config] Не удалось создать config.json", exc_info=exc)
    return deepcopy(DEFAULT_CONFIG)


def save_config(config):
    save_json_atomic(CONFIG_FILE, config)


def apply_config(config):
    global FOLDER_PATH, FACADES_DIR, FACADES_FILE, WATCHED_PATH, WATCHED_PATH_NORM
    global TELEGRAM_TOKEN, CHAT_ID, SERVER_HOST, SERVER_PORT, DEBUG_MODE
    global SEARCH_FOLDERS, MANAGER_NAMES, TECHNOLOGIST_MARKERS, SEARCH_MONTHS
    global ORDER_CONFIRMATION_ENABLED

    server = config.get("server", {})
    telegram_cfg = config.get("telegram", {})
    paths = config.get("paths", {})

    SERVER_HOST = server.get("host", "127.0.0.1")
    SERVER_PORT = server.get("port", 5000)
    DEBUG_MODE = bool(server.get("debug", False))

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

    search_folders = paths.get("search")
    SEARCH_FOLDERS = search_folders if isinstance(search_folders, dict) else {}

    search_cfg = config.get("search", {}) if isinstance(config.get("search"), dict) else {}
    try:
        months_value = int(search_cfg.get("months", DEFAULT_CONFIG["search"]["months"]))
        SEARCH_MONTHS = max(1, min(12, months_value))
    except (TypeError, ValueError):
        SEARCH_MONTHS = DEFAULT_CONFIG["search"]["months"]

    MANAGER_NAMES[:] = [name.strip() for name in config.get("managers", []) if name.strip()]

    raw_markers = config.get("technologists", {})
    if isinstance(raw_markers, dict):
        TECHNOLOGIST_MARKERS.clear()
        TECHNOLOGIST_MARKERS.update(
            {
                marker.strip(): value.strip()
                for marker, value in raw_markers.items()
                if marker and value
            }
        )
    else:
        TECHNOLOGIST_MARKERS.clear()

    features = config.get("features", {}) if isinstance(config.get("features"), dict) else {}
    ORDER_CONFIRMATION_ENABLED = bool(features.get("order_confirmation"))


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
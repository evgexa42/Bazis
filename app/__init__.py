import json
import logging
import os
import threading
from copy import deepcopy
from logging.handlers import RotatingFileHandler

from flask import Flask, g
from werkzeug.exceptions import HTTPException

from app.dal.database import get_all_clients, replace_clients
from app.dal.db import init_db
from app.dal.json_store import load_json_file, save_json_atomic

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
CLIENTS_FILE = os.path.join(BASE_DIR, "clients.json")
MESSAGES_FILE = os.path.join(BASE_DIR, "messages.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "server": {"host": "127.0.0.1", "port": 5000, "debug": False},
    "telegram": {"token": "", "chat_id": ""},
    "paths": {
        "orders": "",
        "facades_dir": "",
        "search": {},
    },
    "managers": [],
    "technologists": {},
    "features": {
        "order_confirmation": False,
    },
}

LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)

    logger_instance = logging.getLogger("bazis")
    logger_instance.setLevel(logging.INFO)

    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    handler.setFormatter(formatter)

    if not any(getattr(h, "baseFilename", None) == handler.baseFilename for h in logger_instance.handlers):
        logger_instance.addHandler(handler)

    logger_instance.propagate = False

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not any(getattr(h, "baseFilename", None) == handler.baseFilename for h in root_logger.handlers):
        root_logger.addHandler(handler)

    return logger_instance


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


logger = setup_logging()
init_db()
CONFIG = load_config()
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
MANAGER_NAMES = []
TECHNOLOGIST_MARKERS = {}


def apply_config(config):
    global FOLDER_PATH, FACADES_DIR, FACADES_FILE, WATCHED_PATH, WATCHED_PATH_NORM
    global TELEGRAM_TOKEN, CHAT_ID, SERVER_HOST, SERVER_PORT, DEBUG_MODE
    global SEARCH_FOLDERS, MANAGER_NAMES, TECHNOLOGIST_MARKERS
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


apply_config(CONFIG)


def build_clients_lookup(clients_data):
    return {name.lower(): manager for name, manager in clients_data.items()}


def load_clients():
    default_clients = {}
    try:
        db_clients = get_all_clients()
        if db_clients:
            return db_clients
    except Exception as exc:
        logger.exception("[clients] Не удалось загрузить клиентов из БД", exc_info=exc)

    if os.path.exists(CLIENTS_FILE):
        try:
            data = load_json_file(CLIENTS_FILE)
            if isinstance(data, dict):
                replace_clients(data)
                return data
        except Exception as exc:
            logger.exception("[clients] Не удалось прочитать clients.json", exc_info=exc)
        return default_clients

    try:
        replace_clients(default_clients)
    except Exception as exc:
        logger.exception("[clients] Не удалось создать таблицу клиентов", exc_info=exc)
    return default_clients


def save_clients(data):
    try:
        replace_clients(data)
    except Exception as exc:
        logger.exception("[clients] Не удалось сохранить клиентов в БД", exc_info=exc)


clients = load_clients()
clients_lookup = build_clients_lookup(clients)
clients_lock = threading.Lock()

orders_snapshot = []
last_snapshot_update = 0.0
orders_snapshot_lock = threading.Lock()
SNAPSHOT_TTL = 3.0

order_index_updated_at = 0.0
order_index_lock = threading.Lock()


def refresh_clients_lookup_locked():
    global clients_lookup
    clients_lookup = build_clients_lookup(clients)


def reload_clients_from_db():
    global clients
    clients.clear()
    clients.update(load_clients())
    refresh_clients_lookup_locked()


def get_manager_from_name(folder_name):
    lname = folder_name.lower()
    with clients_lock:
        snapshot = dict(clients_lookup)
    for client_lower, manager in snapshot.items():
        if client_lower in lname:
            return manager
    return "Неизвестно"


app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
app.logger.handlers = []
app.logger.setLevel(logging.INFO)
for h in logger.handlers:
    app.logger.addHandler(h)
app.logger.propagate = False


def replace_slashes(text):
    return text.replace("\\", "/")


app.jinja_env.filters["replace_slashes"] = replace_slashes

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


@app.context_processor
def inject_config_data():
    return {
        "config_managers": MANAGER_NAMES,
        "config_facades_dir": FACADES_DIR,
        "order_confirmation_enabled": ORDER_CONFIRMATION_ENABLED,
        "current_user": getattr(g, "current_user", None),
        "current_role": getattr(g, "current_role", None),
    }


from app.services import telegram as telegram_service
from app.services import snapshot as snapshot_service
from app.services import monitor as monitor_service
from app.routes.auth import auth_bp
from app.routes.clients import clients_bp
from app.routes.orders import orders_bp
from app.routes.settings import settings_bp
from app.routes.telegram_api import telegram_api_bp


def register_blueprints(flask_app: Flask):
    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(orders_bp)
    flask_app.register_blueprint(clients_bp)
    flask_app.register_blueprint(settings_bp)
    flask_app.register_blueprint(telegram_api_bp)


register_blueprints(app)


telegram_service.init_bot(TELEGRAM_TOKEN)
telegram_service.load_messages_storage(MESSAGES_FILE)

threading.Thread(target=snapshot_service.background_snapshot_updater, daemon=True).start()
snapshot_service.refresh_orders_snapshot(force=True)
monitor_service.initialize_known_state()
monitor_service.start_observer_once()


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        logger.exception("[exception] HTTP ошибка", exc_info=error)
        return error

    logger.exception("[exception] Неперехваченное исключение", exc_info=error)
    return app.response_class(
        response=json.dumps({"status": "error", "message": "Internal server error"}),
        status=500,
        mimetype="application/json",
    )


__all__ = [
    "app",
    "logger",
    "CONFIG",
    "apply_config",
    "save_config",
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
    "clients",
    "clients_lock",
    "refresh_clients_lookup_locked",
    "reload_clients_from_db",
    "get_manager_from_name",
    "SNAPSHOT_TTL",
    "orders_snapshot",
    "orders_snapshot_lock",
    "last_snapshot_update",
    "order_index_updated_at",
    "order_index_lock",
    "SEARCH_FOLDERS",
    "ORDER_CONFIRMATION_ENABLED",
    "MESSAGES_FILE",
]
import json
import logging
import os
import queue
import threading
import time
import traceback
from collections import defaultdict, deque
from datetime import timedelta
from logging.handlers import RotatingFileHandler
from threading import Lock
from time import perf_counter

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from app.dal.database import get_all_clients, replace_clients
from app.dal.db import init_db
from app.dal.json_store import load_json_file

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - опциональная зависимость
    psutil = None

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
CLIENTS_FILE = os.path.join(BASE_DIR, "clients.json")
MESSAGES_FILE = os.path.join(BASE_DIR, "messages.json")

LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")

metrics_lock = Lock()
metrics = {
    "started_at": time.time(),
    "requests": {
        "total": 0,
        "active": 0,
        "per_endpoint": defaultdict(
            lambda: {
                "count": 0,
                "avg_ms": 0.0,
                "last_ms": 0.0,
                "max_ms": 0.0,
                "last_status": 200,
            }
        ),
        "status_codes": defaultdict(int),
        "last_minute_rps": deque(maxlen=60),
    },
    "errors": {
        "total": 0,
        "last_24h": deque(maxlen=2000),
        "last_items": deque(maxlen=50),
    },
    "snapshot": {
        "version": 0,
        "last_update_ts": 0.0,
        "last_build_ms": 0.0,
        "avg_build_ms": 0.0,
        "build_count": 0,
        "orders_count": 0,
    },
    "sse": {
        "active_clients": 0,
        "last_broadcast_ts": 0.0,
    },
    "threads": {},
    "db": {
        "path": os.path.join(BASE_DIR, "database.db"),
        "size_bytes": 0,
        "last_backup_ts": None,
    },
    "cache": {
        "data_hits": 0,
        "data_misses": 0,
        "search_hits": 0,
        "search_misses": 0,
    },
    "system": {
        "cpu_percent": None,
        "ram_percent": None,
        "process_rss_mb": None,
    },
}

rps_state = {"sec": None, "count": 0}


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


logger = setup_logging()
init_db()


def heartbeat(name: str):
    with metrics_lock:
        metrics["threads"][name] = {"last_heartbeat": time.time(), "alive": True}


def ts_ago(ts):
    return None if not ts else round(time.time() - ts, 1)


def measure_time(name=None, bucket="per_endpoint"):
    def deco(func):
        key = name or func.__name__

        def wrapper(*a, **k):
            t0 = perf_counter()
            try:
                return func(*a, **k)
            finally:
                dt = (perf_counter() - t0) * 1000.0
                with metrics_lock:
                    m = metrics["requests"][bucket][key]
                    m["count"] += 1
                    m["last_ms"] = dt
                    m["avg_ms"] += (dt - m["avg_ms"]) / m["count"]
                    m["max_ms"] = max(m["max_ms"], dt)

        return wrapper

    return deco

from app import config as app_config  # noqa: E402

CONFIG = app_config.CONFIG
ORDER_CONFIRMATION_ENABLED = app_config.ORDER_CONFIRMATION_ENABLED

FOLDER_PATH = app_config.FOLDER_PATH
FACADES_DIR = app_config.FACADES_DIR
FACADES_FILE = app_config.FACADES_FILE
WATCHED_PATH = app_config.WATCHED_PATH
WATCHED_PATH_NORM = app_config.WATCHED_PATH_NORM
TELEGRAM_TOKEN = app_config.TELEGRAM_TOKEN
CHAT_ID = app_config.CHAT_ID
SERVER_HOST = app_config.SERVER_HOST
SERVER_PORT = app_config.SERVER_PORT
DEBUG_MODE = app_config.DEBUG_MODE
SEARCH_FOLDERS = app_config.SEARCH_FOLDERS
MANAGER_NAMES = app_config.MANAGER_NAMES
TECHNOLOGIST_MARKERS = app_config.TECHNOLOGIST_MARKERS
save_config = app_config.save_config
load_config = app_config.load_config


def _sync_config_from_module():
    global FOLDER_PATH, FACADES_DIR, FACADES_FILE, WATCHED_PATH, WATCHED_PATH_NORM
    global TELEGRAM_TOKEN, CHAT_ID, SERVER_HOST, SERVER_PORT, DEBUG_MODE
    global ORDER_CONFIRMATION_ENABLED, SEARCH_FOLDERS

    FOLDER_PATH = app_config.FOLDER_PATH
    FACADES_DIR = app_config.FACADES_DIR
    FACADES_FILE = app_config.FACADES_FILE
    WATCHED_PATH = app_config.WATCHED_PATH
    WATCHED_PATH_NORM = app_config.WATCHED_PATH_NORM
    TELEGRAM_TOKEN = app_config.TELEGRAM_TOKEN
    CHAT_ID = app_config.CHAT_ID
    SERVER_HOST = app_config.SERVER_HOST
    SERVER_PORT = app_config.SERVER_PORT
    DEBUG_MODE = app_config.DEBUG_MODE
    ORDER_CONFIRMATION_ENABLED = app_config.ORDER_CONFIRMATION_ENABLED
    SEARCH_FOLDERS = app_config.SEARCH_FOLDERS


def apply_config(config):
    app_config.apply_config(config)
    _sync_config_from_module()


_sync_config_from_module()


def build_clients_lookup(clients_data):
    return {name.lower(): manager for name, manager in clients_data.items()}


def build_clients_lc(clients_data):
    prepared = [(name.lower(), manager) for name, manager in clients_data.items()]
    prepared.sort(key=lambda x: len(x[0]), reverse=True)
    return prepared


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


def refresh_db_metrics():
    db_path = metrics["db"].get("path") or os.path.join(BASE_DIR, "database.db")
    latest_backup = None
    try:
        size_bytes = os.path.getsize(db_path)
    except OSError:
        size_bytes = 0

    backups_dir = os.path.join(BASE_DIR, "backups")
    if os.path.isdir(backups_dir):
        try:
            files = [
                os.path.join(backups_dir, name)
                for name in os.listdir(backups_dir)
                if os.path.isfile(os.path.join(backups_dir, name))
            ]
            if files:
                latest_backup = max(files, key=os.path.getmtime)
        except OSError:
            latest_backup = None

    with metrics_lock:
        metrics["db"]["path"] = db_path
        metrics["db"]["size_bytes"] = size_bytes
        metrics["db"]["last_backup_ts"] = os.path.getmtime(latest_backup) if latest_backup else None


def cleanup_error_window():
    cutoff = time.time() - 24 * 3600
    with metrics_lock:
        while metrics["errors"]["last_24h"] and metrics["errors"]["last_24h"][0] < cutoff:
            metrics["errors"]["last_24h"].popleft()


def collect_system_metrics():
    if not psutil:
        with metrics_lock:
            metrics["system"]["cpu_percent"] = None
            metrics["system"]["ram_percent"] = None
            metrics["system"]["process_rss_mb"] = None
        return

    try:
        process = psutil.Process(os.getpid())
        with metrics_lock:
            metrics["system"]["cpu_percent"] = psutil.cpu_percent(interval=None)
            metrics["system"]["ram_percent"] = psutil.virtual_memory().percent
            metrics["system"]["process_rss_mb"] = round(process.memory_info().rss / (1024 * 1024), 2)
    except Exception:
        with metrics_lock:
            metrics["system"]["cpu_percent"] = None
            metrics["system"]["ram_percent"] = None
            metrics["system"]["process_rss_mb"] = None


def metrics_background_worker():
    refresh_db_metrics()
    last_db_check = time.time()
    while True:
        try:
            collect_system_metrics()
            cleanup_error_window()
            if time.time() - last_db_check >= 60:
                refresh_db_metrics()
                last_db_check = time.time()
            heartbeat("metrics")
        except Exception as exc:
            logger.exception("[metrics] Ошибка фонового обновления", exc_info=exc)
        time.sleep(5)


clients = load_clients()
clients_lookup = build_clients_lookup(clients)
clients_lock = threading.Lock()
clients_lc = build_clients_lc(clients)

orders_snapshot = []
orders_snapshot_lock = threading.Lock()
last_snapshot_update = 0.0
orders_version = 0
last_snapshot_ts = 0.0
SNAPSHOT_TTL = 3.0
sse_clients = set()
sse_clients_lock = threading.Lock()

order_index_updated_at = 0.0
order_index_lock = threading.Lock()


class SSEClient:
    def __init__(self):
        self.q: "queue.Queue[dict]" = queue.Queue(maxsize=50)
        self.alive = True


def sse_broadcast(event: dict):
    dead = []
    with sse_clients_lock:
        for client in sse_clients:
            try:
                client.q.put_nowait(event)
            except queue.Full:
                try:
                    client.q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    client.q.put_nowait(event)
                except queue.Full:
                    pass
            if not getattr(client, "alive", True):
                dead.append(client)
        for client in dead:
            sse_clients.discard(client)
    with metrics_lock:
        metrics["sse"]["last_broadcast_ts"] = time.time()


def refresh_clients_lookup_locked():
    global clients_lookup
    global clients_lc
    clients_lookup = build_clients_lookup(clients)
    clients_lc = build_clients_lc(clients)


def reload_clients_from_db():
    global clients
    clients.clear()
    clients.update(load_clients())
    refresh_clients_lookup_locked()


def get_manager_from_name(folder_name):
    lname = folder_name.lower()
    snapshot = clients_lc
    for client_lower, manager in snapshot:
        if client_lower in lname:
            return manager
    return "Неизвестно"


def get_secret_key():
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key

    server_config = CONFIG.get("server", {}) if isinstance(CONFIG, dict) else {}
    config_key = server_config.get("secret_key")
    if config_key:
        return config_key

    logger.warning(
        "[security] SECRET_KEY не задан в окружении или config.json, используется значение по умолчанию."
    )
    return "dev-secret-key"


app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)

secret_key = get_secret_key()
app.config.update(
    SECRET_KEY=secret_key,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_REFRESH_EACH_REQUEST=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
)

if secret_key == "dev-secret-key":
    logger.warning(
        "[security] Используется дефолтный SECRET_KEY, задайте переменную окружения или config.json."
    )
app.logger.handlers = []
app.logger.setLevel(logging.INFO)
for h in logger.handlers:
    app.logger.addHandler(h)
app.logger.propagate = False


def replace_slashes(text):
    return text.replace("\\", "/")


app.jinja_env.filters["replace_slashes"] = replace_slashes


@app.before_request
def metrics_before_request():
    g._t0 = perf_counter()
    now_sec = int(time.time())
    with metrics_lock:
        metrics["requests"]["total"] += 1
        metrics["requests"]["active"] += 1

        prev_sec = rps_state.get("sec")
        if prev_sec is None:
            rps_state["sec"] = now_sec
            rps_state["count"] = 1
        elif prev_sec == now_sec:
            rps_state["count"] += 1
        else:
            metrics["requests"]["last_minute_rps"].append(rps_state.get("count", 0))
            gap = min(60, max(0, now_sec - prev_sec - 1))
            for _ in range(gap):
                metrics["requests"]["last_minute_rps"].append(0)
            rps_state["sec"] = now_sec
            rps_state["count"] = 1


@app.after_request
def metrics_after_request(response):
    dt = None
    if hasattr(g, "_t0"):
        dt = (perf_counter() - g._t0) * 1000.0
    endpoint_name = request.endpoint or request.path or "unknown"
    with metrics_lock:
        metrics["requests"]["active"] = max(0, metrics["requests"]["active"] - 1)
        metrics["requests"]["status_codes"][response.status_code] += 1
        if dt is not None:
            m = metrics["requests"]["per_endpoint"][endpoint_name]
            m["count"] += 1
            m["last_ms"] = dt
            m["avg_ms"] += (dt - m["avg_ms"]) / m["count"]
            m["max_ms"] = max(m["max_ms"], dt)
            m["last_status"] = response.status_code
    return response

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


if TELEGRAM_TOKEN:
    telegram_service.init_bot(TELEGRAM_TOKEN)
else:
    logger.warning("[telegram] TELEGRAM_TOKEN не задан, бот не инициализирован.")
telegram_service.load_messages_storage(MESSAGES_FILE)

threading.Thread(target=snapshot_service.background_snapshot_updater, daemon=True).start()
threading.Thread(target=metrics_background_worker, daemon=True).start()
snapshot_service.refresh_orders_snapshot(force=True)
monitor_service.initialize_known_state()
monitor_service.start_observer_once()


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    ts = time.time()
    endpoint = request.path if request else "unknown"
    tb = traceback.format_exc(limit=5)

    with metrics_lock:
        metrics["errors"]["total"] += 1
        metrics["errors"]["last_24h"].append(ts)
        metrics["errors"]["last_items"].append(
            {"ts": ts, "endpoint": endpoint, "err": str(error), "trace": tb}
        )

    if isinstance(error, HTTPException):
        logger.exception("[exception] HTTP ошибка", exc_info=error)
        return error

    logger.exception("[exception] Неперехваченное исключение", exc_info=error)
    return render_template("error.html", error=str(error)), 500


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
    "orders_version",
    "last_snapshot_ts",
    "sse_clients",
    "sse_clients_lock",
    "SSEClient",
    "sse_broadcast",
    "order_index_updated_at",
    "order_index_lock",
    "SEARCH_FOLDERS",
    "ORDER_CONFIRMATION_ENABLED",
    "MESSAGES_FILE",
    "metrics",
    "metrics_lock",
    "measure_time",
    "heartbeat",
    "ts_ago",
]
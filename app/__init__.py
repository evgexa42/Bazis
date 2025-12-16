import os
import queue
import secrets
import threading
import time
from datetime import timedelta

from flask import Flask, g, jsonify, request, session

from app.dal.database import get_all_clients, replace_clients
from app.dal.db import init_db
from app.dal.json_store import load_json_file
from app.logging_config import setup_logging
from app.metrics import (
    heartbeat,
    measure_time,
    metrics,
    metrics_bp,
    metrics_lock,
    start_metrics_worker_once,
    ts_ago,
)
from app.security import csrf_error_response, ensure_csrf_token, validate_csrf_token

from app import config as app_config

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
CLIENTS_FILE = os.path.join(BASE_DIR, "clients.json")
SNAPSHOT_TTL = 12.0

logger = setup_logging(app_config.LOG_RETENTION_DAYS)

clients: dict = {}
clients_lookup: dict = {}
clients_lock = threading.Lock()
clients_lc: list[tuple[str, str]] = []

orders_snapshot = []
orders_snapshot_lock = threading.Lock()
last_snapshot_update = 0.0
orders_version = 0
last_snapshot_ts = 0.0

sse_clients = set()
sse_clients_lock = threading.Lock()

order_index_updated_at = 0.0
order_index_lock = threading.Lock()

snapshot_updater_started = False
services_started = False


def build_clients_lookup(clients_data):
    return {name.lower(): manager for name, manager in clients_data.items()}


def build_clients_lc(clients_data):
    prepared = [(name.lower(), manager) for name, manager in clients_data.items()]
    prepared.sort(key=lambda x: len(x[0]), reverse=True)
    return prepared


def load_clients():
    try:
        return get_all_clients()
    except Exception as exc:
        logger.exception("[clients] Не удалось загрузить клиентов из БД", exc_info=exc)
        return {}


def import_clients_from_json(json_path: str = CLIENTS_FILE) -> dict:
    """Разовая миграция клиентов из JSON в БД."""
    data = load_json_file(json_path)
    if isinstance(data, dict):
        replace_clients(data)
        return data
    return {}


def save_clients(data):
    try:
        replace_clients(data)
    except Exception as exc:
        logger.exception("[clients] Не удалось сохранить клиентов в БД", exc_info=exc)


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


def initialize_clients_state():
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

    server_config = app_config.CONFIG.get("server", {}) if isinstance(app_config.CONFIG, dict) else {}
    config_key = server_config.get("secret_key")
    if config_key and config_key != "dev-secret-key":
        return config_key

    generated_key = secrets.token_hex(32)
    try:
        if isinstance(app_config.CONFIG, dict):
            server_section = app_config.CONFIG.setdefault("server", {})
            if server_section.get("secret_key") in {None, "", "dev-secret-key"}:
                server_section["secret_key"] = generated_key
                app_config.save_config(app_config.CONFIG)
    except Exception as exc:  # pragma: no cover - логирование побочного эффекта
        logger.warning(
            "[security] Не удалось сохранить сгенерированный SECRET_KEY, используется временное значение.",
            exc_info=exc,
        )

    logger.warning(
        "[security] SECRET_KEY не задан или использовался dev-secret-key, сгенерирован временный ключ."
    )
    return generated_key


def replace_slashes(text):
    return text.replace("\\", "/")


def inject_config_data():
    return {
        "config_managers": app_config.MANAGER_NAMES,
        "config_facades_dir": app_config.FACADES_DIR,
        "order_confirmation_enabled": app_config.ORDER_CONFIRMATION_ENABLED,
        "current_user": getattr(g, "current_user", None),
        "current_role": getattr(g, "current_role", None),
        "role_perms": getattr(g, "role_perms", {}),
    }


def register_blueprints(flask_app: Flask):
    from app.routes.setup import setup_bp
    from app.routes.auth import auth_bp
    from app.routes.clients import clients_bp
    from app.routes.orders import orders_bp
    from app.routes.settings import settings_bp

    flask_app.register_blueprint(setup_bp)
    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(orders_bp)
    flask_app.register_blueprint(clients_bp)
    flask_app.register_blueprint(settings_bp)


def _attach_logger(flask_app: Flask) -> None:
    flask_app.logger.handlers = []
    flask_app.logger.setLevel(logger.level)
    for handler in logger.handlers:
        flask_app.logger.addHandler(handler)
    flask_app.logger.propagate = False


def _should_start_background(flask_app: Flask) -> bool:
    if flask_app.config.get("TESTING"):
        return False
    env_flag = os.environ.get("BAZIS_DISABLE_BACKGROUND")
    return env_flag not in {"1", "true", "yes"}


def start_snapshot_updater_once(snapshot_service):
    global snapshot_updater_started
    if snapshot_updater_started:
        return
    snapshot_updater_started = True
    threading.Thread(target=snapshot_service.background_snapshot_updater, daemon=True).start()


def start_background_services():
    global services_started
    if services_started:
        return

    from app.services import monitor as monitor_service
    from app.services import snapshot as snapshot_service
    from app.services import telegram as telegram_service

    telegram_service.init_bot(app_config.TELEGRAM_TOKEN)
    telegram_service.load_messages_storage()
    start_snapshot_updater_once(snapshot_service)
    start_metrics_worker_once()
    snapshot_service.refresh_orders_snapshot(force=True)
    monitor_service.initialize_known_state()
    monitor_service.start_observer_once()

    services_started = True


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


def create_app() -> Flask:
    flask_app = Flask(__name__, template_folder=TEMPLATES_DIR, static_folder=STATIC_DIR)

    secret_key = get_secret_key()
    server_config = app_config.CONFIG.get("server", {}) if isinstance(app_config.CONFIG, dict) else {}
    cookie_secure = bool(
        server_config.get("https")
        or server_config.get("use_https")
        or server_config.get("secure_cookies")
        or os.environ.get("BAZIS_SESSION_SECURE") in {"1", "true", "yes"}
    )
    flask_app.config.update(
        SECRET_KEY=secret_key,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SESSION_REFRESH_EACH_REQUEST=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cookie_secure,
    )
    flask_app.config.from_mapping(app_config.CONFIG)

    _attach_logger(flask_app)
    init_db()
    initialize_clients_state()

    flask_app.jinja_env.filters["replace_slashes"] = replace_slashes
    flask_app.context_processor(inject_config_data)
    flask_app.jinja_env.globals["csrf_token"] = ensure_csrf_token

    flask_app.register_blueprint(metrics_bp)
    register_blueprints(flask_app)

    @flask_app.before_request
    def _prepare_csrf():
        token = ensure_csrf_token()
        g.csrf_token = token
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            valid, message = validate_csrf_token()
            if not valid:
                flask_app.logger.warning(
                    "[security] CSRF blocked request %s: %s", request.path, message
                )
                return csrf_error_response(message)

    if _should_start_background(flask_app):
        start_background_services()

    return flask_app


__all__ = [
    "create_app",
    "logger",
    "app_config",
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
    "metrics",
    "metrics_lock",
    "measure_time",
    "heartbeat",
    "ts_ago",
]
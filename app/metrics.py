
import os
import time
import traceback
import threading
from collections import defaultdict, deque
from threading import Lock
from time import perf_counter

from flask import Blueprint, g, render_template, request
from werkzeug.exceptions import HTTPException

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - опциональная зависимость
    psutil = None

from app.logging_config import setup_logging

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

metrics_lock = Lock()
metrics_worker_started = False

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

logger = setup_logging()
metrics_bp = Blueprint("metrics", __name__)


def _prune_old_errors_locked(cutoff: float) -> None:
    while metrics["errors"]["last_24h"] and metrics["errors"]["last_24h"][0] < cutoff:
        metrics["errors"]["last_24h"].popleft()


def heartbeat(name: str) -> None:
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
        _prune_old_errors_locked(cutoff)


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


def start_metrics_worker_once():
    global metrics_worker_started
    if metrics_worker_started:
        return
    metrics_worker_started = True
    threading.Thread(target=metrics_background_worker, daemon=True).start()


@metrics_bp.before_app_request
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


@metrics_bp.after_app_request
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


@metrics_bp.app_errorhandler(Exception)
def handle_unexpected_error(error):
    ts = time.time()
    endpoint = request.path if request else "unknown"
    tb = traceback.format_exc(limit=5)

    with metrics_lock:
        cutoff = ts - 24 * 3600
        _prune_old_errors_locked(cutoff)
        metrics["errors"]["total"] += 1
        metrics["errors"]["last_24h"].append(ts)
        metrics["errors"]["last_items"].append(
            {"ts": ts, "endpoint": endpoint, "err": str(error), "trace": tb}
        )

    if isinstance(error, HTTPException):
        logger.exception("[exception] HTTP ошибка", exc_info=error)
        return error

    logger.exception("[exception] Неперехваченное исключение", exc_info=error)
    generic_message = "Произошла внутренняя ошибка. Попробуйте позже или обратитесь к администратору."
    return render_template("error.html", message=generic_message, code=500), 500


__all__ = [
    "metrics",
    "metrics_bp",
    "metrics_lock",
    "measure_time",
    "heartbeat",
    "ts_ago",
    "start_metrics_worker_once",
    "metrics_background_worker",
]
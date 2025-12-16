import logging
import os
import time
from logging.handlers import RotatingFileHandler

DEFAULT_LOG_RETENTION_DAYS = 30

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")


def _resolve_retention_days(retention_days: int | None = None) -> int:
    if retention_days is not None:
        return max(0, int(retention_days))

    try:
        from app import config as app_config  # локальный импорт для избежания циклов

        return max(0, int(getattr(app_config, "LOG_RETENTION_DAYS", DEFAULT_LOG_RETENTION_DAYS)))
    except Exception:
        return DEFAULT_LOG_RETENTION_DAYS


def cleanup_rotated_logs(retention_days: int, logger_instance: logging.Logger | None = None) -> None:
    """Удаляет старые ротации логов, не трогая активный файл."""

    safe_days = max(0, retention_days)
    if safe_days <= 0:
        return

    cutoff_ts = time.time() - safe_days * 86400
    active_log = os.path.abspath(LOG_FILE)

    try:
        for name in os.listdir(LOG_DIR):
            path = os.path.abspath(os.path.join(LOG_DIR, name))
            if path == active_log:
                continue
            try:
                if not os.path.isfile(path):
                    continue
                if os.path.getmtime(path) < cutoff_ts:
                    os.remove(path)
                    if logger_instance:
                        logger_instance.info("[logging] Удалена старая ротация: %s", name)
            except OSError:
                if logger_instance:
                    logger_instance.warning(
                        "[logging] Не удалось обработать файл ротации: %s", path, exc_info=True
                    )
    except OSError:
        if logger_instance:
            logger_instance.warning("[logging] Не удалось просканировать каталог логов", exc_info=True)


def setup_logging(retention_days: int | None = None) -> logging.Logger:
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

    resolved_retention = _resolve_retention_days(retention_days)
    cleanup_rotated_logs(resolved_retention, logger_instance)

    return logger_instance


__all__ = ["setup_logging", "cleanup_rotated_logs", "LOG_DIR", "LOG_FILE"]
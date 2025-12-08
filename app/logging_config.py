import logging
import os
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")


def setup_logging() -> logging.Logger:
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


__all__ = ["setup_logging", "LOG_DIR", "LOG_FILE"]
from datetime import datetime
from logging.handlers import RotatingFileHandler
import logging
from pathlib import Path
import shutil
import sys

from app.paths import get_base_dir

BASE_DIR = Path(get_base_dir())
DB_PATH = BASE_DIR / "database.db"
BACKUP_DIR = BASE_DIR / "backups"
LOG_DIR = BASE_DIR / "logs"
BACKUP_RETENTION = 14


def setup_logger() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / "backup.log"

    logger = logging.getLogger("backup")
    logger.setLevel(logging.INFO)

    handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(handler)

    logger.propagate = False
    return logger


def create_backup(logger: logging.Logger) -> Path:
    if not DB_PATH.exists():
        logger.error("Database file not found at %s", DB_PATH)
        raise FileNotFoundError(f"Database file not found at {DB_PATH}")

    BACKUP_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    backup_path = BACKUP_DIR / f"database_{timestamp}.db"

    shutil.copy2(DB_PATH, backup_path)
    logger.info("Backup created: %s", backup_path)
    return backup_path


def cleanup_old_backups(logger: logging.Logger) -> None:
    backups = sorted(BACKUP_DIR.glob("database_*.db"))
    if len(backups) <= BACKUP_RETENTION:
        return

    old_backups = backups[:-BACKUP_RETENTION]
    for backup_file in old_backups:
        try:
            backup_file.unlink()
            logger.info("Removed old backup: %s", backup_file)
        except OSError as exc:
            logger.error("Failed to remove %s: %s", backup_file, exc)


def main() -> int:
    logger = setup_logger()

    try:
        create_backup(logger)
        cleanup_old_backups(logger)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Backup failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
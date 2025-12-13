import logging

import app.config as app_config
from app.dal.users import count_active_admins, count_users

logger = logging.getLogger("bazis")


def is_first_run() -> bool:
    """Определяет, требуется ли первичная настройка."""
    try:
        total_users = count_users()
        admin_users = count_active_admins()
    except Exception as exc:  # pragma: no cover - защитное логирование
        logger.warning("[setup] Не удалось прочитать состояние пользователей", exc_info=exc)
        return True

    paths_cfg = app_config.CONFIG.get("paths", {}) if isinstance(app_config.CONFIG, dict) else {}
    config_incomplete = not (paths_cfg.get("orders") or app_config.SEARCH_FOLDERS)

    if total_users == 0 or admin_users == 0:
        return True

    if total_users == 1 and admin_users == 1 and config_incomplete:
        return True

    return False


__all__ = ["is_first_run"]

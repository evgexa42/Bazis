from copy import deepcopy
import logging
import re

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.config import CONFIG, DEFAULT_CONFIG, apply_config, save_config
from app.dal.users import upsert_user
from app.services.setup_state import is_first_run

logger = logging.getLogger("bazis")

setup_bp = Blueprint("setup", __name__)


@setup_bp.before_app_request
def enforce_setup_wizard():
    endpoint = request.endpoint or ""

    if is_first_run():
        if endpoint.startswith("setup.") or endpoint.startswith("static"):
            return
        return redirect(url_for("setup.setup_page"))

    if endpoint.startswith("setup.") and endpoint != "static":
        return redirect(url_for("auth.login"))


def _collect_accounts(prefix: str) -> list[dict]:
    names = request.form.getlist(f"{prefix}_name[]")
    logins = request.form.getlist(f"{prefix}_username[]")
    passwords = request.form.getlist(f"{prefix}_password[]")
    markers = request.form.getlist(f"{prefix}_marker[]") if prefix == "technologist" else []

    collected: list[dict] = []
    for idx, (name, login, password) in enumerate(zip(names, logins, passwords)):
        normalized_name = (name or "").strip()
        normalized_login = (login or "").strip()
        normalized_password = (password or "").strip()
        marker = (markers[idx] if idx < len(markers) else "").strip() if markers else ""

        if not any([normalized_name, normalized_login, normalized_password, marker]):
            continue

        collected.append(
            {
                "name": normalized_name,
                "username": normalized_login,
                "password": normalized_password,
                "marker": marker,
            }
        )

    return collected


@setup_bp.route("/setup", methods=["GET", "POST"])
def setup_page():
    if not is_first_run():
        return redirect(url_for("auth.login"))

    errors: list[str] = []
    status_message = None
    updated_config: dict | None = None

    config_snapshot = deepcopy(CONFIG if isinstance(CONFIG, dict) else DEFAULT_CONFIG)
    managers_form = _collect_accounts("manager") if request.method == "POST" else []
    technologists_form = _collect_accounts("technologist") if request.method == "POST" else []

    if request.method == "POST":
        def parse_chat_ids(value: str) -> tuple[list[str], list[str]]:
            cleaned: list[str] = []
            invalid: list[str] = []
            seen = set()
            for line in value.splitlines():
                for token in line.replace(",", " ").split():
                    token = token.strip()
                    if not token:
                        continue
                    if re.fullmatch(r"-?\d+", token):
                        if token in seen:
                            continue
                        cleaned.append(token)
                        seen.add(token)
                    else:
                        invalid.append(token)
            return cleaned, invalid

        facades_dir = request.form.get("facades_dir", "").strip()

        telegram_token = request.form.get("telegram_token", "").strip()
        telegram_chat_raw = request.form.get("telegram_chat_id", "").strip()
        telegram_chat_ids, telegram_chat_invalid = parse_chat_ids(telegram_chat_raw)
        if telegram_chat_invalid:
            errors.append(
                "Telegram Chat ID должен содержать только числа (можно с минусом). "
                f"Неверные значения: {', '.join(telegram_chat_invalid)}."
            )
        telegram_chat = "\n".join(telegram_chat_ids)

        admin_username = (request.form.get("admin_username") or "").strip()
        admin_password = (request.form.get("admin_password") or "").strip()
        admin_display = (request.form.get("admin_display") or "").strip()

        paths_snapshot = config_snapshot.setdefault("paths", {})
        if facades_dir:
            paths_snapshot["facades_dir"] = facades_dir
            paths_snapshot["facades_list_dir"] = facades_dir

        telegram_snapshot = config_snapshot.setdefault("telegram", {})
        telegram_snapshot["token"] = telegram_token
        telegram_snapshot["chat_id"] = telegram_chat

        if not admin_username or not admin_password:
            errors.append("Укажите логин и пароль администратора.")

        managers = managers_form or _collect_accounts("manager")
        technologists = technologists_form or _collect_accounts("technologist")

        missing_credentials = [item for item in managers + technologists if not item.get("username") or not item.get("password")]
        if missing_credentials:
            errors.append("Каждый менеджер и технолог должен содержать логин и пароль.")

        manager_names = [item.get("name") or item.get("username") for item in managers if item.get("username")]
        tech_markers = {
            (item.get("marker") or item.get("username") or item.get("name")): (item.get("name") or item.get("username"))
            for item in technologists
            if item.get("username")
        }

        if not manager_names:
            fallback_manager = admin_display or admin_username
            if fallback_manager:
                manager_names.append(fallback_manager)
            else:
                errors.append("Добавьте хотя бы одного менеджера или имя администратора.")

        if not errors:
            updated_config = deepcopy(config_snapshot)
            paths_cfg = updated_config.setdefault("paths", {})
            if facades_dir:
                paths_cfg["facades_dir"] = facades_dir
                paths_cfg["facades_list_dir"] = facades_dir

            telegram_cfg = updated_config.setdefault("telegram", {})
            telegram_cfg["token"] = telegram_token
            telegram_cfg["chat_id"] = telegram_chat

            updated_config["managers"] = manager_names
            updated_config["technologists"] = {k: v for k, v in tech_markers.items() if k}

            usernames = [name for name in [admin_username] if name]
            usernames.extend(item.get("username") for item in managers if item.get("username"))
            usernames.extend(item.get("username") for item in technologists if item.get("username"))
            duplicates = {name for name in usernames if usernames.count(name) > 1}
            if duplicates:
                errors.append(
                    "Логины должны быть уникальными. Дубликаты: " + ", ".join(sorted(duplicates)) + "."
                )

        if not errors and updated_config is not None:
            creation_errors: list[str] = []

            admin_result = upsert_user(admin_username, admin_password, "admin")
            if not admin_result.get("ok"):
                creation_errors.append(admin_result.get("message", "Не удалось создать администратора."))

            for item in managers:
                result = upsert_user(item.get("username"), item.get("password"), "manager")
                if not result.get("ok"):
                    creation_errors.append(result.get("message", "Не удалось создать менеджера."))

            for item in technologists:
                result = upsert_user(item.get("username"), item.get("password"), "technologist")
                if not result.get("ok"):
                    creation_errors.append(result.get("message", "Не удалось создать технолога."))

            if creation_errors:
                errors.extend(creation_errors)
                logger.warning("[setup] Ошибки создания пользователей: %s", creation_errors)
            else:
                save_config(updated_config)
                CONFIG.clear()
                CONFIG.update(updated_config)
                apply_config(CONFIG)
                flash(
                    "Первичная настройка завершена. Авторизуйтесь с учётной записью администратора.",
                    "success",
                )
                return redirect(url_for("auth.login"))

    return render_template(
        "setup.html",
        errors=errors,
        status_message=status_message,
        config=config_snapshot,
        managers_prefill=managers_form,
        technologists_prefill=technologists_form,
    )


__all__ = ["setup_bp"]

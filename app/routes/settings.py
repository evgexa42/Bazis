
from copy import deepcopy

from flask import Blueprint, render_template, request

from app import CONFIG, MANAGER_NAMES, SEARCH_FOLDERS, TECHNOLOGIST_MARKERS, apply_config, save_config
from app.services import telegram as telegram_service

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings", methods=["GET", "POST"])
def settings_page():
    global CONFIG

    status_message = None
    errors = []

    if request.method == "POST":
        def parse_list(value):
            return [item.strip() for item in value.splitlines() if item.strip()]

        def parse_mapping(value):
            mapping = {}
            for line in value.splitlines():
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if key and val:
                    mapping[key] = val
            return mapping

        updated = deepcopy(CONFIG)

        orders_path = request.form.get("orders_path", "").strip()
        facades_dir = request.form.get("facades_dir", "").strip()
        search_raw = request.form.get("search_folders", "")
        managers_raw = request.form.get("managers", "")
        technologists_raw = request.form.get("technologists", "")

        server_host = request.form.get("server_host", "").strip()
        server_port = request.form.get("server_port", "").strip()
        debug_mode = request.form.get("debug_mode") == "on"
        order_confirmation = request.form.get("order_confirmation") == "on"

        telegram_token = request.form.get("telegram_token", "").strip()
        telegram_chat = request.form.get("telegram_chat_id", "").strip()

        if orders_path:
            updated.setdefault("paths", {})["orders"] = orders_path
        if facades_dir:
            updated.setdefault("paths", {})["facades_dir"] = facades_dir
        updated.setdefault("paths", {})["search"] = parse_mapping(search_raw)

        managers_list = parse_list(managers_raw)
        if managers_list:
            updated["managers"] = managers_list
        else:
            errors.append("Список менеджеров не может быть пустым.")

        technologists_map = parse_mapping(technologists_raw)
        updated["technologists"] = technologists_map

        server_config = updated.setdefault("server", {})
        if server_host:
            server_config["host"] = server_host
        if server_port:
            try:
                server_config["port"] = int(server_port)
            except (TypeError, ValueError):
                errors.append("Порт должен быть числом.")
        server_config["debug"] = debug_mode

        updated.setdefault("telegram", {})["token"] = telegram_token
        updated.setdefault("telegram", {})["chat_id"] = telegram_chat

        features_config = updated.setdefault("features", {})
        if not isinstance(features_config, dict):
            features_config = {}
            updated["features"] = features_config
        features_config["order_confirmation"] = order_confirmation

        if not errors:
            save_config(updated)
            CONFIG.clear()
            CONFIG.update(updated)
            apply_config(CONFIG)
            telegram_service.init_bot(telegram_token)
            status_message = (
                "Настройки сохранены. Некоторые изменения вступят в силу после перезапуска приложения."
            )

    managers_text = "\n".join(MANAGER_NAMES)
    technologists_text = "\n".join(
        f"{marker}={name}" for marker, name in TECHNOLOGIST_MARKERS.items()
    )
    search_text = "\n".join(f"{title}={path}" for title, path in SEARCH_FOLDERS.items())

    return render_template(
        "settings.html",
        config=CONFIG,
        managers_text=managers_text,
        technologists_text=technologists_text,
        search_text=search_text,
        status_message=status_message,
        errors=errors,
    )
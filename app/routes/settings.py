
from copy import deepcopy

from flask import Blueprint, abort, jsonify, redirect, render_template, request, session, url_for

import app.config as app_config
from app.config import CONFIG, MANAGER_NAMES, SEARCH_FOLDERS, TECHNOLOGIST_MARKERS, apply_config, save_config
from app.dal.permissions import (
    PERMISSION_FIELDS,
    get_all_role_permissions,
    has_permission,
    save_role_permissions,
)
from app.dal.users import (
    ALLOWED_ROLES,
    create_user,
    get_all_users,
    reset_user_password,
    update_user_role,
)
from app import metrics, metrics_lock, ts_ago
from app.routes.auth import login_required
from app.services import telegram as telegram_service

settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    global CONFIG

    current_role = session.get("role")
    if not has_permission(current_role, "can_access_settings"):
        return redirect(url_for("orders.index"))

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
        telegram_token = updated.get("telegram", {}).get("token", "")

        managers_raw = request.form.get("managers", "")
        technologists_raw = request.form.get("technologists", "")

        if has_permission(current_role, "can_edit_paths"):
            orders_path = request.form.get("orders_path", "").strip()
            facades_dir = request.form.get("facades_dir", "").strip()
            search_raw = request.form.get("search_folders", "")

            server_host = request.form.get("server_host", "").strip()
            server_port = request.form.get("server_port", "").strip()
            debug_mode = request.form.get("debug_mode") == "on"

            telegram_token = request.form.get("telegram_token", "").strip()
            telegram_chat = request.form.get("telegram_chat_id", "").strip()

            if orders_path:
                updated.setdefault("paths", {})["orders"] = orders_path
            if facades_dir:
                updated.setdefault("paths", {})["facades_dir"] = facades_dir
            updated.setdefault("paths", {})["search"] = parse_mapping(search_raw)

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

        managers_list = parse_list(managers_raw)
        if managers_list:
            updated["managers"] = managers_list
        else:
            errors.append("Список менеджеров не может быть пустым.")

        technologists_map = parse_mapping(technologists_raw)
        updated["technologists"] = technologists_map

        if has_permission(current_role, "can_toggle_order_options"):
            features_config = updated.setdefault("features", {})
            if not isinstance(features_config, dict):
                features_config = {}
                updated["features"] = features_config
            features_config["order_confirmation"] = (
                request.form.get("order_confirmation") == "on"
            )

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

    users = get_all_users()

    return render_template(
        "settings.html",
        config=CONFIG,
        managers_text=managers_text,
        technologists_text=technologists_text,
        search_text=search_text,
        status_message=status_message,
        errors=errors,
        users=users,
        allowed_roles=sorted(ALLOWED_ROLES),
        current_user=session.get("user"),
        role_permissions=get_all_role_permissions(),
        permission_fields=list(PERMISSION_FIELDS),
    )


@settings_bp.route("/settings/roles/save", methods=["POST"])
@login_required
def update_role_permissions():
    current_role = session.get("role")
    if not (
        has_permission(current_role, "can_access_settings")
        and has_permission(current_role, "can_manage_users")
    ):
        abort(403)

    updates = {}
    for role in ALLOWED_ROLES:
        perms = {}
        for field in PERMISSION_FIELDS:
            key = f"roles[{role}][{field}]"
            perms[field] = 1 if request.form.get(key) else 0
        updates[role] = perms

    updates.setdefault("admin", {})
    updates["admin"]["can_access_settings"] = 1
    updates["admin"]["can_manage_users"] = 1

    save_role_permissions(updates)

    return redirect(url_for("settings.settings_page"))


@settings_bp.route("/api/metrics", methods=["GET"])
@login_required
def api_metrics():
    current_role = session.get("role")
    if not has_permission(current_role, "can_access_metrics"):
        abort(403)

    with metrics_lock:
        data = deepcopy(metrics)

    data["requests"]["last_minute_rps"] = list(data.get("requests", {}).get("last_minute_rps", []))
    data["requests"]["per_endpoint"] = dict(data.get("requests", {}).get("per_endpoint", {}))
    data["errors"]["last_24h"] = list(data.get("errors", {}).get("last_24h", []))
    data["errors"]["last_items"] = list(data.get("errors", {}).get("last_items", []))
    data.setdefault("snapshot", {})["seconds_ago"] = ts_ago(data.get("snapshot", {}).get("last_update_ts"))
    data.setdefault("sse", {})["seconds_ago"] = ts_ago(data.get("sse", {}).get("last_broadcast_ts"))
    data.setdefault("db", {})["last_backup_hours_ago"] = (
        None
        if not data.get("db", {}).get("last_backup_ts")
        else round(ts_ago(data["db"]["last_backup_ts"]) / 3600, 2)
    )
    data["errors"]["errors_last_24h"] = len(data.get("errors", {}).get("last_24h", []))

    return jsonify(data)


@settings_bp.route("/admin/users/add", methods=["POST"])
@login_required
def add_user():
    if not has_permission(session.get("role"), "can_manage_users"):
        abort(403)

    username = request.form.get("username", "")
    password = request.form.get("password", "")
    role = request.form.get("role", "")

    result = create_user(username, password, role)
    status_code = 200 if result.get("ok") else 400
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(result), status_code
    return redirect(url_for("settings.settings_page"))


@settings_bp.route("/admin/users/change_role", methods=["POST"])
@login_required
def change_role():
    if not has_permission(session.get("role"), "can_manage_users"):
        abort(403)

    try:
        user_id = int(request.form.get("user_id", "0"))
    except ValueError:
        return jsonify({"ok": False, "error": "Неверный идентификатор пользователя."}), 400
    role = request.form.get("role", "")

    result = update_user_role(user_id, role)
    status_code = 200 if result.get("ok") else 400
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(result), status_code
    return redirect(url_for("settings.settings_page"))


@settings_bp.route("/admin/users/reset_password", methods=["POST"])
@login_required
def reset_password():
    if not has_permission(session.get("role"), "can_manage_users"):
        abort(403)

    try:
        user_id = int(request.form.get("user_id", "0"))
    except ValueError:
        return jsonify({"ok": False, "error": "Неверный идентификатор пользователя."}), 400

    new_password = request.form.get("new_password", "")
    result = reset_user_password(user_id, new_password, session.get("user"))
    status_code = 200 if result.get("ok") else 400
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(result), status_code
    return redirect(url_for("settings.settings_page"))
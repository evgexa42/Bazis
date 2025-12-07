
import logging
from copy import deepcopy

from flask import (
    Blueprint,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import app.config as app_config
from app.config import (
    CONFIG,
    CONFIG_WARNINGS,
    MANAGER_NAMES,
    SEARCH_FOLDERS,
    TECHNOLOGIST_MARKERS,
    apply_config,
    save_config,
)
from app.dal.permissions import (
    PERMISSION_FIELDS,
    get_all_role_permissions,
    has_permission,
    permissions_required,
    save_role_permissions,
)
from app.dal.users import (
    ALLOWED_ROLES,
    create_user,
    delete_user,
    get_all_users,
    reset_user_password,
    update_user,
    update_user_role,
)
from app import metrics, metrics_lock, ts_ago
from app.routes.auth import login_required
from app.services import telegram as telegram_service

settings_bp = Blueprint("settings", __name__)
logger = logging.getLogger("bazis")


@settings_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    global CONFIG

    current_role = session.get("role")
    if not has_permission(current_role, "can_access_settings"):
        return redirect(url_for("orders.index"))

    status_message = None
    errors = []
    config_warnings = list(CONFIG_WARNINGS)
    needs_restart = False
    monitor_restart_required = False

    if request.method == "POST":
        def parse_list(value: str) -> list[str]:
            return [item.strip() for item in value.splitlines() if item.strip()]

        def parse_mapping(value: str) -> dict:
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
        old_config = deepcopy(CONFIG)
        telegram_token = updated.get("telegram", {}).get("token", "")

        managers_raw = request.form.get("managers", "")
        technologists_raw = request.form.get("technologists", "")

        managers_list = parse_list(managers_raw)
        if managers_list:
            updated["managers"] = managers_list
        else:
            errors.append("Список менеджеров не может быть пустым.")

        updated["technologists"] = parse_mapping(technologists_raw)

        if has_permission(current_role, "can_edit_paths"):
            orders_path = request.form.get("orders_path", "").strip()
            facades_dir = request.form.get("facades_dir", "").strip()
            search_raw = request.form.get("search_folders", "")

            server_host = request.form.get("server_host", "").strip()
            server_port_raw = request.form.get("server_port", "").strip()
            debug_mode = request.form.get("debug_mode") == "on"

            telegram_token = request.form.get("telegram_token", "").strip()
            telegram_chat = request.form.get("telegram_chat_id", "").strip()

            paths_cfg = updated.setdefault("paths", {})
            if orders_path:
                paths_cfg["orders"] = orders_path
            if facades_dir:
                paths_cfg["facades_dir"] = facades_dir
            paths_cfg["search"] = parse_mapping(search_raw)

            server_config = updated.setdefault("server", {})
            if server_host:
                server_config["host"] = server_host
            if server_port_raw:
                try:
                    server_config["port"] = int(server_port_raw)
                except (TypeError, ValueError):
                    errors.append("Порт должен быть числом.")
            server_config["debug"] = debug_mode

            telegram_cfg = updated.setdefault("telegram", {})
            telegram_cfg["token"] = telegram_token
            telegram_cfg["chat_id"] = telegram_chat

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
            apply_results = apply_config(CONFIG)
            config_warnings = apply_results or []
            telegram_service.init_bot(app_config.TELEGRAM_TOKEN)
            logger.info("[settings] Настройки обновлены пользователем %s", session.get("user"))

            paths_changed = old_config.get("paths", {}) != updated.get("paths", {})
            server_changed = {
                "host": old_config.get("server", {}).get("host"),
                "port": old_config.get("server", {}).get("port"),
                "debug": old_config.get("server", {}).get("debug"),
            } != {
                "host": updated.get("server", {}).get("host"),
                "port": updated.get("server", {}).get("port"),
                "debug": updated.get("server", {}).get("debug"),
            }

            monitor_restart_required = paths_changed
            needs_restart = server_changed
            status_parts = ["Настройки сохранены."]
            if monitor_restart_required:
                status_parts.append(
                    "Пути изменены — перезапустите файловый мониторинг, чтобы применить обновления."
                )
            if needs_restart:
                status_parts.append("Параметры сервера изменены — требуется ручной рестарт.")
            status_message = " ".join(status_parts)

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
        config_warnings=config_warnings,
        needs_restart=needs_restart,
        monitor_restart_required=monitor_restart_required,
        errors=errors,
        users=users,
        allowed_roles=sorted(ALLOWED_ROLES),
        current_user=session.get("user"),
        role_permissions=get_all_role_permissions(),
        permission_fields=list(PERMISSION_FIELDS),
    )


@settings_bp.route("/settings/users", methods=["GET"])
@login_required
@permissions_required("can_manage_users")
def users_settings_page():
    return settings_page()


def _json_data() -> dict:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


@settings_bp.route("/settings/users/update", methods=["POST"])
@login_required
@permissions_required("can_manage_users", "can_access_settings")
def update_user_info():
    payload = _json_data()
    try:
        user_id = int(payload.get("id", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Неверный идентификатор пользователя."}), 400

    username = payload.get("username", "")
    role = payload.get("role", "")
    is_active = payload.get("is_active", True)

    result = update_user(user_id, username, role, is_active)
    status_code = 200 if result.get("ok") else 400
    body = {"status": "ok"} if result.get("ok") else {"status": "error", "message": result.get("error")}
    return jsonify(body), status_code


@settings_bp.route("/settings/users/delete", methods=["POST"])
@login_required
@permissions_required("can_manage_users", "can_access_settings")
def delete_user_account():
    payload = _json_data()
    try:
        user_id = int(payload.get("id", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Неверный идентификатор пользователя."}), 400

    result = delete_user(user_id)
    status_code = 200 if result.get("ok") else 400
    body = {"status": "ok"} if result.get("ok") else {"status": "error", "message": result.get("error")}
    return jsonify(body), status_code


@settings_bp.route("/settings/users/reset_password", methods=["POST"])
@login_required
@permissions_required("can_manage_users", "can_access_settings")
def change_user_password():
    payload = _json_data()
    try:
        user_id = int(payload.get("id", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Неверный идентификатор пользователя."}), 400

    new_password = (payload.get("new_password") or "").strip()
    if not new_password:
        return jsonify({"status": "error", "message": "Пароль не может быть пустым."}), 400

    result = reset_user_password(user_id, new_password, session.get("user"))
    status_code = 200 if result.get("ok") else 400
    if result.get("ok"):
        body = {"status": "ok", "password": new_password}
        logger.info(
            "[security] Пароль пользователя id=%s сброшен администратором %s",
            user_id,
            session.get("user"),
        )
    else:
        body = {"status": "error", "message": result.get("error")}
    return jsonify(body), status_code


@settings_bp.route("/settings/roles/save", methods=["POST"])
@login_required
@permissions_required("can_access_settings", "can_manage_users")
def update_role_permissions():

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
    logger.info("[settings] Права ролей обновлены пользователем %s", session.get("user"))

    return redirect(url_for("settings.settings_page"))


@settings_bp.route("/api/metrics", methods=["GET"])
@login_required
@permissions_required("can_access_metrics")
def api_metrics():

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
@permissions_required("can_manage_users")
def add_user():

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
@permissions_required("can_manage_users")
def change_role():

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
@permissions_required("can_manage_users")
def reset_password():

    try:
        user_id = int(request.form.get("user_id", "0"))
    except ValueError:
        return jsonify({"ok": False, "error": "Неверный идентификатор пользователя."}), 400

    new_password = request.form.get("new_password", "")
    result = reset_user_password(user_id, new_password, session.get("user"))
    status_code = 200 if result.get("ok") else 400
    if result.get("ok"):
        logger.info(
            "[security] Пароль пользователя id=%s сброшен администратором %s",
            user_id,
            session.get("user"),
        )
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(result), status_code
    return redirect(url_for("settings.settings_page"))
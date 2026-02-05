
import logging
import time
from copy import deepcopy

from flask import (
    Blueprint,
    g,
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
from app.dal.db import DEFAULT_ROLE_PERMISSIONS
from app.dal.permissions import (
    PERMISSION_FIELDS,
    create_role,
    delete_role,
    get_all_role_permissions,
    permissions_required,
    save_role_permissions,
)
from app.dal.users import (
    create_user,
    delete_user,
    get_allowed_roles,
    get_all_users,
    get_user_by_username,
    reset_user_password,
    update_user,
    update_user_role,
)
from app import metrics, metrics_lock, ts_ago
from app.routes.auth import login_required
from app.services import telegram as telegram_service
from app.services.audit import fetch_events, log_order_event

settings_bp = Blueprint("settings", __name__)
logger = logging.getLogger("bazis")


@settings_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    global CONFIG

    role_perms = getattr(g, "role_perms", {})
    if not role_perms.get("can_access_settings"):
        return redirect(url_for("orders.index"))

    status_message = None
    errors = []
    config_warnings = list(CONFIG_WARNINGS)
    needs_restart = False
    monitor_restart_required = False

    flashed_status = session.pop("settings_status", None)
    flashed_errors = session.pop("settings_errors", [])
    if flashed_status:
        status_message = flashed_status
    if flashed_errors:
        errors.extend(flashed_errors)

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

        if role_perms.get("can_edit_paths"):
            orders_path = request.form.get("orders_path", "").strip()
            facades_dir = request.form.get("facades_dir", "").strip()
            prisadka_root = request.form.get("prisadka_root", "").strip()
            desene_cpu_root = request.form.get("desene_cpu_root", "").strip()
            prisadka_client_root = request.form.get("prisadka_client_root", "").strip()
            facades_list_dir = request.form.get("facades_list_dir", "").strip()
            search_raw = request.form.get("search_folders", "")

            server_host = request.form.get("server_host", "").strip()
            server_port_raw = request.form.get("server_port", "").strip()
            debug_mode = request.form.get("debug_mode") == "on"
            pg_url = request.form.get("orders_pg_url", "").strip()
            pg_poll_seconds = request.form.get("orders_pg_poll_seconds", "").strip()
            pg_tail_days = request.form.get("orders_pg_tail_days", "").strip()
            pg_enabled = request.form.get("orders_sync_enabled") == "on"
            plus_rename = request.form.get("orders_plus_rename") == "on"
            anulat_rename = request.form.get("orders_anulat_rename") == "on"
            not_given_folder = request.form.get("not_given_folder_path", "").strip()

            telegram_token = request.form.get("telegram_token", "").strip()
            telegram_chat = request.form.get("telegram_chat_id", "").strip()

            paths_cfg = updated.setdefault("paths", {})
            if orders_path:
                paths_cfg["orders"] = orders_path
            if facades_dir:
                paths_cfg["facades_dir"] = facades_dir
            if prisadka_root:
                paths_cfg["prisadka_root"] = prisadka_root
            if desene_cpu_root:
                paths_cfg["desene_cpu_root"] = desene_cpu_root
            if prisadka_client_root:
                paths_cfg["prisadka_client_root"] = prisadka_client_root
            if facades_list_dir:
                paths_cfg["facades_list_dir"] = facades_list_dir
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

            orders_sync_cfg = updated.setdefault("orders_sync", {})
            orders_sync_cfg["pg_url"] = pg_url
            sync_defaults = app_config.DEFAULT_CONFIG.get("orders_sync", {})
            try:
                orders_sync_cfg["poll_seconds"] = (
                    max(5, int(pg_poll_seconds)) if pg_poll_seconds else sync_defaults.get("poll_seconds", 30)
                )
            except (TypeError, ValueError):
                orders_sync_cfg["poll_seconds"] = sync_defaults.get("poll_seconds", 30)
                errors.append("Интервал опроса PostgreSQL должен быть числом.")
            try:
                orders_sync_cfg["tail_days"] = (
                    max(1, int(pg_tail_days)) if pg_tail_days else sync_defaults.get("tail_days", 60)
                )
            except (TypeError, ValueError):
                orders_sync_cfg["tail_days"] = sync_defaults.get("tail_days", 60)
                errors.append("Хвост по дням для created_at должен быть числом.")
            orders_sync_cfg["enabled"] = pg_enabled
            orders_sync_cfg["approved_plus_rename"] = plus_rename
            orders_sync_cfg["anulat_rename_tech_marker"] = anulat_rename

            if not not_given_folder:
                errors.append("Путь к папке «НЕ ДАЛИ В РАБОТУ» не может быть пустым.")

            auto_confirm_cfg = updated.setdefault("orders_auto_confirm", {})
            auto_confirm_cfg["path_not_given_folder"] = not_given_folder

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

            managers_changed = old_config.get("managers", []) != updated.get("managers", [])
            technologists_changed = old_config.get("technologists", {}) != updated.get("technologists", {})
            search_changed = old_config.get("paths", {}).get("search", {}) != updated.get("paths", {}).get("search", {})
            features_changed = old_config.get("features", {}) != updated.get("features", {})
            orders_sync_changed = old_config.get("orders_sync", {}) != updated.get("orders_sync", {})
            auto_confirm_changed = (
                old_config.get("orders_auto_confirm", {}) != updated.get("orders_auto_confirm", {})
            )
            telegram_changed = old_config.get("telegram", {}) != updated.get("telegram", {})

            monitor_restart_required = paths_changed
            needs_restart = server_changed

            light_changes = []
            heavy_changes = []
            if managers_changed:
                light_changes.append("списки менеджеров")
            if technologists_changed:
                light_changes.append("список технологов")
            if search_changed:
                light_changes.append("пути для поиска")
            if features_changed:
                light_changes.append("флаги функций")
            if orders_sync_changed:
                light_changes.append("параметры Orders (PostgreSQL)")
            if auto_confirm_changed:
                light_changes.append("параметры автоподтверждения")
            if telegram_changed:
                light_changes.append("настройки Telegram")
            if paths_changed:
                heavy_changes.append("файловые пути")
            if server_changed:
                heavy_changes.append("параметры сервера")

            status_lines = []
            if light_changes and not heavy_changes:
                status_lines.append(
                    "Настройки сохранены и применены сразу: " + ", ".join(light_changes)
                )
            elif light_changes:
                status_lines.append(
                    "Часть настроек применена сразу: " + ", ".join(light_changes)
                )
            else:
                status_lines.append("Настройки сохранены.")

            restart_parts = []
            if monitor_restart_required:
                restart_parts.append("перезапустите файловый мониторинг из-за обновления путей")
            if needs_restart:
                restart_parts.append("нужен рестарт сервера после изменения параметров")
            if restart_parts:
                status_lines.append("; ".join(restart_parts))

            status_message = "<br>".join(status_lines)
            changes_summary = ", ".join(light_changes + heavy_changes) or "без изменений"
            log_order_event(
                "settings_update",
                order_name="settings",
                old_value=changes_summary,
                new_value=status_message,
                user=session.get("user"),
            )

    managers_text = "\n".join(MANAGER_NAMES)
    technologists_text = "\n".join(
        f"{marker}={name}" for marker, name in TECHNOLOGIST_MARKERS.items()
    )
    search_source = app_config.CONFIG.get("paths", {}).get("search") or app_config.SEARCH_FOLDERS
    search_text = request.form.get("search_folders") or "\n".join(
        f"{title}={path}" for title, path in (search_source or {}).items()
    )

    users = get_all_users()
    available_roles = sorted(get_allowed_roles())
    audit_events = fetch_events(limit=120)

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
        allowed_roles=available_roles,
        current_user=session.get("user"),
        role_permissions=get_all_role_permissions(),
        permission_fields=list(PERMISSION_FIELDS),
        protected_roles=set(DEFAULT_ROLE_PERMISSIONS.keys()),
        role_usage={user["role"] for user in users},
        audit_events=audit_events,
    )


@settings_bp.route("/journal", methods=["GET"])
@login_required
@permissions_required("can_access_settings")
def audit_journal():
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    per_page = 50
    safe_page = max(1, page)
    offset = (safe_page - 1) * per_page

    events = fetch_events(limit=per_page, offset=offset)
    has_next = len(events) == per_page

    return render_template(
        "journal.html",
        events=events,
        page=safe_page,
        has_next=has_next,
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


def _checkbox_enabled(form, key: str) -> bool:
    """Надёжно определяет состояние чекбокса с парой hidden+checkbox."""
    values = [str(value).strip().lower() for value in form.getlist(key)]
    if not values:
        return False
    return any(value in {"1", "true", "on", "yes"} for value in values)


def _build_response(result: dict):
    if result.get("ok"):
        return {"status": "ok"}, 200

    message = result.get("message") or result.get("error") or "Запрос не выполнен."
    code = result.get("error_code") or result.get("code")
    payload = {"status": "error", "message": message}
    if code:
        payload["code"] = code
    return payload, 400


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
    body, status_code = _build_response(result)
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
    body, status_code = _build_response(result)
    if result.get("ok"):
        log_order_event(
            "user_delete",
            order_name=str(user_id),
            user=session.get("user"),
        )
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
    if result.get("ok"):
        body = {"status": "ok", "password": new_password}
        logger.info(
            "[security] Пароль пользователя id=%s сброшен администратором %s",
            user_id,
            session.get("user"),
        )
        return jsonify(body), 200

    body, status_code = _build_response(result)
    return jsonify(body), status_code


@settings_bp.route("/settings/roles/save", methods=["POST"])
@login_required
@permissions_required("can_access_settings", "can_manage_users")
def update_role_permissions():
    role_names = {name.strip().lower() for name in request.form.getlist("role_names[]") if name}
    existing = get_all_role_permissions()
    if not role_names:
        role_names = set(existing.keys())

    updates = {}
    for role in role_names:
        perms = {}
        for field in PERMISSION_FIELDS:
            key = f"roles[{role}][{field}]"
            perms[field] = 1 if _checkbox_enabled(request.form, key) else 0
        updates[role] = perms

    updates.setdefault("admin", {})
    updates["admin"]["can_access_settings"] = 1
    updates["admin"]["can_manage_users"] = 1

    save_role_permissions(updates)
    logger.info("[settings] Права ролей обновлены пользователем %s", session.get("user"))
    session["settings_status"] = "Права ролей обновлены."

    return redirect(url_for("settings.settings_page"))


@settings_bp.route("/settings/roles/create", methods=["POST"])
@login_required
@permissions_required("can_access_settings", "can_manage_users")
def create_role_entry():
    role_name = (request.form.get("role_name") or "").strip()
    perms = {field: 1 if _checkbox_enabled(request.form, f"new_role[{field}]") else 0 for field in PERMISSION_FIELDS}

    result = create_role(role_name, perms)
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        body, status_code = _build_response(result)
        return jsonify(body), status_code

    if result.get("ok"):
        session["settings_status"] = f"Роль «{result.get('role', role_name)}» создана."
    else:
        session["settings_errors"] = [result.get("message")]

    return redirect(url_for("settings.settings_page"))


@settings_bp.route("/settings/roles/<role>/delete", methods=["POST"])
@login_required
@permissions_required("can_access_settings", "can_manage_users")
def delete_role_entry(role: str):
    normalized = (role or "").strip().lower()
    result = delete_role(normalized)

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        body, status_code = _build_response(result)
        return jsonify(body), status_code

    if result.get("ok"):
        session["settings_status"] = f"Роль «{normalized}» удалена."
    else:
        session["settings_errors"] = [result.get("message")]

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
    cutoff = time.time() - 24 * 3600
    data["errors"]["errors_last_24h"] = sum(
        1 for ts in data.get("errors", {}).get("last_24h", []) if ts > cutoff
    )

    return jsonify(data)


@settings_bp.route("/admin/users/add", methods=["POST"])
@login_required
@permissions_required("can_manage_users")
def add_user():

    username = (request.form.get("username", "") or "").strip()
    password = (request.form.get("password", "") or "").strip()
    role = (request.form.get("role", "") or "").strip()

    existing = get_user_by_username(username)
    if existing:
        result = {"ok": False, "message": "Пользователь с таким именем уже существует.", "error_code": "username_taken"}
    else:
        result = create_user(username, password, role)
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        body, status_code = _build_response(result)
        return jsonify(body), status_code

    if result.get("ok"):
        session["settings_status"] = "Пользователь создан."
        log_order_event(
            "user_create",
            order_name=username,
            new_value=f"role={role}",
            user=session.get("user"),
        )
    else:
        session["settings_errors"] = [result.get("message")]
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
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        body, status_code = _build_response(result)
        return jsonify(body), status_code
    if result.get("ok"):
        session["settings_status"] = "Роль пользователя обновлена."
    else:
        session["settings_errors"] = [result.get("message")]
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
    if result.get("ok"):
        logger.info(
            "[security] Пароль пользователя id=%s сброшен администратором %s",
            user_id,
            session.get("user"),
        )
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        body, status_code = _build_response(result)
        return jsonify(body), status_code
    if result.get("ok"):
        session["settings_status"] = "Пароль пользователя обновлён."
    else:
        session["settings_errors"] = [result.get("error") or result.get("message")]
    return redirect(url_for("settings.settings_page"))

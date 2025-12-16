import json
import os
import queue
import time

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)

import app as bazis_app
import app.config as app_config
from app import (
    SSEClient,
    get_manager_from_name,
    logger,
    measure_time,
    sse_clients,
    sse_clients_lock,
)
from app.dal.permissions import has_permission, permissions_required
from app.dal.manager_priced import get_priced_map, get_priced_set, is_priced, set_priced
from app.services.audit import log_order_event
from app.services.monitor import (
    discard_programmatic_move,
    move_known_folder,
    register_programmatic_move,
)
from app.services.snapshot import (
    build_orders_payload,
    calculate_period_cutoff,
    refresh_search_index,
    remove_order,
    collect_month_scope,
    search_in_index,
    upsert_order,
)
from app.services.telegram import (
    folder_has_ready_marker,
    technologist_from_folder,
    update_message_for_folder,
)


def get_visible_manager_filter(request, session):
    """Возвращает фильтр менеджера для текущего запроса.

    На данный момент все роли видят полный список заказов, поэтому функция
    всегда возвращает ``None``. Логика фильтрации может быть добавлена позже,
    если появится бизнес-требование ограничивать видимость заказов.
    """

    return None


def _filter_by_manager(folders, visible_manager, requested_manager):
    applied_manager_filter = visible_manager
    if applied_manager_filter is None and requested_manager not in {"", "Все"}:
        applied_manager_filter = requested_manager

    if applied_manager_filter:
        folders = [
            folder
            for folder in folders
            if (folder.get("manager") or "Неизвестно") == applied_manager_filter
        ]

    return folders, applied_manager_filter


def _prepare_event_for_client(event: dict, visible_manager, requested_manager):
    if not isinstance(event, dict):
        return event

    folders = event.get("folders") or event.get("orders")
    if folders is None:
        return event

    filtered_folders, applied_manager = _filter_by_manager(
        list(folders), visible_manager, requested_manager
    )

    manager_stats = {name: 0 for name in app_config.MANAGER_NAMES}
    manager_stats["Неизвестно"] = manager_stats.get("Неизвестно", 0)
    tech_stats = {name: 0 for name in app_config.TECHNOLOGIST_MARKERS.values()}
    tech_stats["Неизвестно"] = tech_stats.get("Неизвестно", 0)

    for folder in filtered_folders:
        manager_name = folder.get("manager") or "Неизвестно"
        technologist = folder.get("technologist") or "Неизвестно"
        manager_stats.setdefault(manager_name, 0)
        manager_stats[manager_name] += 1
        tech_stats.setdefault(technologist, 0)
        tech_stats[technologist] += 1

    prepared = dict(event)
    prepared["folders"] = filtered_folders
    prepared["orders"] = filtered_folders
    prepared["total"] = len(filtered_folders)
    prepared["managers"] = manager_stats
    prepared["technologists"] = tech_stats
    prepared["applied_manager"] = applied_manager
    return prepared


def _snapshot_copy():
    """Возвращает копию текущего снапшота заказов."""
    with bazis_app.orders_snapshot_lock:
        return list(bazis_app.orders_snapshot)


def _find_order_in_snapshot(order_key: str):
    for order in _snapshot_copy():
        if (order.get("name") or "") == order_key:
            return order
    return None


def _technologist_marker(folder_name: str) -> tuple[str | None, str]:
    for marker, tech_name in app_config.TECHNOLOGIST_MARKERS.items():
        if f"[{marker}]" in folder_name:
            return marker, tech_name
    return None, technologist_from_folder(folder_name) or "—"


def _is_ready_status(status: str | None) -> bool:
    return (status or "").strip() == "Готов"


orders_bp = Blueprint("orders", __name__)


@orders_bp.route("/")
def index():
    priced_set = set()
    priced_map = {}
    current_user = session.get("user")
    current_role = session.get("role")
    role_perms = getattr(g, "role_perms", {})

    if role_perms.get("can_view_priced"):
        if current_role == "manager" and current_user:
            priced_set = get_priced_set(current_user)
        elif current_role in {"admin", "technologist"}:
            priced_map = get_priced_map()

    return render_template(
        "index.html",
        manager_priced=sorted(priced_set),
        manager_priced_map=priced_map,
    )


@orders_bp.route("/facades")
def facades_page():
    if not getattr(g, "role_perms", {}).get("can_access_facades"):
        return redirect(url_for("orders.index"))

    template_path = os.path.join(current_app.template_folder or "", "facades.html")
    if template_path and not os.path.exists(template_path):
        logger.error("Шаблон фасадов не найден: %s", template_path)
        abort(
            500,
            description="Не найден шаблон facades.html. Убедитесь, что файл находится в папке templates.",
        )
    return render_template("facades.html")


@orders_bp.route("/data")
def data():
    visible_manager = get_visible_manager_filter(request, session)
    requested_manager = request.args.get("manager", "Все")
    payload = build_orders_payload(visible_manager, requested_manager)

    return jsonify(payload)


@orders_bp.route("/api/manager/priced_set")
def api_manager_priced_set():
    current_user = session.get("user")
    if not current_user:
        abort(401)

    role_perms = getattr(g, "role_perms", {})
    if not role_perms.get("can_view_priced"):
        abort(403)

    priced = []
    priced_map: dict[str, list[str]] = {}
    role = session.get("role")
    if role not in {"manager", "admin", "technologist"}:
        abort(403)

    if role == "manager":
        priced = sorted(get_priced_set(current_user))
        priced_map = {key: [current_user] for key in priced}
    elif role in {"admin", "technologist"}:
        priced_map = get_priced_map()

    return jsonify({"priced": priced, "priced_map": priced_map})


@orders_bp.route("/api/manager/pending_priced")
def api_manager_pending_priced():
    current_user = session.get("user")
    if not current_user:
        abort(401)

    role_perms = getattr(g, "role_perms", {})
    if not role_perms.get("can_view_priced_panel"):
        abort(403)

    role = session.get("role")
    if role not in {"manager", "admin", "technologist"}:
        abort(403)

    priced = get_priced_set(current_user) if role == "manager" else set()
    priced_map = get_priced_map()
    snapshot = _snapshot_copy()
    pending: list[dict] = []

    for order in snapshot:
        if not order:
            continue

        order_key = order.get("name") or ""
        order_status = order.get("status") or ""
        if not _is_ready_status(order_status):
            continue

        marker_code, tech_name = _technologist_marker(order_key)
        if not marker_code:
            continue

        manager_for_order = (order.get("manager") or get_manager_from_name(order_key)).strip()
        if not manager_for_order:
            continue

        if role == "manager" and manager_for_order != current_user:
            continue

        if role == "manager":
            if order_key in priced:
                continue
        else:
            if manager_for_order and manager_for_order in priced_map.get(order_key, []):
                continue

        unc_path = os.path.join(app_config.FOLDER_PATH or "", order_key)
        pending.append(
            {
                "order_key": order_key,
                "display_name": order_key,
                "tech_code": marker_code,
                "tech_name": tech_name or "—",
                "unc_path": unc_path,
            }
        )

    return jsonify(pending)


@orders_bp.route("/api/manager/mark_priced", methods=["POST"])
def api_manager_mark_priced():
    current_user = session.get("user")
    if not current_user:
        abort(401)

    role_perms = getattr(g, "role_perms", {})
    if not role_perms.get("can_mark_priced"):
        return jsonify({"ok": False, "message": "Недостаточно прав"}), 403

    if session.get("role") == "technologist":
        return jsonify({"ok": False, "message": "Недостаточно прав"}), 403

    payload = request.get_json(silent=True) or {}
    order_key = (payload.get("order_key") or "").strip()
    if not order_key:
        return jsonify({"ok": False, "message": "order_key required"}), 400

    order = _find_order_in_snapshot(order_key)
    if not order:
        return jsonify({"ok": False, "message": "Заказ не найден"}), 404

    manager_for_order = (order.get("manager") or get_manager_from_name(order_key)).strip()
    if not manager_for_order:
        return jsonify({"ok": False, "message": "Не указан менеджер заказа"}), 400
    role = session.get("role")
    if role == "manager" and manager_for_order != current_user:
        return jsonify({"ok": False, "message": "Недостаточно прав"}), 403

    if not _is_ready_status(order.get("status")):
        return jsonify({"ok": False, "message": 'Заказ ещё не имеет статуса "Готов".'}), 400

    marker_code, _ = _technologist_marker(order_key)
    if not marker_code:
        return jsonify({"ok": False, "message": "Заказ без отметки технолога"}), 400

    target_manager = current_user if role == "manager" else manager_for_order

    if not is_priced(order_key, target_manager):
        set_priced(order_key, target_manager)

    return jsonify({"ok": True})


@orders_bp.route("/search", methods=["GET", "POST"])
def search_page():
    if not getattr(g, "role_perms", {}).get("can_access_search"):
        return redirect(url_for("orders.index"))

    query = request.form.get("query", "").strip()
    period_choice = (request.form.get("period") or request.args.get("period") or "2").strip()
    if period_choice == "all":
        months_back = 0
    else:
        try:
            months_back = max(1, min(24, int(period_choice)))
        except (TypeError, ValueError):
            months_back = 2

    period_cutoff_ts = calculate_period_cutoff(months_back) if months_back else None

    results = {key: [] for key in app_config.SEARCH_FOLDERS.keys()}

    if query:
        logger.info("[search] Запрос поиска: %s", query)
        with bazis_app.order_index_lock:
            is_index_fresh = bool(bazis_app.order_index_updated_at) and (
                time.time() - bazis_app.order_index_updated_at < bazis_app.SNAPSHOT_TTL * 2
            )

        if not is_index_fresh:
            month_scope = collect_month_scope(months_back)
            refresh_search_index(full=True, months_back=months_back, month_scope=month_scope)
        else:
            month_scope = collect_month_scope(months_back)

        results = search_in_index(query, months_scope=month_scope, cutoff_ts=period_cutoff_ts)
    else:
        month_scope = collect_month_scope(months_back)

    manager_filter = get_visible_manager_filter(request, session)
    if manager_filter:
        filtered_results = {}
        for key, items in results.items():
            filtered_results[key] = [
                item for item in items if (item.get("manager") or "") == manager_filter
            ]
        results = filtered_results

    return render_template(
        "search.html",
        query=query,
        results=results,
        SEARCH_FOLDERS=app_config.SEARCH_FOLDERS,
        period_choice=period_choice,
        month_scope=month_scope,
    )


@orders_bp.route("/events")
def events():
    client = SSEClient()
    with sse_clients_lock:
        sse_clients.add(client)
        bazis_app.metrics["sse"]["active_clients"] = len(sse_clients)

    visible_manager = get_visible_manager_filter(request, session)
    requested_manager = request.args.get("manager", "Все")

    def gen():
        initial = build_orders_payload(visible_manager, requested_manager)
        initial = _prepare_event_for_client(initial, visible_manager, requested_manager)
        yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"

        last_ping = time.time()
        try:
            while True:
                try:
                    ev = client.q.get(timeout=15)
                    prepared = _prepare_event_for_client(ev, visible_manager, requested_manager)
                    yield f"data: {json.dumps(prepared, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
                if time.time() - last_ping > 60:
                    last_ping = time.time()
        finally:
            client.alive = False
            with sse_clients_lock:
                sse_clients.discard(client)
                bazis_app.metrics["sse"]["active_clients"] = len(sse_clients)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(gen()), headers=headers)


@orders_bp.route("/facades/generate", methods=["POST"])
@permissions_required("can_access_facades")
def generate_facades():

    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])

    lines = []
    errors = []

    for idx, item in enumerate(items, start=1):
        position = str(item.get("position", "")).strip()
        width = str(item.get("width", "")).strip()
        height = str(item.get("height", "")).strip()
        count = str(item.get("count", "")).strip()
        side = str(item.get("side", "")).strip().lower()
        hinges = str(item.get("hinges", "")).strip()

        if not width or not height or not count:
            errors.append(f"Строка {idx}: заполните высоту, ширину и количество.")
            continue

        try:
            width_int = int(width)
            height_int = int(height)
            count_int = int(count)
        except ValueError:
            errors.append(f"Строка {idx}: высота, ширина и количество должны быть числами.")
            continue

        if width_int <= 0 or height_int <= 0 or count_int <= 0:
            errors.append(f"Строка {idx}: значения должны быть больше нуля.")
            continue

        if side not in ("left", "right", "левая", "правая", "l", "r"):
            errors.append(f"Строка {idx}: выберите сторону (левая/правая).")
            continue

        try:
            hinges_int = int(hinges)
        except ValueError:
            errors.append(f"Строка {idx}: количество петель должно быть числом.")
            continue

        if hinges_int < 2 or hinges_int > 6:
            errors.append(f"Строка {idx}: количество петель должно быть от 2 до 6.")
            continue

        side_code = "L" if side.startswith("l") or side.startswith("л") else "R"

        line_parts = []
        if position:
            line_parts.append(position)

        line_parts.extend(
            [
                str(height_int),
                str(width_int),
                str(count_int),
                side_code,
                str(hinges_int),
            ]
        )

        lines.append(" ".join(line_parts))

    if errors:
        return jsonify({"status": "error", "errors": errors}), 400

    if not lines:
        return jsonify(
            {"status": "error", "errors": ["Добавьте хотя бы один фасад перед генерацией."]},
            400,
        )

    target_dir = os.path.dirname(app_config.FACADES_FILE)
    if target_dir:
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            return (
                jsonify(
                    {
                        "status": "error",
                        "errors": [f"Не удалось создать папку {target_dir}: {exc}"],
                    }
                ),
                500,
            )

    try:
        with open(app_config.FACADES_FILE, "w", encoding="utf-8") as file:
            file.write("# position(optional) height width count side hinges\n")
            for line in lines:
                file.write(line + "\n")
    except OSError as exc:
        return (
            jsonify({"status": "error", "errors": [f"Не удалось записать файл: {exc}"]}),
            500,
        )

    return jsonify(
        {
            "status": "ok",
            "file": os.path.basename(app_config.FACADES_FILE),
            "folder": app_config.FACADES_DIR
            or os.path.dirname(os.path.abspath(app_config.FACADES_FILE)),
        }
    )


@orders_bp.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"})


@orders_bp.route("/open_folder", methods=["POST"])
def open_folder():
    folder_path = request.form.get("path")
    if not folder_path:
        return jsonify({"status": "error", "message": "Путь не указан"}), 400

    if not os.path.exists(folder_path):
        return jsonify({"status": "error", "message": "Путь не найден"}), 404

    start_fn = getattr(os, "startfile", None)
    if start_fn is None:
        logger.warning("[open_folder] os.startfile недоступен для %s", folder_path)
        return (
            jsonify({"status": "error", "message": "Открытие папки не поддерживается"}),
            501,
        )

    try:
        start_fn(folder_path)
    except Exception as exc:
        logger.exception("Ошибка открытия папки %s", folder_path, exc_info=exc)
        return (
            jsonify({"status": "error", "message": "Не удалось открыть папку"}),
            500,
        )

    return ("", 204)


@orders_bp.route("/confirm_order", methods=["POST"])
@measure_time("confirm_order")
def confirm_order():
    if not app_config.ORDER_CONFIRMATION_ENABLED:
        logger.warning("[confirm_order] Попытка подтверждения при выключенной функции")
        return (
            jsonify({"status": "error", "message": "Подтверждение заказов отключено."}),
            400,
        )

    if not app_config.FOLDER_PATH or not os.path.isdir(app_config.FOLDER_PATH):
        return (
            jsonify({"status": "error", "message": "Путь к папке заказов не настроен."}),
            500,
        )

    payload = request.get_json(silent=True) or {}
    folder_name = (payload.get("folder") or "").strip()
    if not folder_name:
        logger.warning("[confirm_order] Не указано имя заказа")
        return (
            jsonify({"status": "error", "message": "Не указано имя заказа."}),
            400,
        )

    current_path = os.path.join(app_config.FOLDER_PATH, folder_name)
    if not os.path.isdir(current_path):
        return jsonify({"status": "error", "message": "Заказ не найден."}), 404

    current_role = session.get("role")
    current_user = session.get("user")
    folder_manager = (get_manager_from_name(folder_name) or "").strip() or "Неизвестно"

    role_perms = getattr(g, "role_perms", {}) or {}
    has_confirm_right = bool(role_perms.get("can_confirm_orders")) or has_permission(
        current_role, "can_confirm_orders"
    )
    if not has_confirm_right:
        logger.warning(
            "[confirm_order] Запрет подтверждения: %s (%s) попытался подтвердить %s",
            current_user,
            current_role,
            folder_name,
        )
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Недостаточно прав для подтверждения заказов.",
                }
            ),
            403,
        )

    allow = False
    if current_role in {"admin", "technologist"}:
        allow = True
    elif current_role == "manager":
        if folder_manager == current_user:
            allow = True
        elif folder_manager in {"", "Неизвестно"}:
            allow = True
    elif current_role:
        allow = False

    if not allow:
        logger.warning(
            "[confirm_order] Недостаточно прав: %s (%s) попытался подтвердить заказ %s (%s)",
            current_user,
            current_role,
            folder_name,
            folder_manager,
        )
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Недостаточно прав для подтверждения этого заказа",
                }
            ),
            403,
        )

    if folder_name.endswith("+"):
        return jsonify(
            {
                "status": "ok",
                "folder": folder_name,
                "message": "Заказ уже подтверждён.",
            }
        )

    if not folder_has_ready_marker(folder_name):
        logger.warning("[confirm_order] Заказ ещё не готов: %s", folder_name)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": 'Заказ ещё не имеет статуса "Готов".',
                }
            ),
            400,
        )

    new_name = f"{folder_name} +"
    new_path = os.path.join(app_config.FOLDER_PATH, new_name)
    if os.path.exists(new_path):
        logger.warning("[confirm_order] Папка уже существует: %s", new_path)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Папка с подтверждённым заказом уже существует.",
                }
            ),
            409,
        )

    register_programmatic_move(current_path, new_path)

    try:
        os.rename(current_path, new_path)
    except OSError as exc:
        logger.exception("[confirm_order] Не удалось подтвердить заказ %s", folder_name, exc_info=exc)
        discard_programmatic_move(current_path, new_path)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Не удалось подтвердить заказ: {exc}",
                }
            ),
            500,
        )

    move_known_folder(folder_name, new_name)
    update_message_for_folder(folder_name, new_name)
    remove_order(folder_name)
    upsert_order(new_name)

    logger.info("[confirm_order] Заказ подтверждён: %s -> %s", folder_name, new_name)
    log_order_event(
        "rename",
        order_name=new_name,
        old_value=folder_name,
        new_value=new_name,
        manager=folder_manager,
    )
    log_order_event(
        "confirm",
        order_name=new_name,
        old_value=folder_name,
        new_value="confirmed",
        manager=folder_manager,
    )

    return jsonify({"status": "ok", "folder": new_name})
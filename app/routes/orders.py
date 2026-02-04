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
    sse_clients,
    sse_clients_lock,
)
from app.dal.permissions import permissions_required
from app.dal.order_manager_override import upsert_override
from app.security import csrf_error_response, validate_csrf_token
from app.services.snapshot import (
    apply_manager_override_to_snapshot,
    build_orders_payload,
    calculate_period_cutoff,
    refresh_search_index,
    remove_order,
    collect_month_scope,
    search_in_index,
    upsert_order,
)
from app.services.order_times import normalize_order_key
from app.services.audit import log_order_event
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
        snapshot_key = order.get("order_key") or ""
        if snapshot_key and snapshot_key == order_key:
            return order
        if (order.get("name") or "") == order_key:
            return order
    return None


orders_bp = Blueprint("orders", __name__)


@orders_bp.route("/")
def index():
    current_user = session.get("user")
    current_role = session.get("role")
    role_perms = getattr(g, "role_perms", {})

    return render_template(
        "index.html",
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


@orders_bp.route("/api/managers")
def api_managers():
    return jsonify({"status": "ok", "managers": app_config.MANAGER_NAMES})


@orders_bp.route("/api/orders/<order_key>/manager", methods=["POST"])
@permissions_required("can_edit_order_manager")
def update_order_manager(order_key: str):
    ok, message = validate_csrf_token()
    if not ok:
        return csrf_error_response(message)

    payload = request.get_json(silent=True) or {}
    manager_name = (
        payload.get("manager_name")
        or payload.get("manager_id")
        or payload.get("manager")
        or ""
    ).strip()
    if not manager_name:
        return jsonify({"status": "error", "message": "Менеджер не задан."}), 400

    if manager_name not in app_config.MANAGER_NAMES:
        return jsonify({"status": "error", "message": "Менеджер не найден."}), 400

    normalized_key = normalize_order_key(order_key)
    if not normalized_key:
        return jsonify({"status": "error", "message": "Некорректный ключ заказа."}), 400

    snapshot_item = _find_order_in_snapshot(normalized_key) or _find_order_in_snapshot(order_key)
    if not snapshot_item:
        return jsonify({"status": "error", "message": "Заказ не найден."}), 404

    updated_by = session.get("user") or ""
    record = upsert_override(normalized_key, manager_name, updated_by)
    updated_at = record.updated_at.isoformat() if record and record.updated_at else ""

    updated_item = apply_manager_override_to_snapshot(
        normalized_key,
        manager_name,
        updated_by=updated_by,
        updated_at=updated_at,
    ) or dict(snapshot_item)
    updated_item["manager"] = manager_name

    log_order_event(
        "manager_override",
        order_name=snapshot_item.get("name") or normalized_key,
        manager=manager_name,
        old_value=snapshot_item.get("manager") or "",
        new_value=manager_name,
    )

    return jsonify({"status": "ok", "order": updated_item})


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

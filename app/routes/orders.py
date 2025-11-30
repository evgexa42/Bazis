import json
import os
import queue
import time

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)

import app as bazis_app
from app import (
    SSEClient,
    get_manager_from_name,
    logger,
    measure_time,
    sse_clients,
    sse_clients_lock,
)
from app.dal.permissions import has_permission
from app.services.monitor import move_known_folder
from app.services.snapshot import (
    build_orders_payload,
    refresh_search_index,
    remove_order,
    search_in_index,
    upsert_order,
)
from app.services.telegram import folder_has_ready_marker, update_message_for_folder


def get_visible_manager_filter(request, session):
    """Определяет, нужно ли ограничить менеджера для текущего запроса."""

    role = session.get("role")

    if role in {"admin", "technologist", "manager"}:
        return None

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

    manager_stats = {name: 0 for name in bazis_app.MANAGER_NAMES}
    manager_stats["Неизвестно"] = manager_stats.get("Неизвестно", 0)
    tech_stats = {name: 0 for name in bazis_app.TECHNOLOGIST_MARKERS.values()}
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

orders_bp = Blueprint("orders", __name__)


@orders_bp.route("/")
def index():
    return render_template("index.html")


@orders_bp.route("/facades")
def facades_page():
    if not has_permission(session.get("role"), "can_access_facades"):
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


@orders_bp.route("/search", methods=["GET", "POST"])
def search_page():
    if not has_permission(session.get("role"), "can_access_search"):
        return redirect(url_for("orders.index"))

    query = request.form.get("query", "").strip()
    results = {key: [] for key in bazis_app.SEARCH_FOLDERS.keys()}

    if query:
        logger.info("[search] Запрос поиска: %s", query)
        with bazis_app.order_index_lock:
            is_index_fresh = bool(bazis_app.order_index_updated_at) and (
                time.time() - bazis_app.order_index_updated_at < bazis_app.SNAPSHOT_TTL * 2
            )

        if not is_index_fresh:
            refresh_search_index(full=True)

        results = search_in_index(query)

    manager_filter = get_visible_manager_filter(request, session)
    if manager_filter:
        filtered_results = {}
        for key, items in results.items():
            filtered_results[key] = [
                item for item in items if (item.get("manager") or "") == manager_filter
            ]
        results = filtered_results

    return render_template(
        "search.html", query=query, results=results, SEARCH_FOLDERS=bazis_app.SEARCH_FOLDERS
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
def generate_facades():
    if not has_permission(session.get("role"), "can_access_facades"):
        return abort(403)

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

    target_dir = os.path.dirname(bazis_app.FACADES_FILE)
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
        with open(bazis_app.FACADES_FILE, "w", encoding="utf-8") as file:
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
            "file": os.path.basename(bazis_app.FACADES_FILE),
            "folder": bazis_app.FACADES_DIR
            or os.path.dirname(os.path.abspath(bazis_app.FACADES_FILE)),
        }
    )


@orders_bp.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"})


@orders_bp.route("/open_folder", methods=["POST"])
def open_folder():
    folder_path = request.form.get("path")
    if folder_path and os.path.exists(folder_path):
        try:
            os.startfile(folder_path)
        except Exception as exc:
            logger.exception("Ошибка открытия папки %s", folder_path, exc_info=exc)
    return ("", 204)


@orders_bp.route("/confirm_order", methods=["POST"])
@measure_time("confirm_order")
def confirm_order():
    if not bazis_app.ORDER_CONFIRMATION_ENABLED:
        logger.warning("[confirm_order] Попытка подтверждения при выключенной функции")
        return (
            jsonify({"status": "error", "message": "Подтверждение заказов отключено."}),
            400,
        )

    if not bazis_app.FOLDER_PATH or not os.path.isdir(bazis_app.FOLDER_PATH):
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

    current_path = os.path.join(bazis_app.FOLDER_PATH, folder_name)
    if not os.path.isdir(current_path):
        return jsonify({"status": "error", "message": "Заказ не найден."}), 404

    current_role = session.get("role")
    current_user = session.get("user")
    folder_manager = (get_manager_from_name(folder_name) or "").strip() or "Неизвестно"

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
    new_path = os.path.join(bazis_app.FOLDER_PATH, new_name)
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

    try:
        os.rename(current_path, new_path)
    except OSError as exc:
        logger.exception("[confirm_order] Не удалось подтвердить заказ %s", folder_name, exc_info=exc)
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

    return jsonify({"status": "ok", "folder": new_name})
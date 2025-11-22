import json
import os
import time

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    session,
    stream_with_context,
)

import app as bazis_app
from app import get_manager_from_name, logger, measure_time, metrics_lock
from app.services.monitor import move_known_folder
from app.services.snapshot import get_orders_snapshot, refresh_orders_snapshot, search_in_index
from app.services.telegram import folder_has_ready_marker, update_message_for_folder


def get_visible_manager_filter(request, session):
    """Определяет, нужно ли ограничить менеджера для текущего запроса."""

    role = session.get("role")

    if role in {"admin", "technologist"}:
        return None

    if role == "manager":
        requested_manager = (request.values.get("manager") or "").strip()
        show_all_flag = (request.values.get("show_all") or "").strip()

        if requested_manager == "Все" or show_all_flag == "1":
            return None

        return session.get("user")

    return None

orders_bp = Blueprint("orders", __name__)


def _apply_manager_filter(folders, visible_manager, requested_manager):
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


def _build_orders_payload(visible_manager, requested_manager):
    folders = get_orders_snapshot(ttl=bazis_app.SNAPSHOT_TTL)
    folders, _ = _apply_manager_filter(folders, visible_manager, requested_manager)

    total_orders = len(folders)
    manager_stats = {name: 0 for name in bazis_app.MANAGER_NAMES}
    manager_stats["Неизвестно"] = manager_stats.get("Неизвестно", 0)
    for folder in folders:
        manager_name = folder.get("manager") or "Неизвестно"
        manager_stats.setdefault(manager_name, 0)
        manager_stats[manager_name] += 1

    tech_stats = {name: 0 for name in bazis_app.TECHNOLOGIST_MARKERS.values()}
    tech_stats["Неизвестно"] = tech_stats.get("Неизвестно", 0)
    for folder in folders:
        technologist_name = folder.get("technologist") or "Неизвестно"
        tech_stats.setdefault(technologist_name, 0)
        tech_stats[technologist_name] += 1

    return {
        "orders": folders,
        "folders": folders,
        "total": total_orders,
        "managers": manager_stats,
        "technologists": tech_stats,
        "version": bazis_app.snapshot_version,
        "last_snapshot_ts": bazis_app.last_snapshot_ts,
    }


@orders_bp.route("/")
def index():
    return render_template("index.html")


@orders_bp.route("/facades")
def facades_page():
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
    payload = _build_orders_payload(visible_manager, requested_manager)

    return jsonify(payload)


@orders_bp.route("/search", methods=["GET", "POST"])
def search_page():
    query = request.form.get("query", "").strip()
    results = {key: [] for key in bazis_app.SEARCH_FOLDERS.keys()}

    if query:
        logger.info("[search] Запрос поиска: %s", query)
        with bazis_app.order_index_lock:
            is_index_fresh = bool(bazis_app.order_index_updated_at) and (
                time.time() - bazis_app.order_index_updated_at < bazis_app.SNAPSHOT_TTL * 2
            )

        if not is_index_fresh:
            refresh_orders_snapshot(force=True)

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


@orders_bp.route("/stream")
def stream():
    visible_manager = get_visible_manager_filter(request, session)
    requested_manager = request.args.get("manager", "Все")

    @stream_with_context
    def event_stream():
        last_version_sent = None
        last_heartbeat = time.time()
        with metrics_lock:
            bazis_app.active_sse_clients += 1
            bazis_app.metrics["sse"]["active_clients"] = bazis_app.active_sse_clients

        try:
            while True:
                current_version = bazis_app.snapshot_version
                now = time.time()

                if last_version_sent != current_version:
                    payload = _build_orders_payload(visible_manager, requested_manager)
                    last_version_sent = current_version
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    with metrics_lock:
                        bazis_app.metrics["sse"]["last_broadcast_ts"] = now
                    last_heartbeat = now
                elif now - last_heartbeat >= 27:
                    yield "data: {\"ping\": true}\n\n"
                    last_heartbeat = now

                time.sleep(0.5)
        finally:
            with metrics_lock:
                bazis_app.active_sse_clients = max(0, bazis_app.active_sse_clients - 1)
                bazis_app.metrics["sse"]["active_clients"] = bazis_app.active_sse_clients

    return Response(event_stream(), mimetype="text/event-stream")


@orders_bp.route("/facades/generate", methods=["POST"])
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

    if current_role not in {"admin", "technologist", "manager"}:
        return (jsonify({"status": "error", "message": "Требуется авторизация."}), 403)

    if current_role == "manager":
        folder_manager = get_manager_from_name(folder_name)
        if folder_manager != current_user:
            logger.warning(
                "[confirm_order] Менеджер %s пытался подтвердить чужой заказ %s (%s)",
                current_user,
                folder_name,
                folder_manager,
            )
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Недостаточно прав для подтверждения заказа.",
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

    logger.info("[confirm_order] Заказ подтверждён: %s -> %s", folder_name, new_name)

    return jsonify({"status": "ok", "folder": new_name})
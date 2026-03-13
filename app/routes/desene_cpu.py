from __future__ import annotations

import json
import os
import re
from datetime import datetime

from flask import Blueprint, g, jsonify, render_template, request, session
from sqlalchemy import select

import app.config as app_config
from app import get_manager_from_name
from app.dal.db import SessionLocal
from app.dal.desene_cpu import (
    CPU_ARCHIVE_STATUSES,
    CPU_STATUS_CONFIRMED,
    CPU_STATUS_IN_REVIEW,
    CPU_STATUS_NEW,
    DeseneCpuAction,
    DeseneCpuOrder,
    log_action,
    normalize_path,
)


desene_cpu_bp = Blueprint("desene_cpu", __name__)


_NOT_DO_MARKER_RE = re.compile(r"\[\s*не\s*делать\s*\]", re.IGNORECASE)


def _build_confirmed_folder_name(folder_name: str) -> str:
    """Строит имя папки после подтверждения в Desene CPU."""
    name = (folder_name or "").rstrip()
    if not name:
        return ""

    if app_config.DESENE_CPU_REPLACE_NOT_DO_MARKER:
        # Поддерживаем оба маркера по требованию: [$] и [НЕ ДЕЛАТЬ] (любой регистр).
        name = _NOT_DO_MARKER_RE.sub("[A]", name)
        name = name.replace("[$]", "[A]")

    if not name.endswith(" +"):
        name = f"{name} +"
    return name


def _rename_order_folder_on_confirm(row: DeseneCpuOrder, actor_user: str) -> None:
    """Переименовывает физическую папку и синхронизирует путь в БД."""
    if not app_config.DESENE_CPU_RENAME_ON_CONFIRM:
        return

    folder_path = (row.folder_path or "").strip()
    folder_name = (row.order_folder_name or "").strip()
    if not folder_path or not folder_name:
        return

    target_name = _build_confirmed_folder_name(folder_name)
    if not target_name or target_name == folder_name:
        return

    parent_dir = os.path.dirname(folder_path)
    target_path = os.path.join(parent_dir, target_name)
    if os.path.exists(target_path):
        raise FileExistsError(f"Целевая папка уже существует: {target_path}")

    os.rename(folder_path, target_path)
    row.folder_path = target_path
    row.order_folder_name = target_name
    row.normalized_path = normalize_path(target_path)

    log_action(
        row.id,
        "RENAME_ON_CONFIRM",
        actor_user,
        json.dumps({"from": folder_name, "to": target_name}, ensure_ascii=False),
    )


def _can_view_order(order: DeseneCpuOrder) -> bool:
    role = session.get("role") or ""
    user = session.get("user") or ""
    if role in {"admin", "technologist"}:
        return True
    if role == "manager":
        # Менеджеры могут смотреть общий список и переключаться между менеджерами.
        return True
    return False


def _can_mutate_order(order: DeseneCpuOrder) -> bool:
    role = session.get("role") or ""
    user = session.get("user") or ""
    if role in {"admin", "technologist"}:
        return True
    if role == "manager":
        # Изменять статус менеджер может только у собственных заказов.
        return (order.manager_name or "") == user
    return False


@desene_cpu_bp.route("/desene_cpu")
def desene_cpu_page():
    if not getattr(g, "role_perms", {}).get("can_access_desene_cpu"):
        return render_template("error.html", error="Нет доступа к разделу Desene CPU."), 403
    return render_template("desene_cpu.html")


@desene_cpu_bp.route("/api/desene_cpu/orders")
def api_orders():
    if not getattr(g, "role_perms", {}).get("can_access_desene_cpu"):
        return jsonify({"status": "error", "message": "Нет прав"}), 403

    tab = (request.args.get("tab") or "active").strip().lower()
    q = (request.args.get("q") or "").strip().lower()
    month = (request.args.get("month") or "").strip()
    status_filter = (request.args.get("status") or "all").strip().upper()
    manager_filter = (request.args.get("manager") or "").strip()
    current_role = (session.get("role") or "").strip().lower()
    current_user = (session.get("user") or "").strip()
    current_month_key = datetime.now().strftime("%Y-%m")

    with SessionLocal.begin() as session_db:
        stmt = select(DeseneCpuOrder).where(DeseneCpuOrder.pdf_found == 1)
        if tab == "archive":
            stmt = stmt.where(DeseneCpuOrder.status.in_(CPU_ARCHIVE_STATUSES))
        else:
            stmt = stmt.where(DeseneCpuOrder.status.in_([CPU_STATUS_NEW, CPU_STATUS_IN_REVIEW]))

        rows = session_db.execute(stmt).scalars().all()

        for row in rows:
            # Подтягиваем актуального менеджера из справочника клиентов.
            mapped_manager = get_manager_from_name(row.order_folder_name or "")
            if mapped_manager and mapped_manager != "Неизвестно" and (row.manager_name or "") != mapped_manager:
                row.manager_name = mapped_manager

    prepared = []
    for row in rows:
        if not _can_view_order(row):
            continue
        item = {
            "id": row.id,
            "order_folder_name": row.order_folder_name,
            "manager_name": row.manager_name,
            "status": row.status,
            "folder_path": row.folder_path,
            "month_folder": row.month_folder,
            "month_key": row.month_key,
            "year": row.year,
            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
            "created_at": row.created_at.isoformat() if row.created_at else "",
            "reviewed_at": "",
            "confirmed_at": "",
            "can_transition": _can_mutate_order(row),
        }
        if q and q not in (row.order_folder_name or "").lower() and q not in (row.month_folder or "").lower():
            continue
        prepared.append(item)

    with SessionLocal.begin() as session_db:
        action_rows = session_db.execute(
            select(DeseneCpuAction).where(DeseneCpuAction.order_id.in_([item["id"] for item in prepared]))
        ).scalars().all() if prepared else []

    action_map = {}
    for action in action_rows:
        state = action_map.setdefault(action.order_id, {"reviewed_at": "", "confirmed_at": ""})
        if action.action_type == "SEND_TO_REVIEW" and action.ts:
            ts = action.ts.isoformat()
            if not state["reviewed_at"] or ts < state["reviewed_at"]:
                state["reviewed_at"] = ts
        if action.action_type == "CONFIRM" and action.ts:
            ts = action.ts.isoformat()
            if not state["confirmed_at"] or ts < state["confirmed_at"]:
                state["confirmed_at"] = ts

    month_map = {}
    for item in prepared:
        key = item.get("month_key") or ""
        folder = item.get("month_folder") or ""
        if not key or not folder:
            continue
        month_map[key] = folder
    months = [{"key": key, "label": month_map[key]} for key in sorted(month_map.keys(), reverse=True)]
    default_month = current_month_key if current_month_key in month_map else (months[0]["key"] if months else "")

    filtered = []
    stats_filtered = []
    for item in prepared:
        action_state = action_map.get(item["id"], {})
        item["reviewed_at"] = action_state.get("reviewed_at") or ""
        item["confirmed_at"] = action_state.get("confirmed_at") or ""
        if month and (item.get("month_key") or "") != month:
            continue
        if status_filter != "ALL" and (item.get("status") or "") != status_filter:
            continue
        stats_filtered.append(item)
        if manager_filter and manager_filter != "Все" and (item.get("manager_name") or "") != manager_filter:
            continue
        filtered.append(item)

    # Держим стабильный набор менеджеров в статистике: конфиг + "Неизвестно".
    manager_stats_map: dict[str, int] = {
        name: 0 for name in [*app_config.MANAGER_NAMES, "Неизвестно"] if (name or "").strip()
    }
    for item in stats_filtered:
        manager_name = (item.get("manager_name") or "Неизвестно").strip() or "Неизвестно"
        manager_stats_map[manager_name] = manager_stats_map.get(manager_name, 0) + 1

    configured_names = [name for name in [*app_config.MANAGER_NAMES, "Неизвестно"] if (name or "").strip()]
    manager_stats = [{"manager_name": name, "count": manager_stats_map.get(name, 0)} for name in configured_names]

    extra_names = sorted(
        [name for name in manager_stats_map.keys() if name not in configured_names],
        key=lambda value: value.lower(),
    )
    manager_stats.extend({"manager_name": name, "count": manager_stats_map[name]} for name in extra_names)

    # Для менеджера показываем его заказы текущего месяца первыми, сохраняя общий список доступным.
    if current_role == "manager" and current_user:
        def manager_priority(item: dict) -> int:
            is_own = (item.get("manager_name") or "") == current_user
            is_current_month = (item.get("month_key") or "") == current_month_key
            return 0 if is_own and is_current_month else 1

        # Стабильная сортировка: сначала общая свежесть, затем приоритет менеджера.
        filtered.sort(key=lambda x: (x.get("created_at") or x.get("updated_at") or "", x.get("id") or 0), reverse=True)
        filtered.sort(key=manager_priority)
    else:
        # Базовая сортировка для технологов и остальных ролей.
        filtered.sort(key=lambda x: (x.get("created_at") or x.get("updated_at") or "", x.get("id") or 0), reverse=True)

    return jsonify({
        "status": "ok",
        "orders": filtered,
        "months": months,
        "default_month": default_month,
        "manager_stats": manager_stats,
    })


@desene_cpu_bp.route("/api/desene_cpu/pending_new_count")
def api_pending_new_count():
    if not getattr(g, "role_perms", {}).get("can_access_desene_cpu"):
        return jsonify({"status": "error", "message": "Нет прав"}), 403

    role = (session.get("role") or "").strip().lower()
    user = (session.get("user") or "").strip()
    # Для менеджеров считаем только их новые заказы в активном списке (NEW).
    if role != "manager" or not user:
        return jsonify({"status": "ok", "count": 0})

    with SessionLocal.begin() as session_db:
        stmt = (
            select(DeseneCpuOrder)
            .where(DeseneCpuOrder.pdf_found == 1)
            .where(DeseneCpuOrder.status == CPU_STATUS_NEW)
        )
        rows = session_db.execute(stmt).scalars().all()

    user_norm = user.strip().casefold()
    count = 0
    for row in rows:
        # Считаем менеджера так же, как в общем списке CPU: через актуальную привязку клиента.
        mapped_manager = get_manager_from_name(row.order_folder_name or "")
        effective_manager = mapped_manager if mapped_manager and mapped_manager != "Неизвестно" else (row.manager_name or "")
        if (effective_manager or "").strip().casefold() == user_norm:
            count += 1

    return jsonify({"status": "ok", "count": count})


@desene_cpu_bp.route("/api/desene_cpu/orders/<int:order_id>/send", methods=["POST"])
def api_send(order_id: int):
    if not getattr(g, "role_perms", {}).get("can_access_desene_cpu"):
        return jsonify({"status": "error", "message": "Нет прав"}), 403
    user = session.get("user") or ""
    with SessionLocal.begin() as session_db:
        row = session_db.get(DeseneCpuOrder, order_id)
        if not row:
            return jsonify({"status": "error", "message": "Заказ не найден"}), 404
        if not _can_mutate_order(row):
            return jsonify({"status": "error", "message": "Нет прав"}), 403

        if row.status == CPU_STATUS_NEW:
            row.status = CPU_STATUS_IN_REVIEW
            log_action(row.id, "SEND_TO_REVIEW", user, json.dumps({"from": CPU_STATUS_NEW, "to": CPU_STATUS_IN_REVIEW}, ensure_ascii=False))
        else:
            log_action(row.id, "SEND_TO_REVIEW", user)

        folder_path = row.folder_path

    return jsonify({"status": "ok", "folder_path": folder_path, "new_status": CPU_STATUS_IN_REVIEW})


@desene_cpu_bp.route("/api/desene_cpu/orders/<int:order_id>/confirm", methods=["POST"])
def api_confirm(order_id: int):
    if not getattr(g, "role_perms", {}).get("can_access_desene_cpu"):
        return jsonify({"status": "error", "message": "Нет прав"}), 403
    user = session.get("user") or ""
    with SessionLocal.begin() as session_db:
        row = session_db.get(DeseneCpuOrder, order_id)
        if not row:
            return jsonify({"status": "error", "message": "Заказ не найден"}), 404
        if not _can_mutate_order(row):
            return jsonify({"status": "error", "message": "Нет прав"}), 403
        old = row.status
        row.status = CPU_STATUS_CONFIRMED
        log_action(row.id, "CONFIRM", user, json.dumps({"from": old, "to": CPU_STATUS_CONFIRMED}, ensure_ascii=False))

        try:
            _rename_order_folder_on_confirm(row, user)
        except FileExistsError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 409
        except OSError as exc:
            return jsonify({"status": "error", "message": f"Не удалось переименовать папку: {exc}"}), 500

    return jsonify({"status": "ok", "new_status": CPU_STATUS_CONFIRMED})


@desene_cpu_bp.route("/api/desene_cpu/orders/<int:order_id>/set_manager", methods=["POST"])
def api_set_manager(order_id: int):
    if not getattr(g, "role_perms", {}).get("can_access_desene_cpu"):
        return jsonify({"status": "error", "message": "Нет прав"}), 403
    payload = request.get_json(silent=True) or {}
    manager_name = (payload.get("manager_name") or "").strip()
    if not manager_name:
        return jsonify({"status": "error", "message": "Менеджер не указан"}), 400
    user = session.get("user") or ""

    with SessionLocal.begin() as session_db:
        row = session_db.get(DeseneCpuOrder, order_id)
        if not row:
            return jsonify({"status": "error", "message": "Заказ не найден"}), 404

        role = session.get("role") or ""
        if role not in {"admin", "technologist"}:
            return jsonify({"status": "error", "message": "Нет прав"}), 403

        old = row.manager_name
        row.manager_name = manager_name
        log_action(row.id, "MANAGER_CHANGED", user, json.dumps({"from": old, "to": manager_name}, ensure_ascii=False))

    return jsonify({"status": "ok"})


@desene_cpu_bp.route("/api/desene_cpu/orders/<int:order_id>/actions")
def api_actions(order_id: int):
    if not getattr(g, "role_perms", {}).get("can_access_desene_cpu"):
        return jsonify({"status": "error", "message": "Нет прав"}), 403
    with SessionLocal.begin() as session_db:
        row = session_db.get(DeseneCpuOrder, order_id)
        if not row:
            return jsonify({"status": "error", "message": "Заказ не найден"}), 404
        if not _can_view_order(row):
            return jsonify({"status": "error", "message": "Нет прав"}), 403

        actions = session_db.execute(
            select(DeseneCpuAction).where(DeseneCpuAction.order_id == order_id).order_by(DeseneCpuAction.ts.desc()).limit(100)
        ).scalars().all()

    return jsonify({
        "status": "ok",
        "actions": [
            {
                "action_type": a.action_type,
                "actor_user": a.actor_user,
                "ts": a.ts.isoformat() if a.ts else "",
                "meta_json": a.meta_json,
            }
            for a in actions
        ],
        "managers": app_config.MANAGER_NAMES,
    })
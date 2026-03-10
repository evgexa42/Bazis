from __future__ import annotations

import json

from flask import Blueprint, g, jsonify, render_template, request, session
from sqlalchemy import select

import app.config as app_config
from app.dal.db import SessionLocal
from app.dal.desene_cpu import (
    CPU_ARCHIVE_STATUSES,
    CPU_STATUS_CONFIRMED,
    CPU_STATUS_IN_REVIEW,
    CPU_STATUS_NEW,
    DeseneCpuAction,
    DeseneCpuOrder,
    log_action,
)


desene_cpu_bp = Blueprint("desene_cpu", __name__)


def _can_access_order(order: DeseneCpuOrder) -> bool:
    role = session.get("role") or ""
    user = session.get("user") or ""
    if role in {"admin", "technologist"}:
        return True
    if role == "manager":
        return (order.manager_name or "") == user
    return False


@desene_cpu_bp.route("/desene_cpu")
def desene_cpu_page():
    return render_template("desene_cpu.html")


@desene_cpu_bp.route("/api/desene_cpu/orders")
def api_orders():
    tab = (request.args.get("tab") or "active").strip().lower()
    q = (request.args.get("q") or "").strip().lower()

    with SessionLocal.begin() as session_db:
        stmt = select(DeseneCpuOrder).where(DeseneCpuOrder.pdf_found == 1)
        if tab == "archive":
            stmt = stmt.where(DeseneCpuOrder.status.in_(CPU_ARCHIVE_STATUSES))
        else:
            stmt = stmt.where(DeseneCpuOrder.status.in_([CPU_STATUS_NEW, CPU_STATUS_IN_REVIEW]))

        rows = session_db.execute(stmt).scalars().all()

    prepared = []
    for row in rows:
        if not _can_access_order(row):
            continue
        item = {
            "id": row.id,
            "order_folder_name": row.order_folder_name,
            "manager_name": row.manager_name,
            "status": row.status,
            "folder_path": row.folder_path,
            "month_folder": row.month_folder,
            "year": row.year,
            "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        }
        if q and q not in (row.order_folder_name or "").lower() and q not in (row.month_folder or "").lower():
            continue
        prepared.append(item)

    prepared.sort(key=lambda x: (x.get("year") or 0, x.get("month_folder") or "", x.get("order_folder_name") or ""), reverse=True)
    return jsonify({"status": "ok", "orders": prepared})


@desene_cpu_bp.route("/api/desene_cpu/orders/<int:order_id>/send", methods=["POST"])
def api_send(order_id: int):
    user = session.get("user") or ""
    with SessionLocal.begin() as session_db:
        row = session_db.get(DeseneCpuOrder, order_id)
        if not row:
            return jsonify({"status": "error", "message": "Заказ не найден"}), 404
        if not _can_access_order(row):
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
    user = session.get("user") or ""
    with SessionLocal.begin() as session_db:
        row = session_db.get(DeseneCpuOrder, order_id)
        if not row:
            return jsonify({"status": "error", "message": "Заказ не найден"}), 404
        if not _can_access_order(row):
            return jsonify({"status": "error", "message": "Нет прав"}), 403
        old = row.status
        row.status = CPU_STATUS_CONFIRMED
        log_action(row.id, "CONFIRM", user, json.dumps({"from": old, "to": CPU_STATUS_CONFIRMED}, ensure_ascii=False))

    return jsonify({"status": "ok", "new_status": CPU_STATUS_CONFIRMED})


@desene_cpu_bp.route("/api/desene_cpu/orders/<int:order_id>/set_manager", methods=["POST"])
def api_set_manager(order_id: int):
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
    with SessionLocal.begin() as session_db:
        row = session_db.get(DeseneCpuOrder, order_id)
        if not row:
            return jsonify({"status": "error", "message": "Заказ не найден"}), 404
        if not _can_access_order(row):
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

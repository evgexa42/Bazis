from __future__ import annotations

from flask import Blueprint, abort, jsonify, render_template, request, session

import app.config as app_config
from app.dal.cpu_orders import (
    get_cpu_order,
    list_cpu_archive_orders,
    list_cpu_ready_orders,
    update_cpu_manager,
    update_cpu_status,
)
from app.dal.order_manager_override import upsert_override
from app.services.cpu_monitor import sync_cpu_orders

cpu_bp = Blueprint("cpu", __name__)


@cpu_bp.route("/cpu", methods=["GET"])
def cpu_page():
    return render_template("cpu.html", cpu_view="active")


@cpu_bp.route("/cpu/archive", methods=["GET"])
def cpu_archive_page():
    return render_template("cpu.html", cpu_view="archive")


def _normalize_role() -> str:
    return (session.get("role") or "").strip().lower()


def _is_manager_of_order(order_manager: str) -> bool:
    username = (session.get("user") or "").strip().lower()
    manager_name = (order_manager or "").strip().lower()
    return bool(username and manager_name and username == manager_name)


def _can_view_order(order: dict) -> bool:
    role = _normalize_role()
    if role in {"admin", "technologist"}:
        return True
    if role == "manager":
        return _is_manager_of_order(order.get("manager_name") or order.get("manager"))
    return False


def _require_action_permission(order) -> None:
    role = _normalize_role()
    if role in {"admin", "technologist"}:
        return
    if role == "manager" and _is_manager_of_order(order.manager_name):
        return
    abort(403)


def _serialize(row) -> dict:
    return {
        "order_key": row.order_key,
        "folder_name": row.folder_name,
        "full_path": row.full_path,
        "year_month_path": row.year_month_path or "",
        "pdf_visible": bool(row.pdf_visible),
        "pdf_type_found": row.pdf_type_found or "",
        "pdf_filename": row.pdf_filename or "",
        "manager_name": row.manager_name or "Неизвестно",
        "status_cpu": row.status_cpu,
        "sent_at": row.sent_at.isoformat() if row.sent_at else "",
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        "missing_path": bool(row.missing_path),
    }


@cpu_bp.route("/api/cpu/orders", methods=["GET"])
def cpu_orders():
    if not app_config.CONFIG.get("features", {}).get("cpu_monitoring_enabled", False):
        return jsonify({"status": "ok", "orders": []})

    sync_cpu_orders()
    rows = list_cpu_ready_orders()
    visible = [_serialize(row) for row in rows if _can_view_order(_serialize(row))]
    return jsonify({"status": "ok", "orders": visible})


@cpu_bp.route("/api/cpu/archive", methods=["GET"])
def cpu_archive():
    query = request.args.get("query", "")
    status = request.args.get("status", "")
    manager = request.args.get("manager", "")

    sync_cpu_orders()
    rows = list_cpu_archive_orders(query=query, status=status, manager=manager)
    visible = [_serialize(row) for row in rows if _can_view_order(_serialize(row))]
    return jsonify({"status": "ok", "orders": visible})


@cpu_bp.route("/api/cpu/order/<order_key>/send", methods=["POST"])
def cpu_send(order_key: str):
    order = get_cpu_order(order_key)
    if not order:
        return jsonify({"status": "error", "message": "Заказ не найден."}), 404

    _require_action_permission(order)
    update_cpu_status(order_key, "review")

    refreshed = get_cpu_order(order_key)
    return jsonify({"status": "ok", "full_path": refreshed.full_path if refreshed else order.full_path})


@cpu_bp.route("/api/cpu/order/<order_key>/confirm", methods=["POST"])
def cpu_confirm(order_key: str):
    order = get_cpu_order(order_key)
    if not order:
        return jsonify({"status": "error", "message": "Заказ не найден."}), 404

    _require_action_permission(order)
    update_cpu_status(order_key, "confirmed")
    return jsonify({"status": "ok"})


@cpu_bp.route("/api/cpu/order/<order_key>/assign-manager", methods=["POST"])
def cpu_assign_manager(order_key: str):
    order = get_cpu_order(order_key)
    if not order:
        return jsonify({"status": "error", "message": "Заказ не найден."}), 404

    _require_action_permission(order)
    payload = request.get_json(silent=True) or {}
    manager_name = (
        payload.get("manager_name")
        or payload.get("manager_id")
        or payload.get("manager")
        or ""
    ).strip()

    if manager_name not in app_config.MANAGER_NAMES:
        return jsonify({"status": "error", "message": "Менеджер не найден."}), 400

    updated_by = session.get("user") or ""
    upsert_override(order_key, manager_name, updated_by)
    update_cpu_manager(order_key, manager_name)
    return jsonify({"status": "ok"})


__all__ = ["cpu_bp"]

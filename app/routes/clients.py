from flask import Blueprint, abort, jsonify, redirect, render_template, request, session, url_for

from app import clients, clients_lock, reload_clients_from_db
from app.dal.database import add_client as add_client_db
from app.dal.database import delete_client as delete_client_db
from app.dal.database import update_client as update_client_db
from app.dal.permissions import has_permission

clients_bp = Blueprint("clients", __name__)


@clients_bp.route("/clients")
def clients_page():
    if not has_permission(session.get("role"), "can_access_clients"):
        return redirect(url_for("orders.index"))

    with clients_lock:
        clients_snapshot = dict(clients)
    return render_template("clients.html", clients=clients_snapshot)


@clients_bp.route("/add_client", methods=["POST"])
def add_client():
    if not has_permission(session.get("role"), "can_access_clients"):
        return abort(403)

    client_name = (request.form.get("client") or "").strip()
    manager = request.form.get("manager")

    if client_name:
        with clients_lock:
            add_client_db(client_name, manager)
            reload_clients_from_db()
    return redirect(url_for("clients.clients_page"))


@clients_bp.route("/update_client", methods=["POST"])
def update_client():
    if not has_permission(session.get("role"), "can_access_clients"):
        return abort(403)

    data = request.get_json()
    old_name = data.get("old_name")
    new_name = data.get("new_name")
    new_manager = data.get("new_manager")

    with clients_lock:
        if update_client_db(old_name, new_name, new_manager):
            reload_clients_from_db()

    return jsonify({"status": "ok"})


@clients_bp.route("/delete_client", methods=["POST"])
def delete_client():
    if not has_permission(session.get("role"), "can_access_clients"):
        return abort(403)

    data = request.get_json()
    name = data.get("name")

    with clients_lock:
        if delete_client_db(name):
            reload_clients_from_db()

    return jsonify({"status": "ok"})
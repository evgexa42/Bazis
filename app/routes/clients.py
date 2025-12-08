from flask import Blueprint, g, jsonify, redirect, render_template, request, url_for

from app import clients, clients_lock, reload_clients_from_db
from app.dal.database import add_client as add_client_db
from app.dal.database import delete_client as delete_client_db
from app.dal.database import update_client as update_client_db
from app.dal.permissions import permissions_required

clients_bp = Blueprint("clients", __name__)


@clients_bp.route("/clients")
def clients_page():
    if not getattr(g, "role_perms", {}).get("can_access_clients"):
        return redirect(url_for("orders.index"))

    with clients_lock:
        clients_snapshot = dict(clients)
    return render_template("clients.html", clients=clients_snapshot)


@clients_bp.route("/add_client", methods=["POST"])
@permissions_required("can_access_clients")
def add_client():

    client_name = (request.form.get("client") or "").strip()
    manager = request.form.get("manager")

    if client_name:
        with clients_lock:
            add_client_db(client_name, manager)
            reload_clients_from_db()
    return redirect(url_for("clients.clients_page"))


@clients_bp.route("/update_client", methods=["POST"])
@permissions_required("can_access_clients")
def update_client():

    data = request.get_json()
    old_name = data.get("old_name")
    new_name = data.get("new_name")
    # поддерживаем старый ключ manager на случай старого JS
    new_manager = data.get("new_manager") or data.get("manager") or ""

    updated = False
    with clients_lock:
        updated = update_client_db(old_name, new_name, new_manager)
        if updated:
            reload_clients_from_db()
    if not updated:
        return jsonify({"status": "error", "message": "Клиент не найден."}), 404

    return jsonify({"status": "ok"})


@clients_bp.route("/delete_client", methods=["POST"])
@permissions_required("can_access_clients")
def delete_client():

    data = request.get_json()
    name = data.get("name")

    deleted = False
    with clients_lock:
        deleted = delete_client_db(name)
        if deleted:
            reload_clients_from_db()

    if not deleted:
        return jsonify({"status": "error", "message": "Клиент не найден."}), 404

    return jsonify({"status": "ok"})
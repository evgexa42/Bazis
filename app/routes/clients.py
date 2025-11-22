from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from app import clients, clients_lock, refresh_clients_lookup_locked
from app import save_clients

clients_bp = Blueprint("clients", __name__)


@clients_bp.route("/clients")
def clients_page():
    with clients_lock:
        clients_snapshot = dict(clients)
    return render_template("clients.html", clients=clients_snapshot)


@clients_bp.route("/add_client", methods=["POST"])
def add_client():
    client_name = (request.form.get("client") or "").strip()
    manager = request.form.get("manager")

    if client_name:
        with clients_lock:
            clients[client_name] = manager
            refresh_clients_lookup_locked()
            save_clients(clients)
    return redirect(url_for("clients.clients_page"))


@clients_bp.route("/update_client", methods=["POST"])
def update_client():
    data = request.get_json()
    old_name = data.get("old_name")
    new_name = data.get("new_name")
    new_manager = data.get("new_manager")

    with clients_lock:
        if old_name in clients:
            clients.pop(old_name)
            clients[new_name] = new_manager
            refresh_clients_lookup_locked()
            save_clients(clients)

    return jsonify({"status": "ok"})


@clients_bp.route("/delete_client", methods=["POST"])
def delete_client():
    data = request.get_json()
    name = data.get("name")

    with clients_lock:
        if name in clients:
            clients.pop(name)
            refresh_clients_lookup_locked()
            save_clients(clients)

    return jsonify({"status": "ok"})
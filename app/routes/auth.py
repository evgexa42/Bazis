from functools import wraps
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

from flask import (
    Blueprint,
    abort,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
import logging

from app.dal.permissions import get_role_permissions
from app.dal.users import verify_user_credentials

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger("bazis")

PUBLIC_ENDPOINTS = {"auth.login", "static", "orders.ping", "setup.setup_page"}


def login_required(view: Callable):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def roles_required(roles: Iterable[str]):
    def decorator(view: Callable):
        @wraps(view)
        def wrapper(*args, **kwargs):
            current_role = session.get("role")
            if not current_role:
                return redirect(url_for("auth.login", next=request.path))
            if current_role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


@auth_bp.before_app_request
def load_current_user():
    g.current_user = session.get("user")
    g.current_role = session.get("role")


@auth_bp.before_app_request
def load_role_permissions():
    g.role_perms = get_role_permissions(session.get("role"))


@auth_bp.before_app_request
def enforce_login():
    endpoint = request.endpoint or ""

    if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("static"):
        return

    if not session.get("user"):
        next_url = request.path
        if request.query_string:
            try:
                query = request.query_string.decode("utf-8", errors="ignore")
                if query:
                    next_url = f"{next_url}?{query}"
            except Exception:
                next_url = request.path
        return redirect(url_for("auth.login", next=next_url))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error: Optional[str] = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = verify_user_credentials(username, password)
        if user:
            session.permanent = True
            session["user"] = user["username"]
            session["role"] = user["role"]
            logger.info("[auth] Успешный вход пользователя: %s", username)
            next_url = request.args.get("next") or url_for("orders.index")
            parsed = urlparse(next_url)
            if parsed.scheme or parsed.netloc:
                next_url = url_for("orders.index")
            elif not parsed.path.startswith("/"):
                next_url = url_for("orders.index")
            else:
                path = parsed.path or url_for("orders.index")
                if parsed.query:
                    path = f"{path}?{parsed.query}"
                next_url = path
            return redirect(next_url)
        logger.warning("[auth] Неуспешная попытка входа для пользователя: %s", username)
        error = "Неверное имя пользователя или пароль."

    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    logger.info("[auth] Пользователь вышел: %s", session.get("user"))
    session.clear()
    return redirect(url_for("auth.login"))


__all__ = ["auth_bp", "login_required", "roles_required"]
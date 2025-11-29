from functools import wraps
from typing import Callable, Iterable, Optional

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

from app.dal.users import verify_user_credentials

auth_bp = Blueprint("auth", __name__)

PUBLIC_ENDPOINTS = {"auth.login", "static", "orders.ping"}


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
def enforce_login():
    endpoint = request.endpoint or ""

    if endpoint in PUBLIC_ENDPOINTS or endpoint.startswith("static"):
        return

    if not session.get("user"):
        next_url = request.full_path if request.query_string else request.path
        next_url = next_url.rstrip("?")
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
            next_url = request.args.get("next") or url_for("orders.index")
            return redirect(next_url)
        error = "Неверное имя пользователя или пароль."

    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


__all__ = ["auth_bp", "login_required", "roles_required"]
import hashlib
import secrets
import string
from typing import Dict, List, Optional, Set

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app.dal.db import DEFAULT_ROLE_PERMISSIONS, SessionLocal, User
from app.dal.permissions import list_roles

DEFAULT_ROLES = set(DEFAULT_ROLE_PERMISSIONS.keys())


def _user_to_dict(user: User) -> Dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": bool(user.is_active),
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def _count_admins(session) -> int:
    return session.execute(
        select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))
    ).scalar_one()


def _error(code: str, message: str) -> Dict[str, str]:
    return {"ok": False, "error_code": code, "message": message}


def _allowed_roles() -> Set[str]:
    try:
        names = set(list_roles())
        if not names:
            return set(DEFAULT_ROLES)
        return names
    except Exception:
        # Не ломаем поток из-за проблем с таблицей ролей
        return set(DEFAULT_ROLES)


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def get_all_users() -> List[Dict]:
    with SessionLocal.begin() as session:
        rows = session.execute(select(User).order_by(User.id.asc())).scalars().all()
        return [_user_to_dict(row) for row in rows]


def get_allowed_roles() -> Set[str]:
    return _allowed_roles()


def get_user_by_username(username: str) -> Optional[Dict]:
    if not username:
        return None

    with SessionLocal.begin() as session:
        user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
        return _user_to_dict(user) | {"password_hash": user.password_hash} if user else None


def create_user(username: str, password: str, role: str) -> Dict[str, str]:
    username = (username or "").strip()
    role = (role or "").strip().lower()
    if not username or not password:
        return _error("validation", "Имя пользователя и пароль обязательны.")

    if role not in _allowed_roles():
        return _error("invalid_role", "Недопустимая роль.")

    password_hash = generate_password_hash(password)

    try:
        with SessionLocal.begin() as session:
            session.add(User(username=username, password_hash=password_hash, role=role, is_active=True))
        return {"ok": True}
    except IntegrityError:
        return _error("username_taken", "Пользователь с таким именем уже существует.")


def update_user_role(user_id: int, role: str) -> Dict[str, str]:
    role = (role or "").strip().lower()

    if role not in _allowed_roles():
        return _error("invalid_role", "Недопустимая роль.")

    with SessionLocal.begin() as session:
        user = session.get(User, user_id)
        if not user:
            return _error("not_found", "Пользователь не найден.")

        if user.role == "admin" and role != "admin":
            admin_count = _count_admins(session)
            if admin_count <= 1:
                return _error("admin_guard", "Нельзя изменить роль последнего администратора.")

        user.role = role
        return {"ok": True}


def update_user(user_id: int, username: str, role: str, is_active: bool) -> Dict[str, str]:
    username = (username or "").strip()
    role = (role or "").strip().lower()
    is_active = _bool(is_active)

    if not username:
        return _error("validation", "Имя пользователя не может быть пустым.")
    if role not in _allowed_roles():
        return _error("invalid_role", "Недопустимая роль.")

    try:
        with SessionLocal.begin() as session:
            user = session.get(User, user_id)
            if not user:
                return _error("not_found", "Пользователь не найден.")

            if user.role == "admin" and (role != "admin" or not is_active):
                admin_count = _count_admins(session)
                if admin_count <= 1:
                    return _error(
                        "admin_guard", "Нельзя изменить последнего активного администратора."
                    )

            user.username = username
            user.role = role
            user.is_active = is_active

            session.add(user)
        return {"ok": True}
    except IntegrityError:
        return _error("username_taken", "Пользователь с таким именем уже существует.")


def delete_user(user_id: int) -> Dict[str, str]:
    with SessionLocal.begin() as session:
        user = session.get(User, user_id)
        if not user:
            return _error("not_found", "Пользователь не найден.")

        if user.role == "admin" and user.is_active:
            admin_count = _count_admins(session)
            if admin_count <= 1:
                return _error(
                    "admin_guard", "Нельзя удалить последнего активного администратора."
                )

        session.delete(user)
        return {"ok": True}


def generate_random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    length = secrets.randbelow(5) + 8  # 8..12
    return "".join(secrets.choice(alphabet) for _ in range(length))


def reset_user_password_random(user_id: int, current_username: str) -> Dict[str, str]:
    new_password = generate_random_password()

    with SessionLocal.begin() as session:
        user = session.get(User, user_id)
        if not user:
            return {"ok": False, "error": "Пользователь не найден."}

        user.password_hash = generate_password_hash(new_password)
        session.add(user)

    return {"ok": True, "password": new_password}


def reset_user_password(user_id: int, new_password: str, current_username: str) -> Dict[str, str]:
    if not new_password or not new_password.strip():
        return {"ok": False, "error": "Пароль не может быть пустым."}

    with SessionLocal.begin() as session:
        user = session.get(User, user_id)
        if not user:
            return {"ok": False, "error": "Пользователь не найден."}

        user.password_hash = generate_password_hash(new_password.strip())
        return {"ok": True}


def verify_user_credentials(username: str, password: str) -> Optional[Dict]:
    if not username or not password:
        return None

    with SessionLocal.begin() as session:
        user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if not user or not user.is_active:
            return None

        if not check_password_hash(user.password_hash, password):
            # Backward compatibility: previously stored raw sha256 hashes
            is_legacy_hash = (
                len(user.password_hash) == 64
                and all(ch in "0123456789abcdef" for ch in user.password_hash.lower())
            )
            if not is_legacy_hash:
                return None

            legacy_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
            if legacy_hash != user.password_hash:
                return None

            # Upgrade legacy hash to werkzeug-compatible hash
            user.password_hash = generate_password_hash(password)
            session.add(user)

        return _user_to_dict(user)


__all__ = [
    "create_user",
    "delete_user",
    "generate_random_password",
    "get_allowed_roles",
    "get_all_users",
    "get_user_by_username",
    "reset_user_password_random",
    "reset_user_password",
    "update_user",
    "update_user_role",
    "verify_user_credentials",
]
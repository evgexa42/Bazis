import sqlite3
from typing import Dict, List, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from app.dal.db import get_db_connection

ALLOWED_ROLES = {"admin", "technologist", "manager"}


def _count_admins(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
    )
    return cursor.fetchone()[0]


def get_all_users() -> List[Dict]:
    with get_db_connection() as connection:
        cursor = connection.execute(
            """
            SELECT id, username, role, is_active, created_at, updated_at
            FROM users
            ORDER BY id ASC
            """
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


def get_user_by_username(username: str) -> Optional[Dict]:
    if not username:
        return None

    with get_db_connection() as connection:
        cursor = connection.execute(
            """
            SELECT id, username, password_hash, role, is_active, created_at, updated_at
            FROM users
            WHERE username = ?
            """,
            (username,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def create_user(username: str, password: str, role: str) -> Dict[str, str]:
    username = (username or "").strip()
    role = (role or "").strip()
    if not username or not password:
        return {"ok": False, "error": "Имя пользователя и пароль обязательны."}
    if role not in ALLOWED_ROLES:
        return {"ok": False, "error": "Недопустимая роль."}

    password_hash = generate_password_hash(password)

    with get_db_connection() as connection:
        try:
            connection.execute(
                """
                INSERT INTO users (username, password_hash, role, is_active)
                VALUES (?, ?, ?, 1)
                """,
                (username, password_hash, role),
            )
            connection.commit()
            return {"ok": True}
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "Пользователь с таким именем уже существует."}


def update_user_role(user_id: int, role: str) -> Dict[str, str]:
    if role not in ALLOWED_ROLES:
        return {"ok": False, "error": "Недопустимая роль."}

    with get_db_connection() as connection:
        cursor = connection.execute(
            "SELECT role FROM users WHERE id = ?", (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "error": "Пользователь не найден."}

        current_role = row[0]
        if current_role == "admin" and role != "admin":
            admin_count = _count_admins(connection)
            if admin_count <= 1:
                return {"ok": False, "error": "Нельзя изменить роль последнего администратора."}

        connection.execute(
            """
            UPDATE users
            SET role = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (role, user_id),
        )
        connection.commit()
        return {"ok": True}


def reset_user_password(user_id: int, new_password: str, current_username: str) -> Dict[str, str]:
    if not new_password:
        return {"ok": False, "error": "Пароль не может быть пустым."}

    with get_db_connection() as connection:
        cursor = connection.execute(
            "SELECT username FROM users WHERE id = ?", (user_id,)
        )
        row = cursor.fetchone()
        if not row:
            return {"ok": False, "error": "Пользователь не найден."}

        target_username = row[0]
        if target_username == current_username:
            return {
                "ok": False,
                "error": "Нельзя сбросить пароль своей учетной записи без подтверждения.",
            }

        password_hash = generate_password_hash(new_password)
        connection.execute(
            """
            UPDATE users
            SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (password_hash, user_id),
        )
        connection.commit()
        return {"ok": True}


def verify_user_credentials(username: str, password: str) -> Optional[Dict]:
    user = get_user_by_username(username)
    if not user or not user.get("is_active"):
        return None

    if not check_password_hash(user.get("password_hash", ""), password):
        return None

    return user


__all__ = [
    "ALLOWED_ROLES",
    "create_user",
    "get_all_users",
    "get_user_by_username",
    "reset_user_password",
    "update_user_role",
    "verify_user_credentials",
]
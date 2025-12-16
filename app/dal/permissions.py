from functools import wraps
from typing import Dict, Iterable, List

from flask import abort, g, session

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.dal.db import (
    DEFAULT_ROLE_PERMISSIONS,
    RolePermission,
    SessionLocal,
    User,
)

PERMISSION_FIELDS: Iterable[str] = (
    "can_access_settings",
    "can_access_facades",
    "can_access_metrics",
    "can_access_clients",
    "can_access_search",
    "can_manage_users",
    "can_edit_paths",
    "can_toggle_order_options",
    "can_confirm_orders",
    "can_view_priced",
    "can_mark_priced",
    "can_view_priced_panel",
)

_EMPTY_PERMISSIONS: Dict[str, int] = {field: 0 for field in PERMISSION_FIELDS}


def _error(code: str, message: str) -> Dict[str, str]:
    return {"ok": False, "error_code": code, "message": message}


def _enforce_admin_baseline(role: str, perms: Dict[str, int]) -> Dict[str, int]:
    # Критичные права администратора защищаем от выключения
    if role != "admin":
        return perms

    ensured = dict(perms)
    ensured["can_access_settings"] = 1
    ensured["can_manage_users"] = 1
    ensured["can_confirm_orders"] = 1
    ensured["can_view_priced"] = 1
    ensured["can_view_priced_panel"] = 1
    return ensured


def _normalize_permissions(source: Dict[str, int]) -> Dict[str, int]:
    normalized = dict(_EMPTY_PERMISSIONS)
    for field in PERMISSION_FIELDS:
        normalized[field] = int(source.get(field, 0))
    return normalized


def get_role_permissions(role: str) -> Dict[str, int]:
    if not role:
        return dict(_EMPTY_PERMISSIONS)

    with SessionLocal.begin() as session:
        record = session.execute(
            select(RolePermission).where(RolePermission.role == role)
        ).scalar_one_or_none()

        if not record:
            return dict(_EMPTY_PERMISSIONS)

        return {field: int(getattr(record, field) or 0) for field in PERMISSION_FIELDS}


def get_all_role_permissions() -> Dict[str, Dict[str, int]]:
    permissions: Dict[str, Dict[str, int]] = {}
    with SessionLocal.begin() as session:
        rows = session.execute(select(RolePermission)).scalars().all()
        for row in rows:
            permissions[row.role] = {field: int(getattr(row, field) or 0) for field in PERMISSION_FIELDS}

    # Гарантируем наличие записей для дефолтных ролей даже если таблица очищена
    for role, defaults in DEFAULT_ROLE_PERMISSIONS.items():
        permissions.setdefault(role, _normalize_permissions(defaults))

    return permissions


def save_role_permissions(updates: Dict[str, Dict[str, int]]) -> None:
    normalized_updates = {
        role: _normalize_permissions(perms) for role, perms in updates.items() if role
    }
    with SessionLocal.begin() as session:
        for role, perms in normalized_updates.items():
            enforced = _enforce_admin_baseline(role, perms)
            record = session.get(RolePermission, role)
            if not record:
                record = RolePermission(role=role)
                session.add(record)
            for field, value in enforced.items():
                setattr(record, field, value)


def create_role(name: str, base_permissions: Dict[str, int]) -> Dict[str, str]:
    role = (name or "").strip().lower()
    if not role:
        return _error("invalid_role", "Название роли не может быть пустым.")
    if role in DEFAULT_ROLE_PERMISSIONS:
        return _error("protected_role", "Системные роли нельзя создавать повторно.")

    normalized = _normalize_permissions(base_permissions)

    try:
        with SessionLocal.begin() as session:
            record = session.get(RolePermission, role)
            if record:
                return _error("role_exists", "Роль с таким именем уже есть.")
            session.add(RolePermission(role=role, **normalized))
        return {"ok": True, "role": role}
    except IntegrityError:
        return _error("role_exists", "Роль с таким именем уже есть.")


def update_role(name: str, permissions: Dict[str, int]) -> Dict[str, str]:
    role = (name or "").strip().lower()
    if not role:
        return _error("invalid_role", "Название роли не может быть пустым.")

    normalized = _enforce_admin_baseline(role, _normalize_permissions(permissions))

    with SessionLocal.begin() as session:
        record = session.get(RolePermission, role)
        if not record:
            return _error("not_found", "Роль не найдена.")

        for field, value in normalized.items():
            setattr(record, field, value)
    return {"ok": True, "role": role}


def delete_role(name: str) -> Dict[str, str]:
    role = (name or "").strip().lower()
    if not role:
        return _error("invalid_role", "Название роли не может быть пустым.")
    if role in DEFAULT_ROLE_PERMISSIONS:
        return _error("protected_role", "Системные роли нельзя удалить.")

    with SessionLocal.begin() as session:
        is_used = session.execute(
            select(User).where(User.role == role).limit(1)
        ).first() is not None
        if is_used:
            return _error("role_in_use", "Роль назначена пользователям и не может быть удалена.")

        record = session.get(RolePermission, role)
        if not record:
            return _error("not_found", "Роль не найдена.")

        session.delete(record)
        return {"ok": True}


def list_roles() -> List[str]:
    permissions = get_all_role_permissions()
    return sorted(permissions.keys())


def has_permission(role: str, perm: str) -> bool:
    cached = getattr(g, "role_perms", None)
    if cached is not None:
        return bool(cached.get(perm, 0))

    perms = get_role_permissions(role)
    return bool(perms.get(perm, 0))


def permissions_required(*perms: str):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            role = session.get("role")
            cached = getattr(g, "role_perms", None)
            for perm in perms:
                if cached is not None:
                    allowed = bool(cached.get(perm, 0))
                else:
                    allowed = has_permission(role, perm)
                if not allowed:
                    abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


__all__ = [
    "get_role_permissions",
    "get_all_role_permissions",
    "save_role_permissions",
    "create_role",
    "update_role",
    "delete_role",
    "list_roles",
    "has_permission",
    "permissions_required",
    "PERMISSION_FIELDS",
]
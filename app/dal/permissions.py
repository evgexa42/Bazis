from typing import Dict, Iterable

from sqlalchemy import select

from app.dal.db import DEFAULT_ROLE_PERMISSIONS, RolePermission, SessionLocal

PERMISSION_FIELDS: Iterable[str] = (
    "can_access_settings",
    "can_access_facades",
    "can_access_metrics",
    "can_access_clients",
    "can_access_search",
    "can_manage_users",
    "can_edit_paths",
    "can_toggle_order_options",
)

_EMPTY_PERMISSIONS: Dict[str, int] = {field: 0 for field in PERMISSION_FIELDS}


def _normalize_permissions(source: Dict[str, int]) -> Dict[str, int]:
    normalized = dict(_EMPTY_PERMISSIONS)
    for field in PERMISSION_FIELDS:
        normalized[field] = int(source.get(field, 0))
    return normalized


def get_role_permissions(role: str) -> Dict[str, int]:
    if not role:
        return dict(_EMPTY_PERMISSIONS)

    with SessionLocal() as session:
        record = session.execute(
            select(RolePermission).where(RolePermission.role == role)
        ).scalar_one_or_none()

        if not record:
            return dict(_EMPTY_PERMISSIONS)

        return {field: int(getattr(record, field) or 0) for field in PERMISSION_FIELDS}


def get_all_role_permissions() -> Dict[str, Dict[str, int]]:
    permissions: Dict[str, Dict[str, int]] = {}
    with SessionLocal() as session:
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
            record = session.get(RolePermission, role)
            if not record:
                record = RolePermission(role=role)
                session.add(record)
            for field, value in perms.items():
                setattr(record, field, value)


def has_permission(role: str, perm: str) -> bool:
    perms = get_role_permissions(role)
    return bool(perms.get(perm, 0))


__all__ = [
    "get_role_permissions",
    "get_all_role_permissions",
    "save_role_permissions",
    "has_permission",
    "PERMISSION_FIELDS",
]
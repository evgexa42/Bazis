from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, func, select

from app.dal.db import Base, SessionLocal


CPU_STATUS_NEW = "NEW"
CPU_STATUS_IN_REVIEW = "IN_REVIEW"
CPU_STATUS_CONFIRMED = "CONFIRMED"
CPU_ARCHIVE_STATUSES = {CPU_STATUS_CONFIRMED}


class DeseneCpuOrder(Base):
    __tablename__ = "desene_cpu_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    normalized_path = Column(String, unique=True, nullable=False, index=True)

    year = Column(Integer, nullable=False, default=0)
    month_folder = Column(String, nullable=False, default="")
    month_key = Column(String, nullable=False, default="")

    order_folder_name = Column(String, nullable=False, default="")
    order_code = Column(String, nullable=False, default="")

    manager_name = Column(String, nullable=False, default="Неизвестно")
    status = Column(String, nullable=False, default=CPU_STATUS_NEW)

    folder_path = Column(String, nullable=False, default="")
    pdf_found = Column(Boolean, nullable=False, default=False)
    pdf_path = Column(String, nullable=False, default="")
    pdf_mtime = Column(DateTime(timezone=True), nullable=True)

    folder_last_mtime_seen = Column(DateTime(timezone=True), nullable=True)
    last_seen_ts = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    last_pdf_check_ts = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at = Column(DateTime(timezone=True), onupdate=func.current_timestamp())


class DeseneCpuAction(Base):
    __tablename__ = "desene_cpu_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("desene_cpu_orders.id"), nullable=False, index=True)
    action_type = Column(String, nullable=False)
    actor_user = Column(String, nullable=False, default="system")
    ts = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    meta_json = Column(Text, nullable=False, default="")


def normalize_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    # Нормализуем без изменения UNC-смысла, чтобы избежать дублей в БД.
    return os.path.normcase(os.path.normpath(raw))


def get_order(order_id: int) -> Optional[DeseneCpuOrder]:
    with SessionLocal.begin() as session:
        return session.get(DeseneCpuOrder, order_id)


def get_order_by_path(path: str) -> Optional[DeseneCpuOrder]:
    norm = normalize_path(path)
    if not norm:
        return None
    with SessionLocal.begin() as session:
        return session.execute(
            select(DeseneCpuOrder).where(DeseneCpuOrder.normalized_path == norm)
        ).scalar_one_or_none()


def log_action(order_id: int, action_type: str, actor_user: str, meta_json: str = "") -> None:
    with SessionLocal.begin() as session:
        session.add(
            DeseneCpuAction(
                order_id=order_id,
                action_type=action_type,
                actor_user=(actor_user or "system"),
                meta_json=meta_json or "",
            )
        )


def rebind_order_path_by_code(
    *,
    month_key: str,
    order_code: str,
    new_folder_path: str,
    new_folder_name: str,
) -> Optional[DeseneCpuOrder]:
    """Перепривязывает запись при rename/move папки, сохраняя статус и историю."""

    norm_new = normalize_path(new_folder_path)
    if not month_key or not order_code or not norm_new:
        return None

    with SessionLocal.begin() as session:
        # Если новая папка уже есть в БД — переиспользовать её, ничего не делаем.
        existing_new = session.execute(
            select(DeseneCpuOrder).where(DeseneCpuOrder.normalized_path == norm_new)
        ).scalar_one_or_none()
        if existing_new:
            return existing_new

        candidates = session.execute(
            select(DeseneCpuOrder)
            .where(DeseneCpuOrder.month_key == month_key)
            .where(DeseneCpuOrder.order_code == order_code)
            .where(DeseneCpuOrder.normalized_path != norm_new)
            .order_by(DeseneCpuOrder.updated_at.desc(), DeseneCpuOrder.id.desc())
        ).scalars().all()

        for row in candidates:
            old_path = (row.folder_path or "").strip()
            # Восстанавливаем связь только если старая папка реально исчезла.
            if old_path and os.path.exists(old_path):
                continue
            row.normalized_path = norm_new
            row.folder_path = new_folder_path
            row.order_folder_name = new_folder_name
            session.flush()
            return row

    return None


def upsert_order(payload: Dict[str, Any]) -> DeseneCpuOrder:
    norm_path = normalize_path(payload.get("folder_path") or payload.get("normalized_path") or "")
    if not norm_path:
        raise ValueError("normalized path is required")

    with SessionLocal.begin() as session:
        row = session.execute(
            select(DeseneCpuOrder).where(DeseneCpuOrder.normalized_path == norm_path)
        ).scalar_one_or_none()
        if row is None:
            row = DeseneCpuOrder(normalized_path=norm_path)
            session.add(row)

        for field in (
            "year",
            "month_folder",
            "month_key",
            "order_folder_name",
            "order_code",
            "manager_name",
            "status",
            "folder_path",
            "pdf_found",
            "pdf_path",
        ):
            if field in payload:
                setattr(row, field, payload.get(field))

        if "pdf_mtime" in payload:
            row.pdf_mtime = payload.get("pdf_mtime")
        if "folder_last_mtime_seen" in payload:
            row.folder_last_mtime_seen = payload.get("folder_last_mtime_seen")
        if "last_seen_ts" in payload:
            row.last_seen_ts = payload.get("last_seen_ts") or datetime.utcnow()
        if "last_pdf_check_ts" in payload:
            row.last_pdf_check_ts = payload.get("last_pdf_check_ts")

        session.flush()
        return row
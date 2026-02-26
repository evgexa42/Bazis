from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, String, Text, func, or_

from app.dal.db import Base, SessionLocal


class CpuOrder(Base):
    __tablename__ = "cpu_orders"

    order_key = Column(String, primary_key=True)
    folder_name = Column(String, nullable=False)
    full_path = Column(Text, nullable=False)
    year_month_path = Column(String, nullable=True)
    year = Column(String, nullable=True)
    month_folder = Column(String, nullable=True)

    pdf_visible = Column(Boolean, nullable=False, default=False)
    pdf_type_found = Column(String, nullable=True)
    pdf_filename = Column(String, nullable=True)

    manager_name = Column(String, nullable=True)
    status_cpu = Column(String, nullable=False, default="ready")

    sent_at = Column(DateTime(timezone=True), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    missing_path = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


def get_cpu_order(order_key: str) -> Optional[CpuOrder]:
    with SessionLocal.begin() as session:
        return session.get(CpuOrder, order_key)


def list_cpu_ready_orders() -> list[CpuOrder]:
    with SessionLocal.begin() as session:
        return (
            session.query(CpuOrder)
            .filter(
                CpuOrder.pdf_visible.is_(True),
                CpuOrder.status_cpu == "ready",
                CpuOrder.missing_path.is_(False),
            )
            .order_by(CpuOrder.updated_at.desc(), CpuOrder.created_at.desc())
            .all()
        )


def list_cpu_archive_orders(query: str = "", status: str = "", manager: str = "") -> list[CpuOrder]:
    normalized_query = (query or "").strip().lower()
    normalized_status = (status or "").strip().lower()
    normalized_manager = (manager or "").strip()

    with SessionLocal.begin() as session:
        db_query = session.query(CpuOrder).filter(CpuOrder.status_cpu.in_(("review", "confirmed")))

        if normalized_status in {"review", "confirmed"}:
            db_query = db_query.filter(CpuOrder.status_cpu == normalized_status)

        if normalized_manager:
            db_query = db_query.filter(CpuOrder.manager_name == normalized_manager)

        if normalized_query:
            like_expr = f"%{normalized_query}%"
            db_query = db_query.filter(
                or_(
                    func.lower(CpuOrder.folder_name).like(like_expr),
                    func.lower(CpuOrder.manager_name).like(like_expr),
                    func.lower(CpuOrder.pdf_filename).like(like_expr),
                )
            )

        return (
            db_query.order_by(
                CpuOrder.year.desc(),
                CpuOrder.month_folder.desc(),
                CpuOrder.updated_at.desc(),
                CpuOrder.created_at.desc(),
            ).all()
        )


def upsert_cpu_order(
    *,
    order_key: str,
    folder_name: str,
    full_path: str,
    year_month_path: str,
    pdf_visible: bool,
    pdf_type_found: str,
    pdf_filename: str,
    manager_name: str,
    year: int | None = None,
    month_folder: str = "",
    status_cpu: str = "ready",
    missing_path: bool = False,
) -> CpuOrder:
    now = datetime.utcnow()
    with SessionLocal.begin() as session:
        record = session.get(CpuOrder, order_key)
        if not record:
            record = CpuOrder(order_key=order_key, created_at=now)
            session.add(record)

        record.folder_name = folder_name
        record.full_path = full_path
        record.year_month_path = year_month_path
        record.year = str(year) if year else None
        record.month_folder = month_folder or None
        record.pdf_visible = bool(pdf_visible)
        record.pdf_type_found = pdf_type_found or None
        record.pdf_filename = pdf_filename or None
        record.manager_name = manager_name or "Неизвестно"
        record.missing_path = bool(missing_path)

        # Убираем из active, если PDF пропал, но архивные не трогаем.
        if record.status_cpu == "ready" and not record.pdf_visible:
            record.status_cpu = "ready"
        elif record.status_cpu not in {"ready", "review", "confirmed"}:
            record.status_cpu = status_cpu

        record.updated_at = now
        return record


def mark_cpu_order_missing(order_keys: set[str]) -> None:
    if not order_keys:
        return

    now = datetime.utcnow()
    with SessionLocal.begin() as session:
        rows = session.query(CpuOrder).filter(CpuOrder.order_key.in_(list(order_keys))).all()
        for row in rows:
            row.missing_path = True
            row.pdf_visible = False
            row.pdf_type_found = None
            row.pdf_filename = None
            row.updated_at = now


def update_cpu_status(order_key: str, status_cpu: str) -> Optional[CpuOrder]:
    now = datetime.utcnow()
    with SessionLocal.begin() as session:
        row = session.get(CpuOrder, order_key)
        if not row:
            return None

        row.status_cpu = status_cpu
        if status_cpu == "review":
            row.sent_at = row.sent_at or now
        if status_cpu == "confirmed":
            row.confirmed_at = now
        row.updated_at = now
        return row


def update_cpu_manager(order_key: str, manager_name: str) -> Optional[CpuOrder]:
    now = datetime.utcnow()
    with SessionLocal.begin() as session:
        row = session.get(CpuOrder, order_key)
        if not row:
            return None
        row.manager_name = manager_name
        row.updated_at = now
        return row


__all__ = [
    "CpuOrder",
    "get_cpu_order",
    "list_cpu_ready_orders",
    "list_cpu_archive_orders",
    "upsert_cpu_order",
    "mark_cpu_order_missing",
    "update_cpu_status",
    "update_cpu_manager",
]

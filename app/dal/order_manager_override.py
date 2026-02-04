from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, Optional

from sqlalchemy import Column, DateTime, Integer, String, func

from app.dal.db import Base, SessionLocal
from app.services.order_times import normalize_order_key


class OrderManagerOverride(Base):
    __tablename__ = "order_manager_override"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_key = Column(String, nullable=False, unique=True)
    manager_name = Column(String, nullable=False)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp())


def get_overrides_map(order_keys: Iterable[str]) -> Dict[str, OrderManagerOverride]:
    keys = [normalize_order_key(key) for key in order_keys if key]
    if not keys:
        return {}

    with SessionLocal.begin() as session:
        rows = (
            session.query(OrderManagerOverride)
            .filter(OrderManagerOverride.order_key.in_(keys))
            .all()
        )
    return {row.order_key: row for row in rows}


def get_override(order_key: str) -> Optional[OrderManagerOverride]:
    if not order_key:
        return None

    normalized = normalize_order_key(order_key)
    with SessionLocal.begin() as session:
        return session.query(OrderManagerOverride).filter_by(order_key=normalized).first()


def upsert_override(order_key: str, manager_name: str, updated_by: str | None) -> Optional[OrderManagerOverride]:
    if not order_key or not manager_name:
        return None

    normalized = normalize_order_key(order_key)
    with SessionLocal.begin() as session:
        record = session.query(OrderManagerOverride).filter_by(order_key=normalized).first()
        if record:
            record.manager_name = manager_name
            record.updated_by = updated_by
            record.updated_at = datetime.utcnow()
        else:
            record = OrderManagerOverride(
                order_key=normalized,
                manager_name=manager_name,
                updated_by=updated_by,
            )
            session.add(record)
        return record


def delete_override(order_key: str) -> bool:
    if not order_key:
        return False

    normalized = normalize_order_key(order_key)
    with SessionLocal.begin() as session:
        record = session.query(OrderManagerOverride).filter_by(order_key=normalized).first()
        if not record:
            return False
        session.delete(record)
        return True


__all__ = [
    "OrderManagerOverride",
    "get_overrides_map",
    "get_override",
    "upsert_override",
    "delete_override",
]

import logging
from typing import Dict, Iterable, Optional

from sqlalchemy import Column, DateTime, String, func

from app.dal.db import Base, SessionLocal

logger = logging.getLogger("bazis")


class OrderManagerOverride(Base):
    __tablename__ = "order_manager_overrides"

    order_key = Column(String, primary_key=True)
    manager_name = Column(String, nullable=False)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp())


def get_overrides_map(order_keys: Iterable[str] | None = None) -> Dict[str, str]:
    with SessionLocal.begin() as session:
        query = session.query(OrderManagerOverride)
        if order_keys:
            query = query.filter(OrderManagerOverride.order_key.in_(list(order_keys)))
        rows = query.all()
        return {row.order_key: row.manager_name for row in rows if row.order_key}


def get_override(order_key: str) -> Optional[OrderManagerOverride]:
    if not order_key:
        return None
    with SessionLocal.begin() as session:
        return session.get(OrderManagerOverride, order_key)


def set_override(order_key: str, manager_name: str, updated_by: Optional[str] = None) -> bool:
    if not order_key or not manager_name:
        return False

    with SessionLocal.begin() as session:
        record = session.get(OrderManagerOverride, order_key)
        if not record:
            record = OrderManagerOverride(order_key=order_key, manager_name=manager_name, updated_by=updated_by)
            session.add(record)
        else:
            record.manager_name = manager_name
            record.updated_by = updated_by
    return True


def delete_override(order_key: str) -> bool:
    if not order_key:
        return False
    with SessionLocal.begin() as session:
        record = session.get(OrderManagerOverride, order_key)
        if not record:
            return False
        session.delete(record)
    return True


__all__ = [
    "OrderManagerOverride",
    "get_overrides_map",
    "get_override",
    "set_override",
    "delete_override",
]

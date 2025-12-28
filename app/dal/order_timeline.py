from __future__ import annotations

import logging
from typing import Dict, Iterable, Tuple

from sqlalchemy import Column, Float, String

from app.dal.db import Base, SessionLocal

logger = logging.getLogger("bazis")


class OrderTimeline(Base):
    __tablename__ = "order_timelines"

    order_key = Column(String, primary_key=True)
    created_ts = Column(Float, nullable=True)
    processed_ts = Column(Float, nullable=True)


def load_timelines() -> Dict[str, Tuple[float | None, float | None]]:
    """Возвращает все временные метки по заказам."""

    with SessionLocal.begin() as session:
        rows: Iterable[OrderTimeline] = session.query(OrderTimeline).all()
        return {
            row.order_key: (row.created_ts, row.processed_ts)
            for row in rows
            if row.order_key
        }


def upsert_created(order_key: str, created_ts: float) -> float | None:
    """Сохраняет время создания, не затирая более ранние значения."""

    if not order_key or created_ts is None:
        return None

    with SessionLocal.begin() as session:
        record: OrderTimeline | None = session.get(OrderTimeline, order_key)
        if record:
            if record.created_ts is None or created_ts < record.created_ts:
                record.created_ts = created_ts
            return record.created_ts

        session.add(OrderTimeline(order_key=order_key, created_ts=created_ts))
        return created_ts


def upsert_processed(order_key: str, processed_ts: float) -> float | None:
    """Сохраняет время обработки технологом при первом появлении."""

    if not order_key or processed_ts is None:
        return None

    with SessionLocal.begin() as session:
        record: OrderTimeline | None = session.get(OrderTimeline, order_key)
        if record:
            if record.processed_ts is None:
                record.processed_ts = processed_ts
            return record.processed_ts

        session.add(
            OrderTimeline(
                order_key=order_key,
                processed_ts=processed_ts,
            )
        )
        return processed_ts


__all__ = [
    "OrderTimeline",
    "load_timelines",
    "upsert_created",
    "upsert_processed",
]

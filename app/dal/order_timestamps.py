from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String

from app.dal.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderTimestamp(Base):
    __tablename__ = "order_timestamps"

    # stable_key — инвариантный ключ заказа (номер или имя без служебных префиксов)
    stable_key = Column(String, primary_key=True)
    order_name = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True))
    processed_at = Column(DateTime(timezone=True))
    created_source = Column(String)
    processed_source = Column(String)
    last_seen_at = Column(DateTime(timezone=True), default=_utcnow)


__all__ = ["OrderTimestamp"]

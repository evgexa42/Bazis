from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List

from sqlalchemy import Boolean, Column, DateTime, String, func

from app.dal.db import Base, SessionLocal


@dataclass(frozen=True)
class ExternalOrderRecord:
    order_number: str
    approved: bool
    cancelled: bool
    source_created_at: datetime | None
    last_seen_at: datetime | None


class ExternalOrderStatus(Base):
    __tablename__ = "external_orders_status"

    order_number = Column(String, primary_key=True)
    approved = Column(Boolean, nullable=False, default=False)
    cancelled = Column(Boolean, nullable=False, default=False)
    source_created_at = Column(DateTime(timezone=True))
    last_seen_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())


def _to_record(row: ExternalOrderStatus) -> ExternalOrderRecord:
    return ExternalOrderRecord(
        order_number=row.order_number,
        approved=bool(row.approved),
        cancelled=bool(row.cancelled),
        source_created_at=row.source_created_at,
        last_seen_at=row.last_seen_at,
    )


def load_status_map() -> Dict[str, ExternalOrderRecord]:
    with SessionLocal.begin() as session:
        rows: Iterable[ExternalOrderStatus] = session.query(ExternalOrderStatus).all()
        return {row.order_number: _to_record(row) for row in rows}


def replace_statuses(records: List[ExternalOrderRecord]) -> None:
    """
    Это КЭШ внешних статусов (Orders -> локальная SQLite).
    Самый надёжный и простой способ — полностью заменить содержимое таблицы.
    Так мы не упираемся:
      - в лимит SQLite "too many SQL variables"
      - в огромные IN (...)
      - в дубликаты/конфликты upsert.
    """
    with SessionLocal.begin() as session:
        session.query(ExternalOrderStatus).delete()

        if not records:
            return

        # дополнительная страховка: дедуп по order_number (на всякий случай)
        unique: Dict[str, ExternalOrderRecord] = {}
        for rec in records:
            unique[rec.order_number] = rec

        payload = [
            {
                "order_number": rec.order_number,
                "approved": bool(rec.approved),
                "cancelled": bool(rec.cancelled),
                "source_created_at": rec.source_created_at,
                "last_seen_at": rec.last_seen_at,
            }
            for rec in unique.values()
        ]

        # executemany (bulk), не упирается в лимиты переменных
        session.bulk_insert_mappings(ExternalOrderStatus, payload)


__all__ = ["ExternalOrderRecord", "ExternalOrderStatus", "load_status_map", "replace_statuses"]

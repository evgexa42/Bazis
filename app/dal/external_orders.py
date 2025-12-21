from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List

from sqlalchemy import Boolean, Column, DateTime, String, func, insert

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
    numbers = [rec.order_number for rec in records]
    with SessionLocal.begin() as session:
        if not records:
            session.query(ExternalOrderStatus).delete()
            return

        session.query(ExternalOrderStatus).filter(~ExternalOrderStatus.order_number.in_(numbers)).delete()

        payload = [
            {
                "order_number": rec.order_number,
                "approved": bool(rec.approved),
                "cancelled": bool(rec.cancelled),
                "source_created_at": rec.source_created_at,
                "last_seen_at": rec.last_seen_at,
            }
            for rec in records
        ]

        insert_stmt = insert(ExternalOrderStatus)
        stmt = (
            insert_stmt
            .values(payload)
            .on_conflict_do_update(
                index_elements=[ExternalOrderStatus.order_number],
                set_={
                    "approved": insert_stmt.excluded.approved,
                    "cancelled": insert_stmt.excluded.cancelled,
                    "source_created_at": insert_stmt.excluded.source_created_at,
                    "last_seen_at": insert_stmt.excluded.last_seen_at,
                },
            )
        )
        session.execute(stmt)


__all__ = ["ExternalOrderRecord", "ExternalOrderStatus", "load_status_map", "replace_statuses"]

import logging
from collections import defaultdict
from typing import Dict, Iterable, List, Set

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.exc import IntegrityError

from app.dal.db import Base, SessionLocal

logger = logging.getLogger("bazis")


class ManagerPriced(Base):
    __tablename__ = "manager_priced"
    __table_args__ = (
        UniqueConstraint("order_key", "manager_username", name="uq_manager_priced_order_manager"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_key = Column(String, nullable=False)
    manager_username = Column(String, nullable=False)
    priced_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())


def get_priced_set(manager_username: str) -> Set[str]:
    """Возвращает набор отмеченных заказов для менеджера."""
    if not manager_username:
        return set()

    with SessionLocal.begin() as session:
        rows: Iterable[tuple[str]] = session.query(ManagerPriced.order_key).filter_by(
            manager_username=manager_username
        )
        return {row[0] for row in rows}


def is_priced(order_key: str, manager_username: str) -> bool:
    if not order_key or not manager_username:
        return False

    with SessionLocal.begin() as session:
        exists = (
            session.query(ManagerPriced)
            .filter_by(order_key=order_key, manager_username=manager_username)
            .first()
        )
        return bool(exists)


def get_priced_map(order_keys: Iterable[str] | None = None) -> Dict[str, List[str]]:
    """Возвращает карту order_key -> список менеджеров, отметивших заказ."""

    with SessionLocal.begin() as session:
        query = session.query(ManagerPriced.order_key, ManagerPriced.manager_username)
        if order_keys:
            query = query.filter(ManagerPriced.order_key.in_(list(order_keys)))

        rows: Iterable[tuple[str, str]] = query.all()

    mapping: Dict[str, List[str]] = defaultdict(list)
    for order_key, manager_username in rows:
        mapping[order_key].append(manager_username)

    return dict(mapping)


def set_priced(order_key: str, manager_username: str) -> None:
    if not order_key or not manager_username:
        return

    try:
        with SessionLocal.begin() as session:
            record = ManagerPriced(order_key=order_key, manager_username=manager_username)
            session.add(record)
    except IntegrityError:
        # уникальность гарантирует отсутствие дублей; молча игнорируем повторную отметку
        logger.debug(
            "[manager_priced] Запись уже существует для %s / %s", order_key, manager_username
        )


__all__ = [
    "ManagerPriced",
    "get_priced_set",
    "get_priced_map",
    "is_priced",
    "set_priced",
]
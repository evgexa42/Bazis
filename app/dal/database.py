import json
from typing import Dict, Iterable, List, Tuple

from sqlalchemy import Column, Integer, String, Text, func

from app.dal.db import Base, SessionLocal


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    manager = Column(String, nullable=True)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String, nullable=False)
    text = Column(Text, nullable=False)


def get_all_clients() -> Dict[str, str]:
    with SessionLocal.begin() as session:
        rows = session.query(Client).order_by(Client.name.asc()).all()
        return {row.name: row.manager or "" for row in rows}


def get_clients_filtered(query: str | None) -> Dict[str, str]:
    """Возвращает клиентов, отфильтрованных по подстроке имени."""
    normalized = (query or "").strip().lower()
    with SessionLocal.begin() as session:
        q = session.query(Client)
        if normalized:
            like_pattern = f"%{normalized}%"
            q = q.filter(func.lower(Client.name).like(like_pattern))
        rows = q.order_by(Client.name.asc()).all()
        return {row.name: row.manager or "" for row in rows}


def get_clients_stats_by_manager() -> List[Tuple[str, int]]:
    """Компактная статистика: сколько клиентов у каждого менеджера."""
    with SessionLocal.begin() as session:
        rows = (
            session.query(Client.manager, func.count().label("count"))
            .group_by(Client.manager)
            .order_by(Client.manager.asc())
            .all()
        )
        prepared: List[Tuple[str, int]] = []
        for manager, count in rows:
            prepared.append((manager or "Неизвестно", int(count)))
        return prepared


def replace_clients(clients: Dict[str, str]) -> None:
    with SessionLocal.begin() as session:
        session.query(Client).delete()
        objects = [Client(name=name, manager=manager) for name, manager in clients.items()]
        session.add_all(objects)


def add_client(name: str, manager: str) -> None:
    with SessionLocal.begin() as session:
        existing = session.query(Client).filter_by(name=name).one_or_none()
        if existing:
            existing.manager = manager
        else:
            session.add(Client(name=name, manager=manager))


def update_client(old_name: str, new_name: str, new_manager: str) -> bool:
    with SessionLocal.begin() as session:
        client = session.query(Client).filter_by(name=old_name).one_or_none()
        if not client:
            return False
        client.name = new_name
        client.manager = new_manager
        return True


def delete_client(name: str) -> bool:
    with SessionLocal.begin() as session:
        client = session.query(Client).filter_by(name=name).one_or_none()
        if not client:
            return False
        session.delete(client)
        return True


def load_messages(level: str = "telegram") -> List[dict]:
    with SessionLocal.begin() as session:
        rows = session.query(Message).filter_by(level=level).order_by(Message.id).all()
        result: List[dict] = []
        for row in rows:
            try:
                data = json.loads(row.text)
                if isinstance(data, dict):
                    result.append(data)
            except json.JSONDecodeError:
                continue
        return result


def replace_messages(entries: Iterable[dict], level: str = "telegram") -> None:
    with SessionLocal.begin() as session:
        session.query(Message).filter_by(level=level).delete()
        objects = [Message(level=level, text=json.dumps(entry, ensure_ascii=False)) for entry in entries]
        session.add_all(objects)


__all__ = [
    "Client",
    "Message",
    "add_client",
    "delete_client",
    "get_all_clients",
    "get_clients_filtered",
    "get_clients_stats_by_manager",
    "load_messages",
    "replace_clients",
    "replace_messages",
    "update_client",
]
import json
import os
from contextlib import contextmanager
from typing import Dict, Iterable, List

from sqlalchemy import Column, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = os.path.join(BASE_DIR, "database.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}, future=True
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


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


class OrderIndex(Base):
    __tablename__ = "order_index"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    manager = Column(String, nullable=True)
    folder_path = Column(Text, nullable=False)
    mtime = Column(Float, nullable=False, default=0.0)


def init_db() -> None:
    os.makedirs(BASE_DIR, exist_ok=True)
    Base.metadata.create_all(engine)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_all_clients() -> Dict[str, str]:
    with session_scope() as session:
        rows = session.query(Client).order_by(Client.name.asc()).all()
        return {row.name: row.manager or "" for row in rows}


def replace_clients(clients: Dict[str, str]) -> None:
    with session_scope() as session:
        session.query(Client).delete()
        objects = [Client(name=name, manager=manager) for name, manager in clients.items()]
        session.add_all(objects)


def add_client(name: str, manager: str) -> None:
    with session_scope() as session:
        existing = session.query(Client).filter_by(name=name).one_or_none()
        if existing:
            existing.manager = manager
        else:
            session.add(Client(name=name, manager=manager))


def update_client(old_name: str, new_name: str, new_manager: str) -> bool:
    with session_scope() as session:
        client = session.query(Client).filter_by(name=old_name).one_or_none()
        if not client:
            return False
        client.name = new_name
        client.manager = new_manager
        return True


def delete_client(name: str) -> bool:
    with session_scope() as session:
        client = session.query(Client).filter_by(name=name).one_or_none()
        if not client:
            return False
        session.delete(client)
        return True


def load_messages(level: str = "telegram") -> List[dict]:
    with session_scope() as session:
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
    with session_scope() as session:
        session.query(Message).filter_by(level=level).delete()
        objects = [Message(level=level, text=json.dumps(entry, ensure_ascii=False)) for entry in entries]
        session.add_all(objects)


def replace_order_index(entries: Iterable[dict]) -> None:
    with session_scope() as session:
        session.query(OrderIndex).delete()
        objects = [
            OrderIndex(
                name=entry.get("name", ""),
                manager=entry.get("manager"),
                folder_path=entry.get("folder_path", ""),
                mtime=float(entry.get("mtime") or 0.0),
            )
            for entry in entries
        ]
        session.add_all(objects)


def search_orders(query: str, search_folders: Dict[str, str]) -> Dict[str, List[dict]]:
    normalized_query = (query or "").strip().lower()
    keys = list(search_folders.keys())
    results: Dict[str, List[dict]] = {key: [] for key in keys}
    fallback_key = "Результаты" if not results else "Прочее"
    results.setdefault(fallback_key, [])

    if not normalized_query:
        return results

    with session_scope() as session:
        matches = (
            session.query(OrderIndex)
            .filter(OrderIndex.name.ilike(f"%{normalized_query}%"))
            .order_by(OrderIndex.mtime.desc())
            .all()
        )

    for row in matches:
        target_key = None
        for key, base_path in search_folders.items():
            if not base_path:
                continue
            try:
                if os.path.normcase(row.folder_path).startswith(os.path.normcase(base_path)):
                    target_key = key
                    break
            except Exception:
                continue
        if target_key is None:
            target_key = fallback_key
        results.setdefault(target_key, [])
        results[target_key].append(
            {"name": row.name, "manager": row.manager or "", "path": row.folder_path}
        )

    return results


__all__ = [
    "Client",
    "Message",
    "OrderIndex",
    "add_client",
    "delete_client",
    "get_all_clients",
    "init_db",
    "load_messages",
    "replace_clients",
    "replace_messages",
    "replace_order_index",
    "search_orders",
    "update_client",
]
import os

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, create_engine, func, select
from sqlalchemy.orm import declarative_base, sessionmaker
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = os.path.join(BASE_DIR, "database.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}, future=True
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at = Column(DateTime(timezone=True), onupdate=func.current_timestamp())


class OrderEvent(Base):
    __tablename__ = "order_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_name = Column(String)
    manager = Column(String)
    action = Column(String)
    old_value = Column(Text)
    new_value = Column(Text)
    user = Column(String)
    user_ip = Column(String)
    ts = Column(DateTime(timezone=True), server_default=func.current_timestamp())


def _create_default_admin() -> None:
    with SessionLocal() as session:
        existing = session.execute(select(User)).first()
        if existing:
            return

        password_hash = generate_password_hash("admin123")
        session.add(User(username="admin", password_hash=password_hash, role="admin", is_active=True))
        session.commit()


def init_db() -> None:
    os.makedirs(BASE_DIR, exist_ok=True)
    Base.metadata.create_all(engine)
    _create_default_admin()


__all__ = [
    "Base",
    "DB_PATH",
    "DATABASE_URL",
    "SessionLocal",
    "engine",
    "init_db",
    "User",
    "OrderEvent",
]
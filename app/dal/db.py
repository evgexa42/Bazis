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


DEFAULT_ROLE_PERMISSIONS = {
    "admin": {
        "can_access_settings": 1,
        "can_access_facades": 1,
        "can_access_metrics": 1,
        "can_access_clients": 1,
        "can_access_search": 1,
        "can_manage_users": 1,
        "can_edit_paths": 1,
        "can_toggle_order_options": 1,
    },
    "technologist": {
        "can_access_settings": 1,
        "can_access_facades": 1,
        "can_access_metrics": 1,
        "can_access_clients": 1,
        "can_access_search": 1,
        "can_manage_users": 0,
        "can_edit_paths": 0,
        "can_toggle_order_options": 1,
    },
    "manager": {
        "can_access_settings": 0,
        "can_access_facades": 0,
        "can_access_metrics": 0,
        "can_access_clients": 1,
        "can_access_search": 1,
        "can_manage_users": 0,
        "can_edit_paths": 0,
        "can_toggle_order_options": 0,
    },
}


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at = Column(DateTime(timezone=True), onupdate=func.current_timestamp())


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role = Column(String, primary_key=True)

    can_access_settings = Column(Integer, nullable=False, default=0)
    can_access_facades = Column(Integer, nullable=False, default=0)
    can_access_metrics = Column(Integer, nullable=False, default=0)
    can_access_clients = Column(Integer, nullable=False, default=0)
    can_access_search = Column(Integer, nullable=False, default=0)

    can_manage_users = Column(Integer, nullable=False, default=0)
    can_edit_paths = Column(Integer, nullable=False, default=0)
    can_toggle_order_options = Column(Integer, nullable=False, default=0)


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
    with SessionLocal.begin() as session:
        existing = session.execute(select(User)).first()
        if existing:
            return

        password_hash = generate_password_hash("admin123")
        session.add(User(username="admin", password_hash=password_hash, role="admin", is_active=True))


def _ensure_default_role_permissions() -> None:
    with SessionLocal.begin() as session:
        for role, perms in DEFAULT_ROLE_PERMISSIONS.items():
            record = session.get(RolePermission, role)
            enforced = dict(perms)
            if role == "admin":
                enforced["can_access_settings"] = 1
                enforced["can_manage_users"] = 1

            if not record:
                session.add(RolePermission(role=role, **enforced))
            else:
                if role == "admin":
                    record.can_access_settings = 1
                    record.can_manage_users = 1
                else:
                    for field, value in enforced.items():
                        if getattr(record, field) is None:
                            setattr(record, field, value)


def init_db() -> None:
    os.makedirs(BASE_DIR, exist_ok=True)
    # Регистрируем все модели, зависящие от Base, перед созданием таблиц
    import app.dal.database  # noqa: F401

    Base.metadata.create_all(engine)
    _create_default_admin()
    _ensure_default_role_permissions()


__all__ = [
    "Base",
    "DB_PATH",
    "DATABASE_URL",
    "SessionLocal",
    "engine",
    "init_db",
    "User",
    "OrderEvent",
    "RolePermission",
]
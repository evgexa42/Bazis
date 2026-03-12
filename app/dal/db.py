import logging
import os

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
    inspect,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from werkzeug.security import generate_password_hash

from app.paths import get_base_dir, resolve_path

BASE_DIR = get_base_dir()  # базовый путь нужен для инициализации каталога БД
DB_PATH = resolve_path("database.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}, future=True
)
# В SQLite с expire_on_commit=True поля истекают после коммита и рвут ленивые атрибуты
# вне сессии (например, ts в OrderEvent). Отключаем истечение для стабильного чтения.
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
    expire_on_commit=False,
)
Base = declarative_base()


DEFAULT_ROLE_PERMISSIONS = {
    "admin": {
        "can_access_settings": 1,
        "can_access_facades": 1,
        "can_access_metrics": 1,
        "can_access_clients": 1,
        "can_access_search": 1,
        "can_access_desene_cpu": 1,
        "can_manage_users": 1,
        "can_edit_paths": 1,
        "can_export_db": 1,
        "can_import_db": 1,
        "can_toggle_order_options": 1,
        "can_confirm_orders": 1,
        "can_view_priced": 1,
        "can_mark_priced": 0,
        "can_view_priced_panel": 1,
        "can_edit_order_manager": 1,
    },
    "technologist": {
        "can_access_settings": 1,
        "can_access_facades": 1,
        "can_access_metrics": 1,
        "can_access_clients": 1,
        "can_access_search": 1,
        "can_access_desene_cpu": 1,
        "can_manage_users": 0,
        "can_edit_paths": 0,
        "can_export_db": 0,
        "can_import_db": 0,
        "can_toggle_order_options": 1,
        "can_confirm_orders": 1,
        "can_view_priced": 1,
        "can_mark_priced": 0,
        "can_view_priced_panel": 1,
        "can_edit_order_manager": 1,
    },
    "manager": {
        "can_access_settings": 0,
        "can_access_facades": 0,
        "can_access_metrics": 0,
        "can_access_clients": 1,
        "can_access_search": 1,
        "can_access_desene_cpu": 1,
        "can_manage_users": 0,
        "can_edit_paths": 0,
        "can_export_db": 0,
        "can_import_db": 0,
        "can_toggle_order_options": 0,
        "can_confirm_orders": 1,
        "can_view_priced": 1,
        "can_mark_priced": 1,
        "can_view_priced_panel": 1,
        "can_edit_order_manager": 0,
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
    can_access_desene_cpu = Column(Integer, nullable=False, default=0)

    can_manage_users = Column(Integer, nullable=False, default=0)
    can_edit_paths = Column(Integer, nullable=False, default=0)
    can_export_db = Column(Integer, nullable=False, default=0)
    can_import_db = Column(Integer, nullable=False, default=0)
    can_toggle_order_options = Column(Integer, nullable=False, default=0)
    can_confirm_orders = Column(Integer, nullable=False, default=0)
    can_view_priced = Column(Integer, nullable=False, default=0)
    can_mark_priced = Column(Integer, nullable=False, default=0)
    can_view_priced_panel = Column(Integer, nullable=False, default=0)
    can_edit_order_manager = Column(Integer, nullable=False, default=0)


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
                enforced["can_export_db"] = 1
                enforced["can_import_db"] = 1

            if not record:
                session.add(RolePermission(role=role, **enforced))
            else:
                if role == "admin":
                    record.can_access_settings = 1
                    record.can_manage_users = 1
                    record.can_export_db = 1
                    record.can_import_db = 1
                else:
                    for field, value in enforced.items():
                        if getattr(record, field) is None:
                            setattr(record, field, value)


def _ensure_role_permissions_columns() -> None:
    """Гарантирует наличие новых колонок для прав ролей."""

    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("role_permissions")}
    new_columns = {
        "can_access_desene_cpu": 0,
        "can_export_db": 0,
        "can_import_db": 0,
        "can_confirm_orders": 0,
        "can_view_priced": 0,
        "can_mark_priced": 0,
        "can_view_priced_panel": 0,
        "can_edit_order_manager": 0,
    }

    missing = [name for name in new_columns if name not in columns]
    if missing:
        logging.getLogger("bazis").info(
            "[db] Добавляем отсутствующие колонки в role_permissions: %s", ", ".join(missing)
        )
        with engine.begin() as conn:
            for column_name in missing:
                conn.exec_driver_sql(
                    f"ALTER TABLE role_permissions ADD COLUMN {column_name} INTEGER NOT NULL DEFAULT {int(new_columns[column_name])}"
                )
        # Чистим кеш инспектора, чтобы последующие чтения видели новые колонки.
        inspector = inspect(engine)
        inspector.clear_cache()

    # Отдельный проход по значениям с защитой от устаревших схем.
    try:
        with SessionLocal.begin() as session:
            for role, perms in DEFAULT_ROLE_PERMISSIONS.items():
                record = session.get(RolePermission, role)
                if record:
                    for column_name, default_value in new_columns.items():
                        if getattr(record, column_name, None) is None:
                            setattr(record, column_name, perms.get(column_name, default_value))
                else:
                    session.add(RolePermission(role=role, **perms))
    except Exception as exc:  # pragma: no cover - аварийный путь при миграции
        logging.getLogger("bazis").error(
            "[db] Ошибка при применении новых прав: %s", exc
        )
        raise


def init_db() -> None:
    os.makedirs(BASE_DIR, exist_ok=True)
    # Регистрируем все модели, зависящие от Base, перед созданием таблиц
    import app.dal.database  # noqa: F401
    import app.dal.external_orders  # noqa: F401
    import app.dal.manager_priced  # noqa: F401
    import app.dal.order_manager_override  # noqa: F401
    import app.dal.desene_cpu  # noqa: F401

    Base.metadata.create_all(engine)
    _ensure_role_permissions_columns()
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

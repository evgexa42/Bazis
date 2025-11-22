
import hashlib
import os
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = os.path.join(BASE_DIR, "database.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}, future=True
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);
"""

ORDER_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_name TEXT,
    manager TEXT,
    action TEXT,
    old_value TEXT,
    new_value TEXT,
    user TEXT,
    user_ip TEXT,
    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def get_db_connection() -> sqlite3.Connection:
    os.makedirs(BASE_DIR, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_tables_exist() -> None:
    """Create required auth/admin tables without touching existing ones."""

    with get_db_connection() as connection:
        connection.execute(USERS_TABLE_SQL)
        connection.execute(ORDER_EVENTS_TABLE_SQL)
        connection.commit()


def _create_default_admin() -> None:
    """Seed a temporary default admin user if the table is empty."""

    with get_db_connection() as connection:
        cursor = connection.execute("SELECT COUNT(*) FROM users")
        users_count = cursor.fetchone()[0]
        if users_count:
            return

        # TODO: change the temporary password immediately after first login.
        password_hash = hashlib.sha256("admin123".encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO users (username, password_hash, role, is_active) VALUES (?, ?, ?, 1)",
            ("admin", password_hash, "admin"),
        )
        connection.commit()


def init_db() -> None:
    os.makedirs(BASE_DIR, exist_ok=True)
    ensure_tables_exist()
    Base.metadata.create_all(engine)
    _create_default_admin()


__all__ = [
    "Base",
    "DB_PATH",
    "DATABASE_URL",
    "SessionLocal",
    "engine",
    "ensure_tables_exist",
    "get_db_connection",
    "init_db",
]
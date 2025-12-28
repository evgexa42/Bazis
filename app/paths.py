import os
import sys
from functools import lru_cache

# Централизованная точка для путей, чтобы единообразно работать
# как в исходниках, так и в собранном PyInstaller-билде.


@lru_cache(maxsize=1)
def get_base_dir() -> str:
    """Определяет рабочую директорию приложения.

    Приоритет:
    1. env `BAZIS_BASE_DIR` — ручное переопределение.
    2. frozen-режим (PyInstaller) — рядом с исполняемым файлом.
    3. исходники — корень репозитория.
    """

    env_dir = os.environ.get("BAZIS_BASE_DIR")
    if env_dir:
        return os.path.abspath(env_dir)

    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)

    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def resolve_path(*parts: str) -> str:
    """Безопасно склеивает путь от базовой директории."""

    return os.path.join(get_base_dir(), *parts)


BASE_DIR = get_base_dir()

__all__ = ["get_base_dir", "resolve_path", "BASE_DIR"]
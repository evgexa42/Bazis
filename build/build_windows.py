from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.paths import get_base_dir


def _fmt_data_arg(src: Path, dst: str) -> str:
    """Формирует кросс-платформенный аргумент --add-data для PyInstaller."""

    return f"{src}{os.pathsep}{dst}"


def build() -> int:
    """Собирает onedir-бандл с базовыми статическими ресурсами."""

    try:
        from PyInstaller.__main__ import run as pyinstaller_run  # type: ignore
    except Exception as exc:  # pragma: no cover - вспомогательная утилита
        print("PyInstaller не установлен. Выполните: pip install pyinstaller")
        return 1

    base_dir = Path(get_base_dir())
    dist_dir = base_dir / "dist"
    work_dir = base_dir / "build_artifacts"
    app_name = "OrderMonitor"

    #cipher_key = os.environ.get("PYI_CIPHER_KEY", "bazis-prod-key")
    # Ключ должен переопределяться на продакшн-билдах для обфускации байткода.

    data_args = [
        _fmt_data_arg(base_dir / "app" / "static", "app/static"),
        _fmt_data_arg(base_dir / "app" / "templates", "app/templates"),
    ]

    pyinstaller_args = [
        "--noconfirm",
        "--clean",
        "--onedir",
        "--noconsole",
        f"--name={app_name}",
        f"--distpath={dist_dir}",
        f"--workpath={work_dir}",
        f"--specpath={work_dir}",
        #f"--key={cipher_key}",
    ]
    for data_arg in data_args:
        pyinstaller_args.append(f"--add-data={data_arg}")
    pyinstaller_args.append(str(base_dir / "run.py"))

    pyinstaller_run(pyinstaller_args)

    target_dir = dist_dir / app_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Копируем пользовательские файлы рядом с exe, оставляя их в открытом доступе.
    for filename in ("config.json", "database.db"):
        src = base_dir / filename
        if src.exists():
            shutil.copy2(src, target_dir / filename)

    logs_dir = target_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    print(f"Готово: {target_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(build())

import json
import os
from typing import Any


def save_json_atomic(filename: str, data: Any) -> None:
    """Сохраняет JSON во временный файл и атомарно заменяет исходный."""
    tmp_filename = f"{filename}.tmp"
    with open(tmp_filename, "w", encoding="utf-8") as tmp_file:
        json.dump(data, tmp_file, ensure_ascii=False, indent=2)
    os.replace(tmp_filename, filename)


def load_json_file(filename: str):
    """Загружает JSON, если файл существует, иначе возвращает None."""
    if not os.path.exists(filename):
        return None

    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)
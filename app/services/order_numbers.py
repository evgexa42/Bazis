import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

ORDER_PREFIX_RE = re.compile(r"^(?P<number>\d+-\d+)", re.IGNORECASE)
PLUS_SUFFIX_RE = re.compile(r"\s+\+$")


@dataclass(frozen=True)
class ParsedOrderNumber:
    """Результат разбора номера заказа."""

    number: str
    cancelled: bool = False


def extract_order_number_from_folder(folder_name: str) -> Optional[str]:
    """Извлекает номер заказа из имени папки (цифры-цифры в начале строки)."""
    if not folder_name:
        return None
    match = ORDER_PREFIX_RE.match(folder_name.strip())
    if not match:
        return None
    return match.group("number")


def extract_order_info_from_sql(raw_number: str) -> Optional[ParsedOrderNumber]:
    """Извлекает номер и признак аннуляции из значения orders.number в PostgreSQL."""
    if not raw_number:
        return None
    match = ORDER_PREFIX_RE.match(raw_number.strip())
    if not match:
        return None

    number = match.group("number")
    cancelled = "anulat" in raw_number.lower()
    return ParsedOrderNumber(number=number, cancelled=cancelled)


def normalize_plus_suffix(name: str) -> str:
    """Убирает служебный плюс в конце, оставляя остальное имя нетронутым."""
    if not name:
        return ""
    return PLUS_SUFFIX_RE.sub("", name).rstrip()


def ensure_plus_suffix(name: str, approved: bool, rename_enabled: bool) -> Optional[str]:
    """Возвращает целевое имя папки с/без плюса, если требуется переименование."""
    if not rename_enabled or not name:
        return None

    has_plus = bool(PLUS_SUFFIX_RE.search(name))
    if approved and not has_plus:
        return f"{name} +"
    if not approved and has_plus:
        return normalize_plus_suffix(name)
    return None


def created_at_cutoff(now: datetime, tail_days: int) -> datetime:
    """Возвращает границу created_at для выборки «актуальных» заказов."""
    safe_tail = max(1, int(tail_days))
    if now.month in (1, 2):
        return now - timedelta(days=safe_tail)
    return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)


__all__ = [
    "ParsedOrderNumber",
    "created_at_cutoff",
    "ensure_plus_suffix",
    "extract_order_info_from_sql",
    "extract_order_number_from_folder",
    "normalize_plus_suffix",
    "PLUS_SUFFIX_RE",
]

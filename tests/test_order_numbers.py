from datetime import datetime

from app.services.order_numbers import (
    ParsedOrderNumber,
    created_at_cutoff,
    ensure_plus_suffix,
    extract_order_info_from_sql,
    extract_order_number_from_folder,
    normalize_plus_suffix,
)


def test_extract_order_number_from_folder():
    assert extract_order_number_from_folder("864-12 ANDEGRI [J]") == "864-12"
    assert extract_order_number_from_folder("999-01[$] +") == "999-01"
    assert extract_order_number_from_folder("Avans") is None
    assert extract_order_number_from_folder("") is None


def test_extract_order_info_from_sql_with_cancel():
    parsed = extract_order_info_from_sql("864-12 anulat")
    assert parsed == ParsedOrderNumber(number="864-12", cancelled=True)
    assert extract_order_info_from_sql("Avans") is None


def test_created_at_cutoff_rules():
    now_march = datetime(2026, 3, 10)
    now_january = datetime(2026, 1, 15)

    assert created_at_cutoff(now_march, 60) == datetime(2026, 1, 1, 0, 0, 0)
    assert created_at_cutoff(now_january, 60) == datetime(2025, 11, 16, 0, 0, 0)


def test_plus_suffix_rules():
    assert ensure_plus_suffix("864-12 ANDEGRI", approved=True, rename_enabled=True) == "864-12 ANDEGRI +"
    assert ensure_plus_suffix("864-12 ANDEGRI +", approved=True, rename_enabled=True) is None
    assert ensure_plus_suffix("864-12 ANDEGRI +", approved=False, rename_enabled=True) == "864-12 ANDEGRI"
    assert normalize_plus_suffix("864-12 +") == "864-12"

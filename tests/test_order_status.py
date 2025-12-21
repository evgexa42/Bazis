from app.dal.external_orders import ExternalOrderRecord
from app.services import order_status


def test_cancelled_priority_overrides_approved_and_marker():
    original_cache = order_status.cache_snapshot()
    try:
        record = ExternalOrderRecord(
            order_number="864-12",
            approved=True,
            cancelled=True,
            source_created_at=None,
            last_seen_at=None,
        )
        order_status.replace_cache([record])

        enriched = order_status.enrich_orders([{"name": "864-12 [$]", "status": "Готов"}])
        item = enriched[0]

        assert item["is_cancelled"] is True
        assert item["is_approved"] is False
        assert item["status"] == "ANULAT"
        assert "[ANULAT]" in item["display_name"]
    finally:
        order_status.replace_cache(original_cache.values())


def test_plus_suffix_planning_idempotent():
    original_rename = order_status.plan_folder_target_name("864-12", approved=True, cancelled=False)
    assert original_rename == "864-12 +"
    # повторное применение не приводит к смене имени
    assert order_status.plan_folder_target_name("864-12 +", approved=True, cancelled=False) is None
    assert order_status.plan_folder_target_name("864-12 +", approved=False, cancelled=False) == "864-12"

import os
from pathlib import Path

import pytest

import app.config as app_config
from app import create_app
from app.dal.cpu_orders import CpuOrder, get_cpu_order
from app.dal.db import SessionLocal
from app.services.cpu_monitor import build_cpu_order_key, sync_cpu_orders


@pytest.fixture
def cpu_root(tmp_path: Path):
    root = tmp_path / "DESENE CPU"
    folder = root / "2026" / "02. Februarie 2026" / "666-02 CLIENT [$]"
    folder.mkdir(parents=True)
    return root, folder


@pytest.fixture
def client_with_role(cpu_root):
    root, _ = cpu_root
    app_config.CONFIG.setdefault("features", {})["cpu_monitoring_enabled"] = True
    app_config.DESENE_CPU_ROOT = str(root)

    os.environ["BAZIS_DISABLE_BACKGROUND"] = "1"
    app = create_app()
    app.config.update(TESTING=True)

    with app.test_client() as client:
        yield client


def _set_session(client, role: str, username: str):
    with client.session_transaction() as sess:
        sess["user"] = username
        sess["role"] = role
        sess["csrf_token"] = "test-csrf"


def _headers():
    return {"X-CSRF-Token": "test-csrf"}


def _clean_cpu_table():
    with SessionLocal.begin() as session:
        session.query(CpuOrder).delete()


def test_folder_without_pdf_not_in_active_orders(client_with_role, cpu_root):
    client = client_with_role
    _clean_cpu_table()

    _set_session(client, "technologist", "tech")
    response = client.get("/api/cpu/orders")

    assert response.status_code == 200
    assert response.get_json()["orders"] == []


def test_cpu_pdf_appears_in_active_orders(client_with_role, cpu_root):
    client = client_with_role
    _, folder = cpu_root
    _clean_cpu_table()

    (folder / "CPU.pdf").write_text("pdf")
    _set_session(client, "technologist", "tech")

    response = client.get("/api/cpu/orders")
    payload = response.get_json()

    assert response.status_code == 200
    assert len(payload["orders"]) == 1
    assert payload["orders"][0]["pdf_filename"].lower() == "cpu.pdf"
    assert payload["orders"][0]["status_cpu"] == "ready"


def test_send_and_confirm_status_flow(client_with_role, cpu_root):
    client = client_with_role
    _, folder = cpu_root
    _clean_cpu_table()

    (folder / "CPU.pdf").write_text("pdf")
    sync_cpu_orders()
    order_key = build_cpu_order_key(str(folder))

    _set_session(client, "technologist", "tech")

    send_resp = client.post(f"/api/cpu/order/{order_key}/send", headers=_headers())
    assert send_resp.status_code == 200
    assert get_cpu_order(order_key).status_cpu == "review"

    confirm_resp = client.post(f"/api/cpu/order/{order_key}/confirm", headers=_headers())
    assert confirm_resp.status_code == 200
    refreshed = get_cpu_order(order_key)
    assert refreshed.status_cpu == "confirmed"
    assert refreshed.confirmed_at is not None


def test_manager_cannot_change_foreign_order(client_with_role, cpu_root):
    client = client_with_role
    _, folder = cpu_root
    _clean_cpu_table()

    (folder / "CPU.pdf").write_text("pdf")
    sync_cpu_orders()
    order_key = build_cpu_order_key(str(folder))

    # Тестовый менеджер не совпадает с manager_name заказа.
    _set_session(client, "manager", "another_manager")
    response = client.post(f"/api/cpu/order/{order_key}/send", headers=_headers())

    assert response.status_code == 403


def test_technologist_can_change_any_order_and_archive_filters(client_with_role, cpu_root):
    client = client_with_role
    _, folder = cpu_root
    _clean_cpu_table()

    (folder / "CPU.pdf").write_text("pdf")
    sync_cpu_orders()
    order_key = build_cpu_order_key(str(folder))

    _set_session(client, "technologist", "tech")
    send_resp = client.post(f"/api/cpu/order/{order_key}/send", headers=_headers())
    assert send_resp.status_code == 200

    archive_resp = client.get("/api/cpu/archive")
    assert archive_resp.status_code == 200
    orders = archive_resp.get_json()["orders"]
    assert len(orders) == 1
    assert orders[0]["status_cpu"] == "review"


if __name__ == "__main__":
    pytest.main([__file__])

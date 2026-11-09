"""Проверка REST API через TestClient (нужны fastapi и httpx из requirements.txt)."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.container import build_container  # noqa: E402
from app.infrastructure.memory_wallet import InMemoryWallet  # noqa: E402
from app.main import create_app  # noqa: E402

KEY = {"X-API-Key": "test-key"}


@pytest.fixture
def client():
    wallet = InMemoryWallet()
    settings = Settings(db_path=":memory:", api_keys={"test-key": "shop"}, demo_mode=True)
    container = build_container(settings, wallet=wallet, demo_admin=wallet)
    with TestClient(create_app(container=container)) as c:
        yield c


def test_requires_api_key(client):
    r = client.post("/api/v1/payments", json={"order_id": "1", "client_id": "a", "amount": "0.001"})
    assert r.status_code == 401


def test_payment_refund_flow(client):
    assert client.post("/api/v1/demo/clients", json={"client_id": "alice", "fund_btc": "1"}, headers=KEY).status_code == 200

    r = client.post("/api/v1/payments", json={"order_id": "1", "client_id": "alice", "amount": "0.0015"}, headers=KEY)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "SUCCEEDED" and body["message"] == "Деньги успешно списаны"
    assert body["amount_sat"] == 150_000

    again = client.post("/api/v1/payments", json={"order_id": "1", "client_id": "alice", "amount": "0.0015"}, headers=KEY)
    assert again.status_code == 200 and again.json()["id"] == body["id"]

    early = client.post(f"/api/v1/payments/{body['id']}/refunds", json={}, headers=KEY)
    assert early.status_code == 422 and early.json()["error"]["code"] == "NOT_CONFIRMED"

    client.post("/api/v1/demo/mine", json={"blocks": 1}, headers=KEY)
    refund = client.post(f"/api/v1/payments/{body['id']}/refunds", json={"amount": "0.0005"}, headers=KEY)
    assert refund.status_code == 201 and refund.json()["status"] == "COMPLETED"
    status = client.get(f"/api/v1/payments/{body['id']}", headers=KEY).json()
    assert status["status"] == "PARTIALLY_REFUNDED"


def test_failed_payment_explains_reason(client):
    r = client.post("/api/v1/payments", json={"order_id": "2", "client_id": "ghost", "amount": "0.001"}, headers=KEY)
    assert r.status_code == 201
    assert r.json()["failure_reason"] == "CLIENT_NOT_FOUND"


def test_validation(client):
    r = client.post("/api/v1/payments", json={"order_id": "3", "client_id": "a", "amount": "-1"}, headers=KEY)
    assert r.status_code == 422
    r = client.post("/api/v1/payments", json={"order_id": "3", "client_id": "a", "amount": "0.000000001"}, headers=KEY)
    assert r.status_code == 422


def test_not_found(client):
    assert client.get("/api/v1/payments/pay_missing", headers=KEY).status_code == 404

"""Сквозной сценарий через настоящий HTTP JSON-RPC (имитация Bitcoin Core).

Проверяет, что адаптер BitcoinCoreWallet правильно вызывает RPC: адрес
/wallet/<имя>, именованные параметры, разбор ответов и кодов ошибок.
"""
from decimal import Decimal

import pytest

from app.application.payment_service import CreatePaymentCommand
from app.config import Settings
from app.container import build_container
from app.domain.models import FailureReason, PaymentStatus as S, RefundStatus
from app.infrastructure.bitcoin_rpc import BitcoinRpcClient, RpcConnectionError, RpcError
from tests.fake_bitcoind import start_fake_bitcoind


@pytest.fixture
def node():
    server, chain, url = start_fake_bitcoind("u", "p")
    yield chain, url
    server.shutdown()


@pytest.fixture
def container(node):
    _, url = node
    settings = Settings(wallet_backend="bitcoin", bitcoin_rpc_url=url, bitcoin_rpc_user="u",
                        bitcoin_rpc_password="p", db_path=":memory:")
    return build_container(settings)


def test_rpc_errors_are_parsed(node):
    _, url = node
    rpc = BitcoinRpcClient(url, "u", "p")
    with pytest.raises(RpcError) as err:
        rpc.call("getwalletinfo", wallet="nope")
    assert err.value.code == -18


def test_wrong_rpc_password(node):
    _, url = node
    with pytest.raises(RpcConnectionError):
        BitcoinRpcClient(url, "u", "wrong").call("listwallets")


def test_full_flow_over_rpc(node, container):
    chain, _ = node
    assert "pay_service" in chain.loaded  # кошелёк сервиса создан при старте

    created = container.demo_admin.create_client("alice", Decimal("1"))
    assert created["info"].balance_sat == 100_000_000

    payment, _ = container.payments.create_payment(
        CreatePaymentCommand("shop", "order-1", "alice", 150_000, "Заказ 1"))
    assert payment.status is S.SUCCEEDED, payment.failure_message
    assert payment.fee_sat == 141

    methods = [(m, w) for m, w, _ in chain.calls]
    assert ("walletcreatefundedpsbt", "client_alice") in methods   # пробная проверка списания
    assert ("sendtoaddress", "client_alice") in methods            # списание из кошелька клиента
    send = next(p for m, w, p in chain.calls if m == "sendtoaddress" and w == "client_alice")
    assert send["address"] == payment.deposit_address and send["replaceable"] is False

    assert container.payments.get_payment("shop", payment.id).confirmations == 0
    container.demo_admin.mine(1)
    assert container.payments.get_payment("shop", payment.id).confirmations == 1

    refund, _ = container.refunds.create_refund("shop", payment.id, 50_000, "частичный возврат")
    assert refund.status is RefundStatus.COMPLETED
    assert chain.owner_of(refund.destination_address) == "client_alice"
    assert container.payments.get_payment("shop", payment.id).status is S.PARTIALLY_REFUNDED


def test_unknown_client_over_rpc(container):
    payment, _ = container.payments.create_payment(CreatePaymentCommand("shop", "o2", "ghost", 1000))
    assert payment.failure_reason is FailureReason.CLIENT_NOT_FOUND


def test_insufficient_funds_over_rpc(container):
    container.demo_admin.create_client("poor", Decimal("0.0001"))
    payment, _ = container.payments.create_payment(CreatePaymentCommand("shop", "o3", "poor", 10_000))
    assert payment.failure_reason is FailureReason.INSUFFICIENT_FUNDS


def test_unloaded_client_wallet_gets_loaded(node, container):
    chain, _ = node
    container.demo_admin.create_client("carol", Decimal("0.5"))
    chain.loaded.discard("client_carol")  # как после перезапуска узла
    payment, _ = container.payments.create_payment(CreatePaymentCommand("shop", "o4", "carol", 10_000))
    assert payment.status is S.SUCCEEDED

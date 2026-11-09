"""Сценарии оплаты и возврата на кошельке в памяти (быстро, без Bitcoin Core)."""
from datetime import timedelta

import pytest

from app.application.payment_service import CreatePaymentCommand
from app.config import Settings
from app.container import build_container
from app.domain.errors import IdempotencyConflict, PaymentNotFound, RefundNotAllowed, WalletError, WalletTimeout
from app.domain.models import FailureReason, PaymentStatus as S, RefundStatus
from app.infrastructure.memory_wallet import InMemoryWallet


@pytest.fixture
def env():
    wallet = InMemoryWallet(fee_sat=200)
    wallet.add_client("alice", balance_sat=1_000_000)
    wallet.add_client("bob", balance_sat=500)
    wallet.add_client("locked", balance_sat=1_000_000, locked=True)
    container = build_container(Settings(db_path=":memory:"), wallet=wallet, demo_admin=wallet)
    return container, wallet


def pay(container, order="o1", client="alice", amount=100_000, merchant="shop"):
    return container.payments.create_payment(CreatePaymentCommand(merchant, order, client, amount))


def test_successful_payment_goes_through_all_steps(env):
    container, wallet = env
    payment, created = pay(container)
    assert created
    assert payment.status is S.SUCCEEDED
    assert payment.txid and payment.fee_sat == 200
    assert [h.status for h in payment.history] == [
        S.CREATED, S.CLIENT_FOUND, S.CLIENT_VERIFIED, S.FUNDS_CONFIRMED, S.PROCESSING, S.SUCCEEDED,
    ]
    assert wallet.clients["alice"].balance_sat == 1_000_000 - 100_000 - 200


def test_unknown_client(env):
    container, _ = env
    payment, _ = pay(container, client="nobody")
    assert payment.status is S.FAILED
    assert payment.failure_reason is FailureReason.CLIENT_NOT_FOUND


def test_locked_wallet(env):
    container, _ = env
    payment, _ = pay(container, client="locked")
    assert payment.failure_reason is FailureReason.WALLET_UNAVAILABLE


def test_insufficient_funds(env):
    container, wallet = env
    payment, _ = pay(container, client="bob", amount=1_000)
    assert payment.failure_reason is FailureReason.INSUFFICIENT_FUNDS
    assert wallet.clients["bob"].balance_sat == 500  # ничего не списано


def test_balance_ok_but_not_enough_for_fee(env):
    container, _ = env
    payment, _ = pay(container, client="bob", amount=400)  # 400 + комиссия 200 > 500
    assert payment.failure_reason is FailureReason.INSUFFICIENT_FUNDS


def test_fee_too_high():
    wallet = InMemoryWallet(fee_sat=50_000)
    wallet.add_client("alice", balance_sat=1_000_000)
    container = build_container(Settings(db_path=":memory:", max_fee_sat=10_000), wallet=wallet)
    payment, _ = pay(container)
    assert payment.failure_reason is FailureReason.FEE_TOO_HIGH


def test_idempotent_retry_does_not_charge_twice(env):
    container, wallet = env
    first, created1 = pay(container)
    second, created2 = pay(container)
    assert created1 and not created2
    assert first.id == second.id
    assert wallet.clients["alice"].balance_sat == 1_000_000 - 100_200


def test_same_order_different_amount_is_conflict(env):
    container, _ = env
    pay(container)
    with pytest.raises(IdempotencyConflict):
        pay(container, amount=5)


def test_timeout_leaves_processing_then_reconciles(env):
    container, wallet = env
    wallet.fail_next_charge_with = WalletTimeout("нет ответа")
    payment, _ = pay(container)
    assert payment.status is S.PROCESSING          # результат неизвестен — не врём мерчанту
    refreshed = container.payments.get_payment("shop", payment.id)
    assert refreshed.status is S.SUCCEEDED         # сверка нашла транзакцию на адресе сервиса


def test_wallet_error_during_charge(env):
    container, wallet = env
    wallet.fail_next_charge_with = WalletError("узел упал")
    payment, _ = pay(container)
    assert payment.failure_reason is FailureReason.WALLET_ERROR


def test_merchant_cannot_see_foreign_payment(env):
    container, _ = env
    payment, _ = pay(container)
    with pytest.raises(PaymentNotFound):
        container.payments.get_payment("other-shop", payment.id)


def test_confirmations_are_refreshed(env):
    container, wallet = env
    payment, _ = pay(container)
    assert container.payments.get_payment("shop", payment.id).confirmations == 0
    wallet.mine(3)
    assert container.payments.get_payment("shop", payment.id).confirmations == 3


# --- возвраты ---

def test_refund_requires_confirmation(env):
    container, _ = env
    payment, _ = pay(container)
    with pytest.raises(RefundNotAllowed) as err:
        container.refunds.create_refund("shop", payment.id, None, "возврат")
    assert err.value.code == "NOT_CONFIRMED"


def test_partial_then_full_refund(env):
    container, wallet = env
    payment, _ = pay(container)
    wallet.mine(1)
    refund1, _ = container.refunds.create_refund("shop", payment.id, 30_000, "часть")
    assert refund1.status is RefundStatus.COMPLETED and refund1.txid
    assert container.payments.get_payment("shop", payment.id).status is S.PARTIALLY_REFUNDED
    refund2, _ = container.refunds.create_refund("shop", payment.id, None, "остаток")
    assert refund2.amount_sat == 70_000
    p = container.payments.get_payment("shop", payment.id)
    assert p.status is S.REFUNDED and p.refunded_sat == 100_000
    # клиент получил ровно 100 000 сат обратно: комиссию возврата платит сервис
    assert wallet.clients["alice"].balance_sat == 1_000_000 - 200
    assert len(container.refunds.list_refunds("shop", payment.id)) == 2


def test_refund_more_than_paid(env):
    container, wallet = env
    payment, _ = pay(container)
    wallet.mine(1)
    with pytest.raises(RefundNotAllowed) as err:
        container.refunds.create_refund("shop", payment.id, 100_001, None)
    assert err.value.code == "AMOUNT_EXCEEDS_REFUNDABLE"


def test_refund_of_failed_payment(env):
    container, _ = env
    payment, _ = pay(container, client="nobody")
    with pytest.raises(RefundNotAllowed) as err:
        container.refunds.create_refund("shop", payment.id, None, None)
    assert err.value.code == "INVALID_STATUS"


def test_refund_window(env):
    container, wallet = env
    payment, _ = pay(container)
    wallet.mine(1)
    stored = container.payments.get_payment("shop", payment.id)
    stored.created_at -= timedelta(days=31)
    container.payments._repo.update(stored)
    with pytest.raises(RefundNotAllowed) as err:
        container.refunds.create_refund("shop", payment.id, None, None)
    assert err.value.code == "WINDOW_EXPIRED"


def test_refund_idempotency_key(env):
    container, wallet = env
    payment, _ = pay(container)
    wallet.mine(1)
    r1, c1 = container.refunds.create_refund("shop", payment.id, 10_000, None, idempotency_key="k1")
    r2, c2 = container.refunds.create_refund("shop", payment.id, 10_000, None, idempotency_key="k1")
    assert c1 and not c2 and r1.id == r2.id
    assert container.payments.get_payment("shop", payment.id).refunded_sat == 10_000


def test_refund_wallet_failure_is_recorded(env):
    container, wallet = env
    payment, _ = pay(container)
    wallet.mine(1)
    wallet.service_balance_sat = 0
    refund, _ = container.refunds.create_refund("shop", payment.id, None, None)
    assert refund.status is RefundStatus.FAILED
    assert container.payments.get_payment("shop", payment.id).status is S.SUCCEEDED  # платёж не тронут

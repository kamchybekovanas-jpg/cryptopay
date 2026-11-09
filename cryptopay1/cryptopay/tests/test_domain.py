from decimal import Decimal

import pytest

from app.domain.errors import InvalidTransition
from app.domain.models import FailureReason, Payment, PaymentStatus as S
from app.domain.money import btc_to_sat, sat_to_btc


def test_btc_sat_conversion():
    assert btc_to_sat(Decimal("0.0015")) == 150_000
    assert btc_to_sat("1") == 100_000_000
    assert sat_to_btc(150_000) == Decimal("0.00150000")
    with pytest.raises(ValueError):
        btc_to_sat("0.000000001")  # 9 знаков после запятой


def test_happy_path_transitions():
    p = Payment.new("shop", "o1", "alice", 1000)
    for status in (S.CLIENT_FOUND, S.CLIENT_VERIFIED, S.FUNDS_CONFIRMED, S.PROCESSING):
        p.transition_to(status)
    p.mark_succeeded("tx1", 141)
    assert p.status is S.SUCCEEDED
    assert [h.status for h in p.history][0] is S.CREATED
    assert len(p.history) == 6


def test_cannot_skip_steps():
    p = Payment.new("shop", "o1", "alice", 1000)
    with pytest.raises(InvalidTransition):
        p.transition_to(S.SUCCEEDED)  # нельзя списать, не проверив клиента и баланс


def test_failed_is_final():
    p = Payment.new("shop", "o1", "alice", 1000)
    p.fail(FailureReason.CLIENT_NOT_FOUND, "нет клиента")
    assert p.is_final
    with pytest.raises(InvalidTransition):
        p.transition_to(S.CLIENT_FOUND)


def test_refund_accounting():
    p = Payment.new("shop", "o1", "alice", 10_000)
    for status in (S.CLIENT_FOUND, S.CLIENT_VERIFIED, S.FUNDS_CONFIRMED, S.PROCESSING):
        p.transition_to(status)
    p.mark_succeeded("tx1", 141)
    p.register_refund(4_000)
    assert p.status is S.PARTIALLY_REFUNDED and p.refundable_sat == 6_000
    p.register_refund(6_000)
    assert p.status is S.REFUNDED and p.refundable_sat == 0
    with pytest.raises(ValueError):
        p.register_refund(1)


def test_amount_must_be_positive():
    with pytest.raises(ValueError):
        Payment.new("shop", "o1", "alice", 0)

"""Правила возврата. Каждое правило — отдельный класс (принципы S и O).

Новое правило (например, «не больше 3 возвратов на платёж») добавляется
новым классом в список правил в app/main.py, существующий код не меняется.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from app.domain.errors import RefundNotAllowed
from app.domain.models import REFUNDABLE_STATUSES, Payment


class RefundRule(ABC):
    @abstractmethod
    def check(self, payment: Payment, amount_sat: int, now: datetime) -> None:
        """Ничего не возвращает, если возврат разрешён; иначе бросает RefundNotAllowed."""


class StatusRule(RefundRule):
    def check(self, payment, amount_sat, now):
        if payment.status not in REFUNDABLE_STATUSES:
            raise RefundNotAllowed(
                "INVALID_STATUS",
                f"Возврат невозможен для платежа в статусе {payment.status.value}",
            )


class ConfirmationsRule(RefundRule):
    """Нельзя возвращать деньги, пока исходная транзакция не попала в блок."""

    def __init__(self, min_confirmations: int):
        self._min = min_confirmations

    def check(self, payment, amount_sat, now):
        if payment.confirmations < self._min:
            raise RefundNotAllowed(
                "NOT_CONFIRMED",
                f"Оплата ещё не подтверждена сетью ({payment.confirmations} из {self._min} подтверждений)",
            )


class RefundWindowRule(RefundRule):
    def __init__(self, days: int):
        self._window = timedelta(days=days)
        self._days = days

    def check(self, payment, amount_sat, now):
        if now - payment.created_at > self._window:
            raise RefundNotAllowed("WINDOW_EXPIRED", f"Срок возврата ({self._days} дней) истёк")


class AmountRule(RefundRule):
    def __init__(self, min_refund_sat: int):
        self._min = min_refund_sat

    def check(self, payment, amount_sat, now):
        if amount_sat > payment.refundable_sat:
            raise RefundNotAllowed(
                "AMOUNT_EXCEEDS_REFUNDABLE",
                f"Можно вернуть не больше {payment.refundable_sat} сат",
            )
        if amount_sat < self._min:
            raise RefundNotAllowed("AMOUNT_TOO_SMALL", f"Минимальная сумма возврата {self._min} сат")


class RefundPolicy:
    def __init__(self, rules: list[RefundRule]):
        self._rules = rules

    def ensure_allowed(self, payment: Payment, amount_sat: int, now: datetime) -> None:
        for rule in self._rules:
            rule.check(payment, amount_sat, now)

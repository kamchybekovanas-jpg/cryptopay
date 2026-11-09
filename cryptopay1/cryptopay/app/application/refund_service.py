"""Сценарий «вернуть деньги клиенту» (refund).

Порядок:
1. найти платёж и сверить подтверждения;
2. проверить правила возврата (RefundPolicy);
3. сохранить возврат в статусе PENDING;
4. получить новый адрес в кошельке клиента;
5. перевести деньги из кошелька сервиса на этот адрес;
6. отметить возврат COMPLETED, платёж — PARTIALLY_REFUNDED или REFUNDED.
Комиссию сети за возврат платит сервис: клиент получает ровно запрошенную сумму.
"""
from __future__ import annotations

import logging
import threading

from app.application.payment_service import PaymentService
from app.application.ports import PaymentRepository, RefundExecutor
from app.application.refund_policy import RefundPolicy
from app.domain.errors import WalletError
from app.domain.models import Refund, utcnow

log = logging.getLogger(__name__)


class RefundService:
    def __init__(self, repository: PaymentRepository, payments: PaymentService,
                 executor: RefundExecutor, policy: RefundPolicy):
        self._repo = repository
        self._payments = payments
        self._executor = executor
        self._policy = policy
        self._lock = threading.Lock()  # два параллельных возврата не превысят сумму платежа

    def create_refund(self, merchant_id: str, payment_id: str, amount_sat: int | None,
                      reason: str | None, idempotency_key: str | None = None) -> tuple[Refund, bool]:
        with self._lock:
            payment = self._payments.get_payment(merchant_id, payment_id)

            if idempotency_key:
                existing = self._repo.get_refund_by_key(payment.id, idempotency_key)
                if existing:
                    return existing, False

            amount = amount_sat if amount_sat is not None else payment.refundable_sat
            self._policy.ensure_allowed(payment, amount, utcnow())

            refund = Refund.new(payment.id, amount, reason, idempotency_key)
            self._repo.add_refund(refund)

            try:
                address = self._executor.new_client_address(payment.client_id, label=refund.id)
                result = self._executor.send_refund(address, amount, comment=refund.id)
            except WalletError as exc:
                log.exception("Возврат %s не прошёл", refund.id)
                refund.fail(f"Ошибка кошелька: {exc}")
                self._repo.update_refund(refund)
                return refund, True

            refund.complete(address, result.txid, result.fee_sat)
            payment.register_refund(amount)
            self._repo.update_refund(refund)
            self._repo.update(payment)
            return refund, True

    def list_refunds(self, merchant_id: str, payment_id: str) -> list[Refund]:
        payment = self._payments.get_payment(merchant_id, payment_id)
        return self._repo.list_refunds(payment.id)

"""Сценарий «принять оплату» — дирижёр шагов.

PaymentService не знает, как устроен кошелёк или база: он получает
список шагов и репозиторий через конструктор (принцип D) и просто
прогоняет платёж по шагам, сохраняя результат после каждого.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Sequence

from app.application.ports import PaymentRepository, PaymentTracker
from app.application.steps import OutcomeUnknown, PaymentContext, PaymentStep, StepFailed
from app.domain.errors import IdempotencyConflict, PaymentNotFound, WalletError
from app.domain.models import FailureReason, Payment, PaymentStatus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CreatePaymentCommand:
    merchant_id: str
    order_id: str
    client_id: str
    amount_sat: int
    description: str | None = None


class PaymentService:
    def __init__(self, repository: PaymentRepository, steps: Sequence[PaymentStep],
                 tracker: PaymentTracker):
        self._repo = repository
        self._steps = list(steps)
        self._tracker = tracker
        self._lock = threading.Lock()  # один order_id не обрабатывается дважды параллельно

    def create_payment(self, cmd: CreatePaymentCommand) -> tuple[Payment, bool]:
        """Возвращает (платёж, создан_ли_новый). Повтор того же order_id вернёт старый платёж."""
        with self._lock:
            existing = self._repo.get_by_order(cmd.merchant_id, cmd.order_id)
            if existing:
                if (existing.client_id, existing.amount_sat) != (cmd.client_id, cmd.amount_sat):
                    raise IdempotencyConflict(
                        f"order_id {cmd.order_id} уже использован с другим клиентом или суммой"
                    )
                return self.refresh(existing), False
            payment = Payment.new(cmd.merchant_id, cmd.order_id, cmd.client_id,
                                  cmd.amount_sat, cmd.description)
            self._repo.add(payment)

        self._run_pipeline(payment)
        return payment, True

    def _run_pipeline(self, payment: Payment) -> None:
        ctx = PaymentContext(payment=payment, checkpoint=lambda: self._repo.update(payment))
        try:
            for step in self._steps:
                step.execute(ctx)
                self._repo.update(payment)
        except StepFailed as exc:
            log.info("Платёж %s отклонён: %s", payment.id, exc.message)
            payment.fail(exc.reason, exc.message)
        except OutcomeUnknown as exc:
            log.warning("Платёж %s: результат списания неизвестен (%s), нужна сверка", payment.id, exc)
        except WalletError as exc:
            log.exception("Платёж %s: ошибка кошелька", payment.id)
            payment.fail(FailureReason.WALLET_ERROR, f"Ошибка кошелька: {exc}")
        self._repo.update(payment)

    def get_payment(self, merchant_id: str, payment_id: str) -> Payment:
        payment = self._repo.get(payment_id)
        if payment is None or payment.merchant_id != merchant_id:
            raise PaymentNotFound(payment_id)
        return self.refresh(payment)

    def list_payments(self, merchant_id: str, limit: int = 50) -> list[Payment]:
        return self._repo.list_by_merchant(merchant_id, limit)

    def refresh(self, payment: Payment) -> Payment:
        """Сверка с блокчейном: подтягивает подтверждения и завершает зависшие платежи."""
        try:
            if payment.status is PaymentStatus.PROCESSING and payment.deposit_address:
                incoming = self._tracker.find_incoming(payment.deposit_address)
                if incoming and incoming.amount_sat >= payment.amount_sat:
                    payment.mark_succeeded(incoming.txid, None)
            if payment.txid:
                payment.confirmations = self._tracker.get_confirmations(payment.txid)
        except WalletError as exc:
            log.warning("Не удалось сверить платёж %s: %s", payment.id, exc)
            return payment
        self._repo.update(payment)
        return payment

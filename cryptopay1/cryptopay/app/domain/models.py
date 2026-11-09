"""Сущности предметной области: платёж, возврат, их статусы.

Здесь нет ни FastAPI, ни SQL, ни Bitcoin Core — только бизнес-правила.
Статусы платежа меняются только через Payment.transition_to(), который
сверяется с таблицей ALLOWED_TRANSITIONS (это и есть диаграмма состояний).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from app.domain.errors import InvalidTransition


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentStatus(str, Enum):
    CREATED = "CREATED"                        # запрос принят и сохранён
    CLIENT_FOUND = "CLIENT_FOUND"              # шаг 2: клиент и его кошелёк найдены
    CLIENT_VERIFIED = "CLIENT_VERIFIED"        # шаг 3: данные клиента получены
    FUNDS_CONFIRMED = "FUNDS_CONFIRMED"        # шаг 4: денег хватает, списание возможно
    PROCESSING = "PROCESSING"                  # шаг 5: транзакция отправляется
    SUCCEEDED = "SUCCEEDED"                    # шаг 6: деньги списаны
    FAILED = "FAILED"                          # оплата не прошла (причина в failure_reason)
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"  # часть суммы возвращена
    REFUNDED = "REFUNDED"                      # вся сумма возвращена


class FailureReason(str, Enum):
    CLIENT_NOT_FOUND = "CLIENT_NOT_FOUND"
    WALLET_UNAVAILABLE = "WALLET_UNAVAILABLE"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    FEE_TOO_HIGH = "FEE_TOO_HIGH"
    WALLET_ERROR = "WALLET_ERROR"


S = PaymentStatus
ALLOWED_TRANSITIONS: dict[PaymentStatus, set[PaymentStatus]] = {
    S.CREATED: {S.CLIENT_FOUND, S.FAILED},
    S.CLIENT_FOUND: {S.CLIENT_VERIFIED, S.FAILED},
    S.CLIENT_VERIFIED: {S.FUNDS_CONFIRMED, S.FAILED},
    S.FUNDS_CONFIRMED: {S.PROCESSING},
    S.PROCESSING: {S.SUCCEEDED, S.FAILED},
    S.SUCCEEDED: {S.PARTIALLY_REFUNDED, S.REFUNDED},
    S.PARTIALLY_REFUNDED: {S.PARTIALLY_REFUNDED, S.REFUNDED},
    S.REFUNDED: set(),
    S.FAILED: set(),
}
REFUNDABLE_STATUSES = {S.SUCCEEDED, S.PARTIALLY_REFUNDED}


@dataclass
class StatusChange:
    status: PaymentStatus
    at: datetime
    note: str = ""


@dataclass
class Payment:
    id: str
    merchant_id: str
    order_id: str
    client_id: str
    amount_sat: int
    description: str | None = None
    status: PaymentStatus = PaymentStatus.CREATED
    failure_reason: FailureReason | None = None
    failure_message: str | None = None
    deposit_address: str | None = None
    txid: str | None = None
    fee_sat: int | None = None
    confirmations: int = 0
    refunded_sat: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    history: list[StatusChange] = field(default_factory=list)

    @classmethod
    def new(cls, merchant_id: str, order_id: str, client_id: str,
            amount_sat: int, description: str | None = None) -> "Payment":
        if amount_sat <= 0:
            raise ValueError("Сумма должна быть больше нуля")
        payment = cls(
            id=f"pay_{uuid.uuid4().hex[:16]}",
            merchant_id=merchant_id,
            order_id=order_id,
            client_id=client_id,
            amount_sat=amount_sat,
            description=description,
        )
        payment.history.append(StatusChange(PaymentStatus.CREATED, payment.created_at, "Запрос принят"))
        return payment

    # --- переходы состояний ---

    def can_transition_to(self, target: PaymentStatus) -> bool:
        return target in ALLOWED_TRANSITIONS[self.status]

    def transition_to(self, target: PaymentStatus, note: str = "") -> None:
        if not self.can_transition_to(target):
            raise InvalidTransition(self.status, target)
        self.status = target
        self.updated_at = utcnow()
        self.history.append(StatusChange(target, self.updated_at, note))

    def fail(self, reason: FailureReason, message: str) -> None:
        self.transition_to(PaymentStatus.FAILED, message)
        self.failure_reason = reason
        self.failure_message = message

    def mark_succeeded(self, txid: str, fee_sat: int | None) -> None:
        self.transition_to(PaymentStatus.SUCCEEDED, f"txid={txid}")
        self.txid = txid
        self.fee_sat = fee_sat

    # --- возвраты ---

    @property
    def refundable_sat(self) -> int:
        if self.status not in REFUNDABLE_STATUSES:
            return 0
        return self.amount_sat - self.refunded_sat

    def register_refund(self, amount_sat: int) -> None:
        if amount_sat <= 0 or amount_sat > self.refundable_sat:
            raise ValueError("Сумма возврата вне допустимого диапазона")
        self.refunded_sat += amount_sat
        target = PaymentStatus.REFUNDED if self.refunded_sat == self.amount_sat else PaymentStatus.PARTIALLY_REFUNDED
        self.transition_to(target, f"Возврат {amount_sat} сат")

    @property
    def is_final(self) -> bool:
        return self.status in {PaymentStatus.FAILED, PaymentStatus.REFUNDED}


class RefundStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class Refund:
    id: str
    payment_id: str
    amount_sat: int
    reason: str | None = None
    idempotency_key: str | None = None
    status: RefundStatus = RefundStatus.PENDING
    destination_address: str | None = None
    txid: str | None = None
    fee_sat: int | None = None
    failure_message: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @classmethod
    def new(cls, payment_id: str, amount_sat: int, reason: str | None,
            idempotency_key: str | None = None) -> "Refund":
        return cls(
            id=f"ref_{uuid.uuid4().hex[:16]}",
            payment_id=payment_id,
            amount_sat=amount_sat,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def complete(self, address: str, txid: str, fee_sat: int | None) -> None:
        if self.status is not RefundStatus.PENDING:
            raise InvalidTransition(self.status, RefundStatus.COMPLETED)
        self.status = RefundStatus.COMPLETED
        self.destination_address = address
        self.txid = txid
        self.fee_sat = fee_sat
        self.updated_at = utcnow()

    def fail(self, message: str) -> None:
        if self.status is not RefundStatus.PENDING:
            raise InvalidTransition(self.status, RefundStatus.FAILED)
        self.status = RefundStatus.FAILED
        self.failure_message = message
        self.updated_at = utcnow()

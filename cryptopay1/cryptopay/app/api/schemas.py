"""Схемы запросов и ответов REST API (валидация входных данных — Pydantic)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer

from app.domain.models import FailureReason, Payment, PaymentStatus, Refund, RefundStatus
from app.domain.money import sat_to_btc

BtcAmount = Annotated[Decimal, Field(gt=0, le=21_000_000, decimal_places=8,
                                     description="Сумма в BTC, не больше 8 знаков после запятой")]

# В ответах суммы всегда строкой с 8 знаками: "0.00000000", а не "0E-8"
BtcOut = Annotated[Decimal, PlainSerializer(lambda d: f"{d:.8f}", return_type=str)]

STATUS_MESSAGES = {
    PaymentStatus.SUCCEEDED: "Деньги успешно списаны",
    PaymentStatus.PROCESSING: "Платёж обрабатывается, результат уточняется",
    PaymentStatus.PARTIALLY_REFUNDED: "Деньги списаны, часть суммы возвращена клиенту",
    PaymentStatus.REFUNDED: "Деньги списаны и полностью возвращены клиенту",
}
FAILURE_MESSAGES = {
    FailureReason.CLIENT_NOT_FOUND: "Оплата не прошла: клиент не найден в кошельке",
    FailureReason.WALLET_UNAVAILABLE: "Оплата не прошла: кошелёк клиента недоступен",
    FailureReason.INSUFFICIENT_FUNDS: "Оплата не прошла: недостаточно средств",
    FailureReason.FEE_TOO_HIGH: "Оплата не прошла: комиссия сети слишком высокая",
    FailureReason.WALLET_ERROR: "Оплата не прошла: ошибка кошелька",
}


class CreatePaymentRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=64, examples=["order-1001"],
                          description="Номер заказа у мерчанта. Повтор с тем же order_id не спишет деньги дважды")
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,50}$", examples=["alice"])
    amount: BtcAmount = Field(examples=["0.0015"])
    currency: Literal["BTC"] = "BTC"
    description: str | None = Field(default=None, max_length=255, examples=["Заказ №1001"])


class CreateRefundRequest(BaseModel):
    amount: BtcAmount | None = Field(default=None, description="Пусто — вернуть весь остаток")
    reason: str | None = Field(default=None, max_length=255, examples=["Товар возвращён"])
    idempotency_key: str | None = Field(default=None, max_length=64, examples=["refund-1001-1"])


class DemoClientRequest(BaseModel):
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,50}$", examples=["alice"])
    fund_btc: Annotated[Decimal, Field(ge=0, le=50, decimal_places=8)] = Decimal("1")


class DemoMineRequest(BaseModel):
    blocks: int = Field(default=1, ge=1, le=200)


class StatusChangeOut(BaseModel):
    status: PaymentStatus
    at: datetime
    note: str


class PaymentOut(BaseModel):
    id: str
    order_id: str
    client_id: str
    amount: BtcOut
    amount_sat: int
    currency: str = "BTC"
    status: PaymentStatus
    message: str
    failure_reason: FailureReason | None
    failure_message: str | None
    deposit_address: str | None
    txid: str | None
    network_fee: BtcOut | None
    confirmations: int
    refunded_amount: BtcOut
    refundable_amount: BtcOut
    description: str | None
    created_at: datetime
    updated_at: datetime
    history: list[StatusChangeOut]

    @classmethod
    def from_domain(cls, p: Payment) -> "PaymentOut":
        if p.status is PaymentStatus.FAILED and p.failure_reason:
            message = FAILURE_MESSAGES[p.failure_reason]
        else:
            message = STATUS_MESSAGES.get(p.status, "Платёж в обработке")
        return cls(
            id=p.id, order_id=p.order_id, client_id=p.client_id,
            amount=sat_to_btc(p.amount_sat), amount_sat=p.amount_sat, status=p.status, message=message,
            failure_reason=p.failure_reason, failure_message=p.failure_message,
            deposit_address=p.deposit_address, txid=p.txid,
            network_fee=sat_to_btc(p.fee_sat) if p.fee_sat is not None else None,
            confirmations=p.confirmations, refunded_amount=sat_to_btc(p.refunded_sat),
            refundable_amount=sat_to_btc(p.refundable_sat), description=p.description,
            created_at=p.created_at, updated_at=p.updated_at,
            history=[StatusChangeOut(status=h.status, at=h.at, note=h.note) for h in p.history],
        )


class RefundOut(BaseModel):
    id: str
    payment_id: str
    amount: BtcOut
    status: RefundStatus
    reason: str | None
    destination_address: str | None
    txid: str | None
    network_fee: BtcOut | None
    failure_message: str | None
    created_at: datetime

    @classmethod
    def from_domain(cls, r: Refund) -> "RefundOut":
        return cls(
            id=r.id, payment_id=r.payment_id, amount=sat_to_btc(r.amount_sat), status=r.status,
            reason=r.reason, destination_address=r.destination_address, txid=r.txid,
            network_fee=sat_to_btc(r.fee_sat) if r.fee_sat is not None else None,
            failure_message=r.failure_message, created_at=r.created_at,
        )

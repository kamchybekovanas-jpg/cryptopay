"""Шаги обработки платежа — по одному классу на шаг из ТЗ.

Принцип S: каждый шаг делает одну вещь.
Принцип O: чтобы добавить шаг (например, антифрод-проверку), пишется новый
класс PaymentStep и добавляется в список в app/main.py. PaymentService при
этом не меняется.
Принцип L: любой шаг можно поставить в конвейер вместо другого — все они
выполняют один контракт execute(ctx): либо продвигают платёж, либо
бросают StepFailed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from app.application.ports import (
    ClientDirectory, ClientInfo, DepositAddressProvider, FundsInspector, PaymentExecutor,
)
from app.domain.errors import InsufficientFunds, WalletError, WalletTimeout, WalletUnavailable
from app.domain.models import FailureReason, Payment, PaymentStatus


class StepFailed(Exception):
    """Шаг определил, что оплата невозможна. Платёж переводится в FAILED."""

    def __init__(self, reason: FailureReason, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class OutcomeUnknown(Exception):
    """Кошелёк не ответил во время списания. Платёж остаётся в PROCESSING до сверки."""


@dataclass
class PaymentContext:
    payment: Payment
    checkpoint: Callable[[], None] = lambda: None   # сохранить платёж в БД прямо сейчас
    client_info: ClientInfo | None = None
    estimated_fee_sat: int | None = None
    extra: dict = field(default_factory=dict)


class PaymentStep(ABC):
    name: str = "step"

    @abstractmethod
    def execute(self, ctx: PaymentContext) -> None: ...


class ClientLookupStep(PaymentStep):
    """Шаг 2. Есть ли такой клиент и есть ли у него кошелёк."""
    name = "client_lookup"

    def __init__(self, directory: ClientDirectory):
        self._directory = directory

    def execute(self, ctx: PaymentContext) -> None:
        client_id = ctx.payment.client_id
        if not self._directory.client_exists(client_id):
            raise StepFailed(FailureReason.CLIENT_NOT_FOUND, f"Клиент {client_id} не найден в кошельке")
        try:
            self._directory.open_client_wallet(client_id)
        except WalletUnavailable as exc:
            raise StepFailed(FailureReason.WALLET_UNAVAILABLE, f"Кошелёк клиента недоступен: {exc}") from exc
        ctx.payment.transition_to(PaymentStatus.CLIENT_FOUND, "Клиент и кошелёк найдены")


class ClientInfoStep(PaymentStep):
    """Шаг 3. Запрос данных клиента."""
    name = "client_info"

    def __init__(self, directory: ClientDirectory):
        self._directory = directory

    def execute(self, ctx: PaymentContext) -> None:
        ctx.client_info = self._directory.get_client_info(ctx.payment.client_id)
        ctx.payment.transition_to(
            PaymentStatus.CLIENT_VERIFIED,
            f"Кошелёк {ctx.client_info.wallet_name}, транзакций: {ctx.client_info.tx_count}",
        )


class DepositAddressStep(PaymentStep):
    """Выдаёт адрес кошелька сервиса для этого платежа (статус не меняет)."""
    name = "deposit_address"

    def __init__(self, provider: DepositAddressProvider):
        self._provider = provider

    def execute(self, ctx: PaymentContext) -> None:
        if not ctx.payment.deposit_address:
            ctx.payment.deposit_address = self._provider.new_deposit_address(label=ctx.payment.id)


class FundsCheckStep(PaymentStep):
    """Шаг 4. Повторный запрос: хватает ли денег и можно ли их списать."""
    name = "funds_check"

    def __init__(self, inspector: FundsInspector, max_fee_sat: int):
        self._inspector = inspector
        self._max_fee_sat = max_fee_sat

    def execute(self, ctx: PaymentContext) -> None:
        p = ctx.payment
        balance = self._inspector.spendable_balance(p.client_id)
        if balance < p.amount_sat:
            raise StepFailed(
                FailureReason.INSUFFICIENT_FUNDS,
                f"Недостаточно средств: на балансе {balance} сат, нужно {p.amount_sat} сат",
            )
        try:
            fee = self._inspector.estimate_fee(p.client_id, p.deposit_address, p.amount_sat)
        except InsufficientFunds as exc:
            raise StepFailed(
                FailureReason.INSUFFICIENT_FUNDS,
                f"Недостаточно средств с учётом комиссии сети (баланс {balance} сат)",
            ) from exc
        if fee > self._max_fee_sat:
            raise StepFailed(
                FailureReason.FEE_TOO_HIGH,
                f"Комиссия сети {fee} сат превышает лимит {self._max_fee_sat} сат",
            )
        ctx.estimated_fee_sat = fee
        p.transition_to(PaymentStatus.FUNDS_CONFIRMED, f"Баланс {balance} сат, комиссия ~{fee} сат")


class ChargeStep(PaymentStep):
    """Шаг 5. Списание денег (отправка транзакции)."""
    name = "charge"

    def __init__(self, executor: PaymentExecutor):
        self._executor = executor

    def execute(self, ctx: PaymentContext) -> None:
        p = ctx.payment
        p.transition_to(PaymentStatus.PROCESSING, "Отправка транзакции")
        ctx.checkpoint()  # PROCESSING сохраняется ДО отправки — чтобы после сбоя было видно, что надо сверить
        try:
            result = self._executor.charge(p.client_id, p.deposit_address, p.amount_sat, comment=p.id)
        except WalletTimeout as exc:
            raise OutcomeUnknown(str(exc)) from exc
        except InsufficientFunds as exc:
            raise StepFailed(FailureReason.INSUFFICIENT_FUNDS, f"Списание отклонено: {exc}") from exc
        except WalletError as exc:
            raise StepFailed(FailureReason.WALLET_ERROR, f"Ошибка кошелька при списании: {exc}") from exc
        p.mark_succeeded(result.txid, result.fee_sat)

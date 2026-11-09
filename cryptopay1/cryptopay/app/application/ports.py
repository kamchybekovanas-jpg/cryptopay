"""Порты — интерфейсы, через которые прикладной слой работает с внешним миром.

Принцип I (разделение интерфейсов): вместо одного большого «Wallet»
несколько маленьких интерфейсов. Каждый шаг оплаты зависит только от того,
что ему нужно: шаг проверки клиента не видит метода списания денег.

Принцип D (инверсия зависимостей): сервисы зависят от этих абстракций,
а конкретные классы (BitcoinCoreWallet, SqlitePaymentRepository)
подставляются в app/main.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.domain.models import Payment, Refund


@dataclass(frozen=True)
class ClientInfo:
    client_id: str
    wallet_name: str
    balance_sat: int          # подтверждённый баланс
    unconfirmed_sat: int      # ещё не подтверждённые поступления
    tx_count: int


@dataclass(frozen=True)
class TransferResult:
    txid: str
    fee_sat: int | None


@dataclass(frozen=True)
class IncomingTransfer:
    txid: str
    amount_sat: int
    confirmations: int


# --- Порты кошелька ---

class ClientDirectory(ABC):
    """Шаги 2–3: есть ли клиент, есть ли у него кошелёк, данные клиента."""

    @abstractmethod
    def client_exists(self, client_id: str) -> bool: ...

    @abstractmethod
    def open_client_wallet(self, client_id: str) -> None:
        """Открывает кошелёк клиента. WalletUnavailable, если не получается."""

    @abstractmethod
    def get_client_info(self, client_id: str) -> ClientInfo: ...


class FundsInspector(ABC):
    """Шаг 4: хватит ли денег и можно ли их списать."""

    @abstractmethod
    def spendable_balance(self, client_id: str) -> int: ...

    @abstractmethod
    def estimate_fee(self, client_id: str, address: str, amount_sat: int) -> int:
        """Пробная сборка транзакции без отправки. InsufficientFunds, если не собирается."""


class DepositAddressProvider(ABC):
    """Адрес кошелька сервиса, на который придёт оплата."""

    @abstractmethod
    def new_deposit_address(self, label: str) -> str: ...


class PaymentExecutor(ABC):
    """Шаг 5: списание денег с кошелька клиента."""

    @abstractmethod
    def charge(self, client_id: str, address: str, amount_sat: int, comment: str) -> TransferResult: ...


class PaymentTracker(ABC):
    """Сверка с блокчейном: подтверждения и поиск зависших платежей."""

    @abstractmethod
    def find_incoming(self, address: str) -> IncomingTransfer | None: ...

    @abstractmethod
    def get_confirmations(self, txid: str) -> int: ...


class RefundExecutor(ABC):
    """Возврат: адрес клиента и перевод из кошелька сервиса."""

    @abstractmethod
    def new_client_address(self, client_id: str, label: str) -> str: ...

    @abstractmethod
    def send_refund(self, address: str, amount_sat: int, comment: str) -> TransferResult: ...


class WalletHealth(ABC):
    @abstractmethod
    def health(self) -> dict: ...


# --- Порт хранилища ---

class PaymentRepository(ABC):
    @abstractmethod
    def add(self, payment: Payment) -> None: ...

    @abstractmethod
    def update(self, payment: Payment) -> None: ...

    @abstractmethod
    def get(self, payment_id: str) -> Payment | None: ...

    @abstractmethod
    def get_by_order(self, merchant_id: str, order_id: str) -> Payment | None: ...

    @abstractmethod
    def list_by_merchant(self, merchant_id: str, limit: int = 50) -> list[Payment]: ...

    @abstractmethod
    def add_refund(self, refund: Refund) -> None: ...

    @abstractmethod
    def update_refund(self, refund: Refund) -> None: ...

    @abstractmethod
    def list_refunds(self, payment_id: str) -> list[Refund]: ...

    @abstractmethod
    def get_refund_by_key(self, payment_id: str, key: str) -> Refund | None: ...

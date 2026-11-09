"""Кошелёк в памяти — для тестов и запуска без Bitcoin Core (WALLET_BACKEND=memory).

Реализует те же порты, что и BitcoinCoreWallet, и ведёт себя по тем же
контрактам. Поэтому его можно подставить вместо настоящего кошелька, и
сервис этого не заметит (принцип L — подстановка Лисков).
"""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from decimal import Decimal

from app.application.ports import (
    ClientDirectory, ClientInfo, DepositAddressProvider, FundsInspector, IncomingTransfer,
    PaymentExecutor, PaymentTracker, RefundExecutor, TransferResult, WalletHealth,
)
from app.domain.errors import InsufficientFunds, WalletTimeout, WalletUnavailable
from app.domain.money import btc_to_sat, sat_to_btc


@dataclass
class _Tx:
    txid: str
    to_address: str
    amount_sat: int
    height: int | None = None  # None = ещё не в блоке


@dataclass
class _ClientWallet:
    name: str
    balance_sat: int = 0
    locked: bool = False
    tx_count: int = 0
    addresses: list[str] = field(default_factory=list)


class InMemoryWallet(ClientDirectory, FundsInspector, DepositAddressProvider, PaymentExecutor,
                     PaymentTracker, RefundExecutor, WalletHealth):
    def __init__(self, fee_sat: int = 200, service_balance_sat: int = 1_000_000):
        # у кошелька сервиса есть резерв на комиссии возвратов
        self.fee_sat = fee_sat
        self.service_balance_sat = service_balance_sat
        self.clients: dict[str, _ClientWallet] = {}
        self.txs: dict[str, _Tx] = {}
        self.height = 0
        self.fail_next_charge_with: Exception | None = None  # для тестов сбоев
        self._counter = itertools.count(1)

    # --- помощники для тестов и демо ---

    def add_client(self, client_id: str, balance_sat: int = 0, locked: bool = False) -> None:
        self.clients[client_id] = _ClientWallet(name=f"client_{client_id}", balance_sat=balance_sat, locked=locked)

    def mine(self, blocks: int = 1) -> dict:
        self.height += blocks
        for tx in self.txs.values():
            if tx.height is None:
                tx.height = self.height - blocks + 1
        return {"mined": blocks, "height": self.height}

    def create_client(self, client_id: str, fund_btc: Decimal) -> dict:
        if client_id not in self.clients:
            self.add_client(client_id)
        self.clients[client_id].balance_sat += btc_to_sat(fund_btc)
        return {"client_id": client_id, "wallet": self.clients[client_id].name,
                "funding_txid": None, "info": self.get_client_info(client_id)}

    def _new_txid(self) -> str:
        return hashlib.sha256(f"tx{next(self._counter)}".encode()).hexdigest()

    def _new_address(self, prefix: str) -> str:
        return f"bcrt1q{prefix}{next(self._counter):06d}"

    # --- ClientDirectory ---

    def client_exists(self, client_id: str) -> bool:
        return client_id in self.clients

    def open_client_wallet(self, client_id: str) -> None:
        if self.clients[client_id].locked:
            raise WalletUnavailable("кошелёк заблокирован")

    def get_client_info(self, client_id: str) -> ClientInfo:
        w = self.clients[client_id]
        return ClientInfo(client_id, w.name, w.balance_sat, 0, w.tx_count)

    # --- FundsInspector ---

    def spendable_balance(self, client_id: str) -> int:
        return self.clients[client_id].balance_sat

    def estimate_fee(self, client_id: str, address: str, amount_sat: int) -> int:
        if self.clients[client_id].balance_sat < amount_sat + self.fee_sat:
            raise InsufficientFunds("Insufficient funds")
        return self.fee_sat

    # --- DepositAddressProvider ---

    def new_deposit_address(self, label: str) -> str:
        return self._new_address("svc")

    # --- PaymentExecutor ---

    def charge(self, client_id: str, address: str, amount_sat: int, comment: str) -> TransferResult:
        if self.fail_next_charge_with:
            exc, self.fail_next_charge_with = self.fail_next_charge_with, None
            if isinstance(exc, WalletTimeout):  # имитация: транзакция ушла, но ответ потерян
                self._transfer(client_id, address, amount_sat)
            raise exc
        return self._transfer(client_id, address, amount_sat)

    def _transfer(self, client_id: str, address: str, amount_sat: int) -> TransferResult:
        w = self.clients[client_id]
        if w.balance_sat < amount_sat + self.fee_sat:
            raise InsufficientFunds("Insufficient funds")
        w.balance_sat -= amount_sat + self.fee_sat
        w.tx_count += 1
        self.service_balance_sat += amount_sat
        tx = _Tx(self._new_txid(), address, amount_sat)
        self.txs[tx.txid] = tx
        return TransferResult(tx.txid, self.fee_sat)

    # --- PaymentTracker ---

    def find_incoming(self, address: str) -> IncomingTransfer | None:
        for tx in self.txs.values():
            if tx.to_address == address:
                return IncomingTransfer(tx.txid, tx.amount_sat, self._confirmations(tx))
        return None

    def _confirmations(self, tx: _Tx) -> int:
        return 0 if tx.height is None else self.height - tx.height + 1

    def get_confirmations(self, txid: str) -> int:
        return self._confirmations(self.txs[txid])

    # --- RefundExecutor ---

    def new_client_address(self, client_id: str, label: str) -> str:
        address = self._new_address("cli")
        self.clients[client_id].addresses.append(address)
        return address

    def send_refund(self, address: str, amount_sat: int, comment: str) -> TransferResult:
        if self.service_balance_sat < amount_sat + self.fee_sat:
            raise InsufficientFunds("Insufficient funds in service wallet")
        self.service_balance_sat -= amount_sat + self.fee_sat
        for w in self.clients.values():
            if address in w.addresses:
                w.balance_sat += amount_sat
                w.tx_count += 1
        tx = _Tx(self._new_txid(), address, amount_sat)
        self.txs[tx.txid] = tx
        return TransferResult(tx.txid, self.fee_sat)

    # --- WalletHealth ---

    def health(self) -> dict:
        return {"backend": "memory", "chain": "memory", "blocks": self.height,
                "service_wallet": "memory", "service_balance_btc": str(sat_to_btc(self.service_balance_sat))}

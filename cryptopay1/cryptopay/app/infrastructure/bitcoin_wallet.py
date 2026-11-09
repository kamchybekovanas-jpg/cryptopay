"""Адаптер к open-source кошельку Bitcoin Core.

Как предметная область ложится на Bitcoin Core:
- клиент            = кошелёк на узле с именем  <prefix><client_id>  (по умолчанию client_alice);
- кошелёк сервиса   = кошелёк pay_service, сюда приходят оплаты и отсюда уходят возвраты;
- проверка «можно ли списать» = walletcreatefundedpsbt: Bitcoin Core собирает
  транзакцию и считает комиссию, но НИЧЕГО не отправляет;
- списание          = sendtoaddress из кошелька клиента на адрес сервиса.

Класс реализует несколько маленьких портов сразу (ClientDirectory,
FundsInspector, ...). Прикладной слой видит его только через эти порты.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from app.application.ports import (
    ClientDirectory, ClientInfo, DepositAddressProvider, FundsInspector, IncomingTransfer,
    PaymentExecutor, PaymentTracker, RefundExecutor, TransferResult, WalletHealth,
)
from app.domain.errors import InsufficientFunds, WalletError, WalletTimeout, WalletUnavailable
from app.domain.money import btc_to_sat, sat_to_btc
from app.infrastructure.bitcoin_rpc import BitcoinRpcClient, RpcConnectionError, RpcError, RpcTimeout

log = logging.getLogger(__name__)

# Коды ошибок Bitcoin Core (src/rpc/protocol.h)
RPC_WALLET_ERROR = -4
RPC_WALLET_INSUFFICIENT_FUNDS = -6
RPC_WALLET_UNLOCK_NEEDED = -13
RPC_WALLET_NOT_FOUND = -18
RPC_WALLET_ALREADY_LOADED = -35


def _is_insufficient(exc: RpcError) -> bool:
    return exc.code == RPC_WALLET_INSUFFICIENT_FUNDS or "insufficient funds" in exc.message.lower()


class BitcoinCoreWallet(ClientDirectory, FundsInspector, DepositAddressProvider, PaymentExecutor,
                        PaymentTracker, RefundExecutor, WalletHealth):
    def __init__(self, rpc: BitcoinRpcClient, service_wallet: str = "pay_service",
                 client_prefix: str = "client_"):
        self._rpc = rpc
        self._service_wallet = service_wallet
        self._prefix = client_prefix

    # --- служебное ---

    def wallet_name(self, client_id: str) -> str:
        return f"{self._prefix}{client_id}"

    def _call(self, method: str, params=None, wallet: str | None = None):
        """Переводит ошибки транспорта в ошибки кошелька предметной области."""
        try:
            return self._rpc.call(method, params, wallet=wallet)
        except RpcTimeout as exc:
            raise WalletTimeout(str(exc)) from exc
        except RpcConnectionError as exc:
            raise WalletError(str(exc)) from exc

    def _loaded_wallets(self) -> list[str]:
        return self._call("listwallets")

    def _ensure_loaded(self, name: str) -> None:
        if name in self._loaded_wallets():
            return
        try:
            self._call("loadwallet", [name])
        except RpcError as exc:
            if exc.code == RPC_WALLET_ALREADY_LOADED:
                return
            raise WalletUnavailable(exc.message) from exc

    def ensure_ready(self) -> None:
        """При старте: открыть или создать кошелёк сервиса."""
        try:
            self._ensure_loaded(self._service_wallet)
        except WalletUnavailable:
            log.info("Создаю кошелёк сервиса %s", self._service_wallet)
            self._call("createwallet", [self._service_wallet])

    # --- ClientDirectory: шаги 2–3 ---

    def client_exists(self, client_id: str) -> bool:
        name = self.wallet_name(client_id)
        try:
            on_disk = {w["name"] for w in self._call("listwalletdir")["wallets"]}
        except RpcError as exc:
            raise WalletError(exc.message) from exc
        return name in on_disk or name in self._loaded_wallets()

    def open_client_wallet(self, client_id: str) -> None:
        self._ensure_loaded(self.wallet_name(client_id))

    def get_client_info(self, client_id: str) -> ClientInfo:
        name = self.wallet_name(client_id)
        try:
            info = self._call("getwalletinfo", wallet=name)
            balances = self._call("getbalances", wallet=name)["mine"]
        except RpcError as exc:
            raise WalletUnavailable(exc.message) from exc
        return ClientInfo(
            client_id=client_id,
            wallet_name=info["walletname"],
            balance_sat=btc_to_sat(balances["trusted"]),
            unconfirmed_sat=btc_to_sat(balances["untrusted_pending"]),
            tx_count=int(info.get("txcount", 0)),
        )

    # --- FundsInspector: шаг 4 ---

    def spendable_balance(self, client_id: str) -> int:
        try:
            balances = self._call("getbalances", wallet=self.wallet_name(client_id))
        except RpcError as exc:
            raise WalletUnavailable(exc.message) from exc
        return btc_to_sat(balances["mine"]["trusted"])

    def estimate_fee(self, client_id: str, address: str, amount_sat: int) -> int:
        """Пробная сборка транзакции: деньги не уходят, но Bitcoin Core проверяет,
        что монет хватает, и считает комиссию."""
        try:
            psbt = self._call(
                "walletcreatefundedpsbt",
                {"inputs": [], "outputs": [{address: sat_to_btc(amount_sat)}]},
                wallet=self.wallet_name(client_id),
            )
        except RpcError as exc:
            if _is_insufficient(exc):
                raise InsufficientFunds(exc.message) from exc
            if exc.code == RPC_WALLET_UNLOCK_NEEDED:
                raise WalletUnavailable("Кошелёк клиента заблокирован паролем") from exc
            raise WalletError(exc.message) from exc
        return btc_to_sat(psbt["fee"])

    # --- DepositAddressProvider ---

    def new_deposit_address(self, label: str) -> str:
        try:
            return self._call("getnewaddress", [label], wallet=self._service_wallet)
        except RpcError as exc:
            raise WalletError(exc.message) from exc

    # --- PaymentExecutor: шаг 5 ---

    def _send(self, from_wallet: str, address: str, amount_sat: int, comment: str) -> TransferResult:
        try:
            txid = self._call(
                "sendtoaddress",
                {"address": address, "amount": sat_to_btc(amount_sat), "comment": comment,
                 "replaceable": False},
                wallet=from_wallet,
            )
        except RpcError as exc:
            if _is_insufficient(exc):
                raise InsufficientFunds(exc.message) from exc
            raise WalletError(exc.message) from exc
        fee_sat = None
        try:
            tx = self._call("gettransaction", [txid], wallet=from_wallet)
            if "fee" in tx:
                fee_sat = abs(btc_to_sat(tx["fee"]))
        except (RpcError, WalletError):
            log.warning("Не удалось узнать комиссию транзакции %s", txid)
        return TransferResult(txid=txid, fee_sat=fee_sat)

    def charge(self, client_id: str, address: str, amount_sat: int, comment: str) -> TransferResult:
        return self._send(self.wallet_name(client_id), address, amount_sat, comment)

    # --- PaymentTracker ---

    def find_incoming(self, address: str) -> IncomingTransfer | None:
        try:
            rows = self._call(
                "listreceivedbyaddress",
                {"minconf": 0, "include_empty": False, "address_filter": address},
                wallet=self._service_wallet,
            )
        except RpcError as exc:
            raise WalletError(exc.message) from exc
        for row in rows:
            if row["address"] == address and row.get("txids"):
                return IncomingTransfer(row["txids"][0], btc_to_sat(row["amount"]), int(row["confirmations"]))
        return None

    def get_confirmations(self, txid: str) -> int:
        try:
            tx = self._call("gettransaction", [txid], wallet=self._service_wallet)
        except RpcError as exc:
            raise WalletError(exc.message) from exc
        return max(int(tx.get("confirmations", 0)), 0)

    # --- RefundExecutor ---

    def new_client_address(self, client_id: str, label: str) -> str:
        name = self.wallet_name(client_id)
        self._ensure_loaded(name)
        try:
            return self._call("getnewaddress", [label], wallet=name)
        except RpcError as exc:
            raise WalletError(exc.message) from exc

    def send_refund(self, address: str, amount_sat: int, comment: str) -> TransferResult:
        return self._send(self._service_wallet, address, amount_sat, comment)

    # --- WalletHealth ---

    def health(self) -> dict:
        try:
            chain = self._call("getblockchaininfo")
            service = self._call("getbalances", wallet=self._service_wallet)["mine"]
        except RpcError as exc:
            raise WalletError(exc.message) from exc
        return {
            "backend": "bitcoin-core",
            "chain": chain["chain"],
            "blocks": chain["blocks"],
            "service_wallet": self._service_wallet,
            "service_balance_btc": str(Decimal(service["trusted"])),
        }


class BitcoinCoreDemoAdmin:
    """Только для демонстрации в regtest: создать клиента, пополнить, намайнить блок.
    В настоящей системе клиенты создают кошельки сами, а блоки майнит сеть."""

    MINER_WALLET = "miner"

    def __init__(self, rpc: BitcoinRpcClient, wallet: BitcoinCoreWallet):
        self._rpc = rpc
        self._wallet = wallet

    def _miner_address(self) -> str:
        try:
            self._wallet._ensure_loaded(self.MINER_WALLET)
        except WalletUnavailable:
            self._rpc.call("createwallet", [self.MINER_WALLET])
        return self._rpc.call("getnewaddress", [], wallet=self.MINER_WALLET)

    SERVICE_FEE_RESERVE = Decimal("0.01")

    def _ensure_service_reserve(self) -> None:
        """Кошелёк сервиса платит комиссию за возвраты, поэтому ему нужен небольшой резерв."""
        service = self._wallet._service_wallet
        balance = Decimal(self._rpc.call("getbalances", wallet=service)["mine"]["trusted"])
        if balance >= self.SERVICE_FEE_RESERVE:
            return
        self._miner_address()
        miner_balance = Decimal(self._rpc.call("getbalances", wallet=self.MINER_WALLET)["mine"]["trusted"])
        if miner_balance < Decimal("1"):
            self.mine(101)
        address = self._rpc.call("getnewaddress", ["fee-reserve"], wallet=service)
        self._rpc.call("sendtoaddress", [address, str(self.SERVICE_FEE_RESERVE * 10)], wallet=self.MINER_WALLET)
        self.mine(1)

    def mine(self, blocks: int = 1) -> dict:
        hashes = self._rpc.call("generatetoaddress", [blocks, self._miner_address()])
        return {"mined": len(hashes), "height": self._rpc.call("getblockcount")}

    def create_client(self, client_id: str, fund_btc: Decimal) -> dict:
        name = self._wallet.wallet_name(client_id)
        if not self._wallet.client_exists(client_id):
            self._rpc.call("createwallet", [name])
        else:
            self._wallet.open_client_wallet(client_id)
        self._ensure_service_reserve()
        txid = None
        if fund_btc > 0:
            self._miner_address()  # создаёт и открывает кошелёк майнера при первом вызове
            miner_balance = Decimal(self._rpc.call("getbalances", wallet=self.MINER_WALLET)["mine"]["trusted"])
            if miner_balance < fund_btc + Decimal("0.001"):
                self.mine(101)  # монеты из блока становятся доступны через 100 блоков
            address = self._rpc.call("getnewaddress", [], wallet=name)
            txid = self._rpc.call("sendtoaddress", [address, str(fund_btc)], wallet=self.MINER_WALLET)
            self.mine(1)  # подтверждаем пополнение
        return {"client_id": client_id, "wallet": name, "funding_txid": txid,
                "info": self._wallet.get_client_info(client_id)}

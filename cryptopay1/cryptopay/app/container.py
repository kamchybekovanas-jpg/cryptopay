"""Корень композиции: единственное место, где выбираются конкретные классы.

Здесь видно принцип D целиком: сервисы получают абстракции, а какой
кошелёк (Bitcoin Core или память) и какая БД будут за ними — решается
только тут, по настройкам. И принцип O: порядок и состав шагов оплаты и
правил возврата задаются списками.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.application.payment_service import PaymentService
from app.application.ports import WalletHealth
from app.application.refund_policy import (
    AmountRule, ConfirmationsRule, RefundPolicy, RefundWindowRule, StatusRule,
)
from app.application.refund_service import RefundService
from app.application.steps import (
    ChargeStep, ClientInfoStep, ClientLookupStep, DepositAddressStep, FundsCheckStep,
)
from app.config import Settings
from app.infrastructure.sqlite_repository import SqlitePaymentRepository


@dataclass
class Container:
    settings: Settings
    payments: PaymentService
    refunds: RefundService
    health: WalletHealth
    demo_admin: Any  # объект с методами create_client() и mine()


def build_container(settings: Settings, wallet=None, demo_admin=None) -> Container:
    if wallet is None:
        if settings.wallet_backend == "memory":
            from app.infrastructure.memory_wallet import InMemoryWallet
            wallet = InMemoryWallet()
            demo_admin = wallet
        else:
            from app.infrastructure.bitcoin_rpc import BitcoinRpcClient
            from app.infrastructure.bitcoin_wallet import BitcoinCoreDemoAdmin, BitcoinCoreWallet
            rpc = BitcoinRpcClient(settings.bitcoin_rpc_url, settings.bitcoin_rpc_user,
                                   settings.bitcoin_rpc_password, settings.bitcoin_rpc_timeout)
            wallet = BitcoinCoreWallet(rpc, settings.service_wallet, settings.client_wallet_prefix)
            wallet.ensure_ready()
            demo_admin = BitcoinCoreDemoAdmin(rpc, wallet)

    repository = SqlitePaymentRepository(settings.db_path)

    steps = [
        ClientLookupStep(wallet),                       # 2. есть ли клиент и кошелёк
        ClientInfoStep(wallet),                         # 3. данные клиента
        DepositAddressStep(wallet),                     #    адрес сервиса для оплаты
        FundsCheckStep(wallet, settings.max_fee_sat),   # 4. хватит ли денег
        ChargeStep(wallet),                             # 5. списание
    ]
    payments = PaymentService(repository, steps, tracker=wallet)

    policy = RefundPolicy([
        StatusRule(),
        ConfirmationsRule(settings.refund_min_confirmations),
        RefundWindowRule(settings.refund_window_days),
        AmountRule(settings.min_refund_sat),
    ])
    refunds = RefundService(repository, payments, executor=wallet, policy=policy)

    return Container(settings=settings, payments=payments, refunds=refunds,
                     health=wallet, demo_admin=demo_admin)

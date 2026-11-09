"""Настройки из переменных окружения (файл .env читает systemd или вы сами)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _api_keys(raw: str) -> dict[str, str]:
    """"shop1:key1,shop2:key2" -> {"key1": "shop1", "key2": "shop2"}"""
    keys = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        merchant, _, key = pair.partition(":")
        if merchant and key:
            keys[key] = merchant
    return keys


@dataclass
class Settings:
    wallet_backend: str = "bitcoin"            # bitcoin | memory
    bitcoin_rpc_url: str = "http://127.0.0.1:18443"
    bitcoin_rpc_user: str = "cryptopay"
    bitcoin_rpc_password: str = "change-me"
    bitcoin_rpc_timeout: float = 30.0
    service_wallet: str = "pay_service"
    client_wallet_prefix: str = "client_"
    db_path: str = "cryptopay.db"
    api_keys: dict[str, str] = field(default_factory=lambda: {"demo-key": "demo-shop"})
    max_fee_sat: int = 10_000                  # предел комиссии сети на одно списание
    refund_min_confirmations: int = 1
    refund_window_days: int = 30
    min_refund_sat: int = 1_000
    demo_mode: bool = True                     # демо-эндпоинты /api/v1/demo/*

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        d = cls()
        return cls(
            wallet_backend=env("WALLET_BACKEND", d.wallet_backend),
            bitcoin_rpc_url=env("BITCOIN_RPC_URL", d.bitcoin_rpc_url),
            bitcoin_rpc_user=env("BITCOIN_RPC_USER", d.bitcoin_rpc_user),
            bitcoin_rpc_password=env("BITCOIN_RPC_PASSWORD", d.bitcoin_rpc_password),
            bitcoin_rpc_timeout=float(env("BITCOIN_RPC_TIMEOUT", d.bitcoin_rpc_timeout)),
            service_wallet=env("SERVICE_WALLET", d.service_wallet),
            client_wallet_prefix=env("CLIENT_WALLET_PREFIX", d.client_wallet_prefix),
            db_path=env("DB_PATH", d.db_path),
            api_keys=_api_keys(env("API_KEYS", "demo-shop:demo-key")),
            max_fee_sat=int(env("MAX_FEE_SAT", d.max_fee_sat)),
            refund_min_confirmations=int(env("REFUND_MIN_CONFIRMATIONS", d.refund_min_confirmations)),
            refund_window_days=int(env("REFUND_WINDOW_DAYS", d.refund_window_days)),
            min_refund_sat=int(env("MIN_REFUND_SAT", d.min_refund_sat)),
            demo_mode=_bool(env("DEMO_MODE"), d.demo_mode),
        )

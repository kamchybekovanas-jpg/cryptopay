"""Минимальный JSON-RPC клиент Bitcoin Core (только стандартная библиотека).

Отвечает только за транспорт: отправить метод, получить результат или
ошибку (принцип S). Бизнес-смысл вызовов — в bitcoin_wallet.py.
"""
from __future__ import annotations

import base64
import itertools
import json
import socket
import urllib.error
import urllib.request
from decimal import Decimal
from urllib.parse import quote

from app.domain.errors import WalletError, WalletTimeout


class RpcError(WalletError):
    """Bitcoin Core вернул ошибку (например, -18 «кошелёк не найден»)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class RpcConnectionError(WalletError):
    """Узел недоступен."""


class RpcTimeout(RpcConnectionError, WalletTimeout):
    """Узел не ответил за отведённое время."""


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Не сериализуется в JSON: {type(value)}")


class BitcoinRpcClient:
    def __init__(self, url: str, user: str, password: str, timeout: float = 30.0):
        self._url = url.rstrip("/")
        self._timeout = timeout
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._headers = {"Content-Type": "application/json", "Authorization": f"Basic {token}"}
        self._ids = itertools.count(1)

    def call(self, method: str, params: list | dict | None = None, wallet: str | None = None):
        """Вызов RPC. params — список (по позиции) или словарь (по имени).
        wallet — имя кошелька: запрос уйдёт на /wallet/<имя>."""
        url = self._url + (f"/wallet/{quote(wallet, safe='')}" if wallet else "")
        body = json.dumps(
            {"jsonrpc": "1.0", "id": next(self._ids), "method": method,
             "params": params if params is not None else []},
            default=_json_default,
        ).encode()
        request = urllib.request.Request(url, data=body, headers=self._headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Bitcoin Core отдаёт ошибки RPC с кодом 500/404 и JSON в теле
            raw = exc.read()
            if not raw:
                raise RpcConnectionError(f"HTTP {exc.code} от Bitcoin Core (проверьте логин/пароль RPC)") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise RpcTimeout(f"Bitcoin Core не ответил за {self._timeout} с ({method})") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise RpcTimeout(f"Bitcoin Core не ответил за {self._timeout} с ({method})") from exc
            raise RpcConnectionError(f"Bitcoin Core недоступен: {exc.reason}") from exc

        payload = json.loads(raw, parse_float=Decimal)
        error = payload.get("error")
        if error:
            raise RpcError(error.get("code", -1), error.get("message", "unknown error"))
        return payload.get("result")

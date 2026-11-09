"""Перевод сумм между BTC и сатоши.

Внутри сервиса все суммы хранятся в сатоши (целое число), чтобы не было
ошибок округления float. BTC (Decimal) используется только на границе:
в API и в вызовах Bitcoin Core.
"""
from decimal import Decimal, InvalidOperation

SATOSHI_PER_BTC = 100_000_000
MAX_SUPPLY_SAT = 21_000_000 * SATOSHI_PER_BTC


def btc_to_sat(btc: Decimal | str | int) -> int:
    """Decimal('0.0015') -> 150000. Больше 8 знаков после запятой — ошибка."""
    try:
        value = Decimal(str(btc))
    except InvalidOperation as exc:
        raise ValueError(f"Некорректная сумма: {btc!r}") from exc
    sat = value * SATOSHI_PER_BTC
    if sat != sat.to_integral_value():
        raise ValueError("Сумма не может содержать больше 8 знаков после запятой")
    return int(sat)


def sat_to_btc(sat: int) -> Decimal:
    """150000 -> Decimal('0.00150000')."""
    return (Decimal(sat) / SATOSHI_PER_BTC).quantize(Decimal("0.00000001"))

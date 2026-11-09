"""Ошибки предметной области и ошибки кошелька.

Ошибки кошелька объявлены здесь, а не в инфраструктуре: прикладной слой
ловит их, не зная, какой именно кошелёк стоит за интерфейсом (принцип D).
"""


class DomainError(Exception):
    """Базовая ошибка бизнес-правил."""


class InvalidTransition(DomainError):
    def __init__(self, current, target):
        super().__init__(f"Недопустимый переход статуса: {current.value} -> {target.value}")
        self.current = current
        self.target = target


class PaymentNotFound(DomainError):
    def __init__(self, payment_id: str):
        super().__init__(f"Платёж {payment_id} не найден")
        self.payment_id = payment_id


class IdempotencyConflict(DomainError):
    """Тот же order_id пришёл повторно, но с другими параметрами."""


class RefundNotAllowed(DomainError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- Ошибки кошелька (общие для любой реализации кошелька) ---

class WalletError(Exception):
    """Кошелёк вернул ошибку."""


class WalletUnavailable(WalletError):
    """Кошелёк клиента есть, но его нельзя открыть или использовать."""


class InsufficientFunds(WalletError):
    """В кошельке недостаточно средств для списания с учётом комиссии."""


class WalletTimeout(WalletError):
    """Кошелёк не ответил вовремя. Результат операции неизвестен."""

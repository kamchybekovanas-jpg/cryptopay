"""HTTP-эндпоинты. Только приём запроса, вызов сервиса и форматирование ответа —
никакой бизнес-логики (принцип S)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.security import APIKeyHeader

from app.api.schemas import (
    CreatePaymentRequest, CreateRefundRequest, DemoClientRequest, DemoMineRequest, PaymentOut, RefundOut,
)
from app.application.payment_service import CreatePaymentCommand
from app.container import Container
from app.domain.money import btc_to_sat, sat_to_btc

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_merchant(api_key: str | None = Depends(api_key_header),
                 container: Container = Depends(get_container)) -> str:
    """Определяет мерчанта по ключу. Нет ключа или ключ неверный — 401."""
    merchant = container.settings.api_keys.get(api_key or "")
    if merchant is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "UNAUTHORIZED", "message": "Неверный или отсутствующий X-API-Key"})
    return merchant


router = APIRouter(prefix="/api/v1")


@router.get("/health", tags=["service"], summary="Сервис и узел Bitcoin Core живы?")
def health(container: Container = Depends(get_container)):
    return {"status": "ok", "wallet": container.health.health()}


@router.post("/payments", response_model=PaymentOut, status_code=status.HTTP_201_CREATED,
             tags=["payments"], summary="Списать оплату с кошелька клиента")
def create_payment(body: CreatePaymentRequest, response: Response,
                   merchant: str = Depends(get_merchant), container: Container = Depends(get_container)):
    payment, created = container.payments.create_payment(CreatePaymentCommand(
        merchant_id=merchant, order_id=body.order_id, client_id=body.client_id,
        amount_sat=btc_to_sat(body.amount), description=body.description,
    ))
    if not created:
        response.status_code = status.HTTP_200_OK  # повтор запроса: деньги второй раз не списываются
    return PaymentOut.from_domain(payment)


@router.get("/payments", response_model=list[PaymentOut], tags=["payments"], summary="Последние платежи мерчанта")
def list_payments(limit: int = Query(20, ge=1, le=100),
                  merchant: str = Depends(get_merchant), container: Container = Depends(get_container)):
    return [PaymentOut.from_domain(p) for p in container.payments.list_payments(merchant, limit)]


@router.get("/payments/{payment_id}", response_model=PaymentOut, tags=["payments"],
            summary="Статус платежа (со сверкой подтверждений)")
def get_payment(payment_id: str, merchant: str = Depends(get_merchant),
                container: Container = Depends(get_container)):
    return PaymentOut.from_domain(container.payments.get_payment(merchant, payment_id))


@router.post("/payments/{payment_id}/refunds", response_model=RefundOut, status_code=status.HTTP_201_CREATED,
             tags=["refunds"], summary="Вернуть деньги клиенту (полностью или частично)")
def create_refund(payment_id: str, body: CreateRefundRequest, response: Response,
                  merchant: str = Depends(get_merchant), container: Container = Depends(get_container)):
    refund, created = container.refunds.create_refund(
        merchant, payment_id,
        amount_sat=btc_to_sat(body.amount) if body.amount is not None else None,
        reason=body.reason, idempotency_key=body.idempotency_key,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return RefundOut.from_domain(refund)


@router.get("/payments/{payment_id}/refunds", response_model=list[RefundOut], tags=["refunds"],
            summary="Возвраты по платежу")
def list_refunds(payment_id: str, merchant: str = Depends(get_merchant),
                 container: Container = Depends(get_container)):
    return [RefundOut.from_domain(r) for r in container.refunds.list_refunds(merchant, payment_id)]


# --- Демо-эндпоинты (DEMO_MODE=true): только для показа в regtest ---

demo_router = APIRouter(prefix="/api/v1/demo", tags=["demo (regtest)"])


@demo_router.post("/clients", summary="Создать кошелёк клиента и пополнить его")
def demo_create_client(body: DemoClientRequest, merchant: str = Depends(get_merchant),
                       container: Container = Depends(get_container)):
    result = container.demo_admin.create_client(body.client_id, body.fund_btc)
    info = result["info"]
    return {
        "client_id": result["client_id"], "wallet": result["wallet"], "funding_txid": result["funding_txid"],
        "balance_btc": sat_to_btc(info.balance_sat), "unconfirmed_btc": sat_to_btc(info.unconfirmed_sat),
    }


@demo_router.post("/mine", summary="Намайнить блоки (подтвердить транзакции)")
def demo_mine(body: DemoMineRequest, merchant: str = Depends(get_merchant),
              container: Container = Depends(get_container)):
    return container.demo_admin.mine(body.blocks)

"""Точка входа: uvicorn app.main:app

Swagger (интерактивная документация для демонстрации): http://<сервер>:8000/docs
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import demo_router, router
from app.config import Settings
from app.container import Container, build_container
from app.domain.errors import IdempotencyConflict, PaymentNotFound, RefundNotAllowed, WalletError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or (container.settings if container else Settings.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container or build_container(settings)
        yield

    app = FastAPI(
        title="CryptoPay",
        version="1.0.0",
        description="Платёжный сервис: приём оплаты в биткоинах через кошелёк Bitcoin Core (regtest).",
        lifespan=lifespan,
    )
    app.include_router(router)
    if settings.demo_mode:
        app.include_router(demo_router)

    @app.exception_handler(PaymentNotFound)
    async def _not_found(request: Request, exc: PaymentNotFound):
        return _error(404, "PAYMENT_NOT_FOUND", str(exc))

    @app.exception_handler(IdempotencyConflict)
    async def _conflict(request: Request, exc: IdempotencyConflict):
        return _error(409, "IDEMPOTENCY_CONFLICT", str(exc))

    @app.exception_handler(RefundNotAllowed)
    async def _refund_not_allowed(request: Request, exc: RefundNotAllowed):
        return _error(422, exc.code, exc.message)

    @app.exception_handler(WalletError)
    async def _wallet_error(request: Request, exc: WalletError):
        return _error(503, "WALLET_UNAVAILABLE", f"Кошелёк недоступен: {exc}")

    @app.exception_handler(ValueError)
    async def _value_error(request: Request, exc: ValueError):
        return _error(400, "BAD_REQUEST", str(exc))

    @app.get("/", include_in_schema=False)
    def root():
        return {"service": "CryptoPay", "docs": "/docs", "health": "/api/v1/health"}

    return app


app = create_app()

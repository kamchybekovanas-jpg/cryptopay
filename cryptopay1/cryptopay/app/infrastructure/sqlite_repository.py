"""Хранилище платежей и возвратов в SQLite (встроено в Python, сервер БД не нужен).

Чтобы перейти на PostgreSQL, достаточно написать новый класс с тем же
интерфейсом PaymentRepository — сервисы менять не придётся (принципы O и D).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime

from app.application.ports import PaymentRepository
from app.domain.models import (
    FailureReason, Payment, PaymentStatus, Refund, RefundStatus, StatusChange,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    amount_sat INTEGER NOT NULL CHECK (amount_sat > 0),
    description TEXT,
    status TEXT NOT NULL,
    failure_reason TEXT,
    failure_message TEXT,
    deposit_address TEXT,
    txid TEXT,
    fee_sat INTEGER,
    confirmations INTEGER NOT NULL DEFAULT 0,
    refunded_sat INTEGER NOT NULL DEFAULT 0,
    history TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (merchant_id, order_id)
);
CREATE TABLE IF NOT EXISTS refunds (
    id TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL REFERENCES payments(id),
    amount_sat INTEGER NOT NULL CHECK (amount_sat > 0),
    reason TEXT,
    idempotency_key TEXT,
    status TEXT NOT NULL,
    destination_address TEXT,
    txid TEXT,
    fee_sat INTEGER,
    failure_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (payment_id, idempotency_key)
);
"""


class SqlitePaymentRepository(PaymentRepository):
    def __init__(self, path: str = "cryptopay.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    # --- преобразование строк ---

    @staticmethod
    def _payment_values(p: Payment) -> dict:
        return {
            "id": p.id, "merchant_id": p.merchant_id, "order_id": p.order_id, "client_id": p.client_id,
            "amount_sat": p.amount_sat, "description": p.description, "status": p.status.value,
            "failure_reason": p.failure_reason.value if p.failure_reason else None,
            "failure_message": p.failure_message, "deposit_address": p.deposit_address,
            "txid": p.txid, "fee_sat": p.fee_sat, "confirmations": p.confirmations,
            "refunded_sat": p.refunded_sat,
            "history": json.dumps([
                {"status": h.status.value, "at": h.at.isoformat(), "note": h.note} for h in p.history
            ], ensure_ascii=False),
            "created_at": p.created_at.isoformat(), "updated_at": p.updated_at.isoformat(),
        }

    @staticmethod
    def _to_payment(row: sqlite3.Row) -> Payment:
        return Payment(
            id=row["id"], merchant_id=row["merchant_id"], order_id=row["order_id"],
            client_id=row["client_id"], amount_sat=row["amount_sat"], description=row["description"],
            status=PaymentStatus(row["status"]),
            failure_reason=FailureReason(row["failure_reason"]) if row["failure_reason"] else None,
            failure_message=row["failure_message"], deposit_address=row["deposit_address"],
            txid=row["txid"], fee_sat=row["fee_sat"], confirmations=row["confirmations"],
            refunded_sat=row["refunded_sat"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            history=[
                StatusChange(PaymentStatus(h["status"]), datetime.fromisoformat(h["at"]), h["note"])
                for h in json.loads(row["history"])
            ],
        )

    @staticmethod
    def _refund_values(r: Refund) -> dict:
        return {
            "id": r.id, "payment_id": r.payment_id, "amount_sat": r.amount_sat, "reason": r.reason,
            "idempotency_key": r.idempotency_key, "status": r.status.value,
            "destination_address": r.destination_address, "txid": r.txid, "fee_sat": r.fee_sat,
            "failure_message": r.failure_message,
            "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat(),
        }

    @staticmethod
    def _to_refund(row: sqlite3.Row) -> Refund:
        return Refund(
            id=row["id"], payment_id=row["payment_id"], amount_sat=row["amount_sat"], reason=row["reason"],
            idempotency_key=row["idempotency_key"], status=RefundStatus(row["status"]),
            destination_address=row["destination_address"], txid=row["txid"], fee_sat=row["fee_sat"],
            failure_message=row["failure_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _execute(self, sql: str, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _fetchone(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- платежи ---

    def add(self, payment: Payment) -> None:
        v = self._payment_values(payment)
        cols = ", ".join(v)
        self._execute(f"INSERT INTO payments ({cols}) VALUES ({', '.join(':' + k for k in v)})", v)

    def update(self, payment: Payment) -> None:
        v = self._payment_values(payment)
        sets = ", ".join(f"{k} = :{k}" for k in v if k != "id")
        self._execute(f"UPDATE payments SET {sets} WHERE id = :id", v)

    def get(self, payment_id: str) -> Payment | None:
        row = self._fetchone("SELECT * FROM payments WHERE id = ?", (payment_id,))
        return self._to_payment(row) if row else None

    def get_by_order(self, merchant_id: str, order_id: str) -> Payment | None:
        row = self._fetchone("SELECT * FROM payments WHERE merchant_id = ? AND order_id = ?",
                             (merchant_id, order_id))
        return self._to_payment(row) if row else None

    def list_by_merchant(self, merchant_id: str, limit: int = 50) -> list[Payment]:
        rows = self._fetchall(
            "SELECT * FROM payments WHERE merchant_id = ? ORDER BY created_at DESC LIMIT ?",
            (merchant_id, limit),
        )
        return [self._to_payment(r) for r in rows]

    # --- возвраты ---

    def add_refund(self, refund: Refund) -> None:
        v = self._refund_values(refund)
        cols = ", ".join(v)
        self._execute(f"INSERT INTO refunds ({cols}) VALUES ({', '.join(':' + k for k in v)})", v)

    def update_refund(self, refund: Refund) -> None:
        v = self._refund_values(refund)
        sets = ", ".join(f"{k} = :{k}" for k in v if k != "id")
        self._execute(f"UPDATE refunds SET {sets} WHERE id = :id", v)

    def list_refunds(self, payment_id: str) -> list[Refund]:
        rows = self._fetchall("SELECT * FROM refunds WHERE payment_id = ? ORDER BY created_at", (payment_id,))
        return [self._to_refund(r) for r in rows]

    def get_refund_by_key(self, payment_id: str, key: str) -> Refund | None:
        row = self._fetchone("SELECT * FROM refunds WHERE payment_id = ? AND idempotency_key = ?",
                             (payment_id, key))
        return self._to_refund(row) if row else None

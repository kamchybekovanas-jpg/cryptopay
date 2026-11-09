#!/usr/bin/env bash
# Сценарий демонстрации на защите. Нужны только curl и python3.
#   BASE=http://<ip>:8000 KEY=<api-key> bash scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
KEY="${KEY:-demo-key}"
RUN="$(date +%s)"   # уникальный суффикс, чтобы скрипт можно было запускать много раз

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
post() { curl -s -X POST "$BASE$1" -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d "$2"; }
get()  { curl -s "$BASE$1" -H "X-API-Key: $KEY"; }
pretty() { python3 -m json.tool --no-ensure-ascii; }
field()  { python3 -c "import sys, json; print(json.load(sys.stdin)$1)"; }

say "0. Сервис жив и подключён к Bitcoin Core"
curl -s "$BASE/api/v1/health" | pretty

say "1. Создаём клиентов в кошельке: alice (1 BTC) и bob (0.0001 BTC)"
post /api/v1/demo/clients '{"client_id":"alice","fund_btc":"1"}' | pretty
post /api/v1/demo/clients '{"client_id":"bob","fund_btc":"0.0001"}' | pretty

say "2. Магазин просит списать 0.0015 BTC с alice"
PAYMENT="$(post /api/v1/payments "{\"order_id\":\"order-$RUN\",\"client_id\":\"alice\",\"amount\":\"0.0015\",\"description\":\"Заказ $RUN\"}")"
echo "$PAYMENT" | pretty
PAYMENT_ID="$(echo "$PAYMENT" | field "['id']")"

say "3. Повтор того же запроса (сеть моргнула) — деньги второй раз НЕ списываются"
post /api/v1/payments "{\"order_id\":\"order-$RUN\",\"client_id\":\"alice\",\"amount\":\"0.0015\"}" | field "['id'], '<- тот же id'"

say "4. У bob не хватает денег"
post /api/v1/payments "{\"order_id\":\"order-$RUN-bob\",\"client_id\":\"bob\",\"amount\":\"0.01\"}" | pretty

say "5. Клиента ghost нет в кошельке"
post /api/v1/payments "{\"order_id\":\"order-$RUN-ghost\",\"client_id\":\"ghost\",\"amount\":\"0.001\"}" | pretty

say "6. Возврат до подтверждения транзакции — запрещён"
post "/api/v1/payments/$PAYMENT_ID/refunds" '{"reason":"Передумал"}' | pretty

say "7. Майним 1 блок — транзакция оплаты подтверждается"
post /api/v1/demo/mine '{"blocks":1}' | pretty

say "8. Частичный возврат 0.0005 BTC"
post "/api/v1/payments/$PAYMENT_ID/refunds" "{\"amount\":\"0.0005\",\"reason\":\"Не подошёл размер\",\"idempotency_key\":\"refund-$RUN\"}" | pretty

say "9. Итоговый статус платежа"
get "/api/v1/payments/$PAYMENT_ID" | pretty

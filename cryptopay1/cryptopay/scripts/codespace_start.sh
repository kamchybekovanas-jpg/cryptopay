#!/usr/bin/env bash
# Запуск CryptoPay + Bitcoin Core (regtest) в GitHub Codespaces.
# Запускается сам при каждом старте Codespace. Вручную: bash scripts/codespace_start.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA=/workspaces/.cryptopay-data          # хранится между перезапусками Codespace
BITCOIN_VERSION="${BITCOIN_VERSION:-28.1}"
CLI() { "$DATA/bin/bitcoin-cli" -conf="$DATA/bitcoin.conf" -datadir="$DATA/bitcoin" "$@"; }
mkdir -p "$DATA/bitcoin" "$DATA/bin"

# 1. Bitcoin Core (скачивается один раз)
if [[ ! -x "$DATA/bin/bitcoind" ]]; then
  echo "==> Скачиваю Bitcoin Core $BITCOIN_VERSION"
  TMP="$(mktemp -d)"
  T="bitcoin-${BITCOIN_VERSION}-x86_64-linux-gnu.tar.gz"
  BASE="https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}"
  (cd "$TMP" && curl -fsSLO "$BASE/$T" && curl -fsSLO "$BASE/SHA256SUMS" \
     && sha256sum --ignore-missing --check SHA256SUMS && tar xzf "$T")
  cp "$TMP/bitcoin-${BITCOIN_VERSION}/bin/bitcoind" "$TMP/bitcoin-${BITCOIN_VERSION}/bin/bitcoin-cli" "$DATA/bin/"
  rm -rf "$TMP"
fi

# 2. Пароль RPC и API-ключ (создаются один раз и не меняются)
if [[ ! -f "$DATA/secrets" ]]; then
  {
    echo "RPC_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    echo "API_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  } > "$DATA/secrets"
fi
# shellcheck disable=SC1091
source "$DATA/secrets"
sed "s/^rpcpassword=.*/rpcpassword=${RPC_PASSWORD}/" "$REPO_DIR/deploy/bitcoin.conf" > "$DATA/bitcoin.conf"

# 3. Узел Bitcoin Core
if ! CLI getblockcount >/dev/null 2>&1; then
  echo "==> Запускаю Bitcoin Core (regtest)"
  "$DATA/bin/bitcoind" -conf="$DATA/bitcoin.conf" -datadir="$DATA/bitcoin" -daemon
  for _ in $(seq 1 60); do CLI getblockcount >/dev/null 2>&1 && break; sleep 1; done
fi

# 4. Python-окружение (один раз)
if [[ ! -x "$DATA/venv/bin/uvicorn" ]]; then
  echo "==> Устанавливаю Python-зависимости"
  python3 -m venv "$DATA/venv"
  "$DATA/venv/bin/pip" install -q --upgrade pip
  "$DATA/venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
fi

# 5. Сервис CryptoPay
cat > "$DATA/env" <<ENV
WALLET_BACKEND=bitcoin
BITCOIN_RPC_URL=http://127.0.0.1:18443
BITCOIN_RPC_USER=cryptopay
BITCOIN_RPC_PASSWORD=${RPC_PASSWORD}
DB_PATH=${DATA}/cryptopay.db
API_KEYS=demo-shop:${API_KEY}
DEMO_MODE=true
ENV
if command -v pkill >/dev/null; then pkill -f "uvicorn app.main:app" || true; sleep 1; fi
echo "==> Запускаю CryptoPay"
(
  cd "$REPO_DIR"
  set -a; source "$DATA/env"; set +a
  setsid nohup "$DATA/venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port 8000 \
    > "$DATA/cryptopay.log" 2>&1 < /dev/null &
)
for _ in $(seq 1 30); do curl -fs localhost:8000/api/v1/health >/dev/null 2>&1 && break; sleep 1; done

# 6. Сделать порт 8000 публичным (если не получится — вручную во вкладке PORTS)
if [[ -n "${CODESPACE_NAME:-}" ]] && command -v gh >/dev/null; then
  gh codespace ports visibility 8000:public -c "$CODESPACE_NAME" >/dev/null 2>&1 || true
fi

echo
if curl -fs localhost:8000/api/v1/health >/dev/null 2>&1; then
  echo "CryptoPay работает."
else
  echo "Сервис не ответил. Лог: tail -n 50 $DATA/cryptopay.log"
fi
if [[ -n "${CODESPACE_NAME:-}" ]]; then
  echo "  Swagger:  https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}/docs"
fi
echo "  API-ключ: ${API_KEY}"
echo "  Лог:      tail -f $DATA/cryptopay.log"

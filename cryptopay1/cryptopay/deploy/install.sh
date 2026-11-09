#!/usr/bin/env bash
# Установка CryptoPay + Bitcoin Core (regtest) на чистый Ubuntu 22.04/24.04.
# Запуск (от root), когда проект уже лежит в /opt/cryptopay:
#   bash /opt/cryptopay/deploy/install.sh
set -euo pipefail
# не задавать вопросов во время установки пакетов
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

BITCOIN_VERSION="${BITCOIN_VERSION:-28.1}"
APP_DIR=/opt/cryptopay
ARCH="$(uname -m)"   # x86_64 или aarch64

if [[ $EUID -ne 0 ]]; then echo "Запустите от root: sudo bash $0"; exit 1; fi
if [[ ! -f "$APP_DIR/app/main.py" ]]; then echo "Сначала скопируйте проект в $APP_DIR"; exit 1; fi

echo "==> Пакеты"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl openssl

echo "==> Bitcoin Core $BITCOIN_VERSION"
if ! command -v bitcoind >/dev/null; then
  TMP="$(mktemp -d)"; cd "$TMP"
  TARBALL="bitcoin-${BITCOIN_VERSION}-${ARCH}-linux-gnu.tar.gz"
  BASE="https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}"
  curl -fLO "$BASE/$TARBALL"
  curl -fLO "$BASE/SHA256SUMS"
  sha256sum --ignore-missing --check SHA256SUMS   # проверка целостности архива
  tar xzf "$TARBALL"
  install -m 0755 -t /usr/local/bin "bitcoin-${BITCOIN_VERSION}/bin/bitcoind" "bitcoin-${BITCOIN_VERSION}/bin/bitcoin-cli"
  cd /; rm -rf "$TMP"
fi

id bitcoin >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/bitcoind --shell /usr/sbin/nologin bitcoin
id cryptopay >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin cryptopay

RPC_PASSWORD="$(openssl rand -hex 16)"
API_KEY="$(openssl rand -hex 16)"

mkdir -p /etc/bitcoin
sed "s/^rpcpassword=.*/rpcpassword=${RPC_PASSWORD}/" "$APP_DIR/deploy/bitcoin.conf" > /etc/bitcoin/bitcoin.conf
chown root:bitcoin /etc/bitcoin/bitcoin.conf
chmod 640 /etc/bitcoin/bitcoin.conf

echo "==> Python-окружение"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

cat > "$APP_DIR/.env" <<EOF
WALLET_BACKEND=bitcoin
BITCOIN_RPC_URL=http://127.0.0.1:18443
BITCOIN_RPC_USER=cryptopay
BITCOIN_RPC_PASSWORD=${RPC_PASSWORD}
SERVICE_WALLET=pay_service
CLIENT_WALLET_PREFIX=client_
DB_PATH=/var/lib/cryptopay/cryptopay.db
API_KEYS=demo-shop:${API_KEY}
MAX_FEE_SAT=10000
REFUND_MIN_CONFIRMATIONS=1
REFUND_WINDOW_DAYS=30
MIN_REFUND_SAT=1000
DEMO_MODE=true
EOF
chown root:cryptopay "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"

echo "==> systemd"
cp "$APP_DIR/deploy/bitcoind.service" /etc/systemd/system/bitcoind.service
cp "$APP_DIR/deploy/cryptopay.service" /etc/systemd/system/cryptopay.service
systemctl daemon-reload
systemctl enable --now bitcoind
sleep 5
systemctl enable --now cryptopay

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 8000/tcp
fi

IP="$(curl -fs4 https://ifconfig.me || hostname -I | awk '{print $1}')"
echo
echo "Готово."
echo "  Swagger:  http://${IP}:8000/docs"
echo "  API-ключ: ${API_KEY}   (заголовок X-API-Key)"
echo "  Демо:     BASE=http://${IP}:8000 KEY=${API_KEY} bash $APP_DIR/scripts/demo.sh"
echo "  Логи:     journalctl -u cryptopay -f"

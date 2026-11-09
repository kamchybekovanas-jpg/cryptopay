# CryptoPay — платёжный сервис с приёмом оплаты в биткоинах

Бэкенд-сервис (имитация платёжного шлюза). Внешний сервис, например интернет-магазин,
присылает запрос «спиши с моего клиента N BTC», CryptoPay проверяет клиента и баланс
в open-source кошельке **Bitcoin Core** (режим regtest), списывает деньги и отвечает,
прошла ли оплата. Есть возврат (refund), полный и частичный.

## Как устроен один платёж

| Шаг из ТЗ | Класс | Вызов Bitcoin Core | Статус после шага |
|---|---|---|---|
| 1. Запрос от сервиса | `routes.create_payment` | — | `CREATED` |
| 2. Есть ли клиент и кошелёк | `ClientLookupStep` | `listwalletdir`, `loadwallet` | `CLIENT_FOUND` |
| 3. Данные клиента | `ClientInfoStep` | `getwalletinfo`, `getbalances` | `CLIENT_VERIFIED` |
| — адрес для оплаты | `DepositAddressStep` | `getnewaddress` (кошелёк сервиса) | без изменений |
| 4. Хватит ли денег, можно ли списать | `FundsCheckStep` | `getbalances`, `walletcreatefundedpsbt` (пробная сборка, без отправки) | `FUNDS_CONFIRMED` |
| 5. Списание | `ChargeStep` | `sendtoaddress` (кошелёк клиента → сервис) | `PROCESSING` → `SUCCEEDED` |
| 6. Ответ | `PaymentOut` | — | `message`: «Деньги успешно списаны» или причина |

Клиент = кошелёк на узле с именем `client_<id>`. Кошелёк сервиса = `pay_service`.

## Структура

```
app/
  domain/          сущности и правила: Payment, Refund, статусы, переходы (без зависимостей)
  application/     сценарии: PaymentService, RefundService, шаги оплаты, правила возврата, порты (интерфейсы)
  infrastructure/  адаптеры: Bitcoin Core (JSON-RPC), кошелёк в памяти, SQLite
  api/             FastAPI: эндпоинты и схемы
  container.py     сборка зависимостей (здесь выбирается кошелёк и БД)
  main.py          точка входа uvicorn
tests/             тесты: домен, сценарии, адаптер Bitcoin Core (на имитации RPC), API
deploy/            bitcoin.conf, systemd-юниты, install.sh
scripts/demo.sh    сценарий демонстрации
docs/diagrams/     четыре диаграммы (Mermaid + PNG)
```

## Быстрый старт на своём компьютере (без Bitcoin Core)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q
WALLET_BACKEND=memory DB_PATH=:memory: uvicorn app.main:app --reload
# открыть http://127.0.0.1:8000/docs, ключ: demo-key
```

## Запуск в GitHub Codespaces (бесплатно, без карты)

1. Загрузите проект в репозиторий GitHub.
2. Code → Codespaces → Create codespace on main.
3. Сервис запустится сам (`scripts/codespace_start.sh`), в терминале появятся ссылка на Swagger и API-ключ.
4. Вкладка PORTS → порт 8000 → правой кнопкой → Port Visibility → Public.

## Развёртывание на сервере (DigitalOcean, Ubuntu 22.04/24.04)

1. Создайте Droplet (Ubuntu, самый дешёвый план, 1 GB RAM хватает: regtest почти не занимает места).
2. Скопируйте проект на сервер:
   ```bash
   scp -r cryptopay root@<IP>:/opt/cryptopay
   ```
3. Установите всё одной командой:
   ```bash
   ssh root@<IP>
   bash /opt/cryptopay/deploy/install.sh
   ```
   Скрипт ставит Bitcoin Core (с проверкой SHA256), Python-окружение, создаёт
   systemd-сервисы `bitcoind` и `cryptopay`, генерирует пароль RPC и API-ключ и печатает их.
4. Проверка:
   ```bash
   curl http://<IP>:8000/api/v1/health
   BASE=http://<IP>:8000 KEY=<ключ> bash /opt/cryptopay/scripts/demo.sh
   ```

Полезные команды на сервере:

```bash
journalctl -u cryptopay -f                       # логи сервиса
systemctl restart cryptopay                      # перезапуск
alias btc='sudo -u bitcoin bitcoin-cli -conf=/etc/bitcoin/bitcoin.conf -datadir=/var/lib/bitcoind'
btc listwallets                                  # кошельки на узле
btc -rpcwallet=client_alice getbalances          # баланс клиента прямо в Bitcoin Core
btc -rpcwallet=pay_service getbalances           # баланс кошелька сервиса
```

## API

Все запросы, кроме `/health`, требуют заголовок `X-API-Key`.

| Метод | Путь | Что делает |
|---|---|---|
| GET | `/api/v1/health` | состояние сервиса и узла |
| POST | `/api/v1/payments` | списать оплату |
| GET | `/api/v1/payments/{id}` | статус платежа (со сверкой подтверждений) |
| GET | `/api/v1/payments` | последние платежи мерчанта |
| POST | `/api/v1/payments/{id}/refunds` | возврат, полный или частичный |
| GET | `/api/v1/payments/{id}/refunds` | возвраты по платежу |
| POST | `/api/v1/demo/clients` | (демо) создать и пополнить кошелёк клиента |
| POST | `/api/v1/demo/mine` | (демо) намайнить блоки |

Пример:

```bash
curl -X POST http://<IP>:8000/api/v1/payments \
  -H "X-API-Key: <ключ>" -H "Content-Type: application/json" \
  -d '{"order_id":"order-1001","client_id":"alice","amount":"0.0015","description":"Заказ 1001"}'
```

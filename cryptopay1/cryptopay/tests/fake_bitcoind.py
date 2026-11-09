"""Упрощённая имитация JSON-RPC Bitcoin Core для тестов адаптера без настоящего узла.

Поддерживает только методы, которые вызывает сервис, и возвращает ответы
в том же формате и с теми же кодами ошибок, что и Bitcoin Core.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

FEE = Decimal("0.00000141")


class FakeChain:
    def __init__(self):
        self.on_disk: dict[str, dict] = {}   # name -> {"utxo": Decimal, "addresses": {addr: label}}
        self.loaded: set[str] = set()
        self.txs: dict[str, dict] = {}
        self.height = 0
        self.calls: list[tuple[str, str | None, object]] = []
        self._n = itertools.count(1)

    def _txid(self):
        return hashlib.sha256(str(next(self._n)).encode()).hexdigest()

    def owner_of(self, address):
        for name, w in self.on_disk.items():
            if address in w["addresses"]:
                return name
        return None

    def balance(self, name, confirmed=True):
        total = Decimal(0)
        for tx in self.txs.values():
            conf = tx["height"] is not None
            if tx["to_wallet"] == name and (conf or not confirmed):
                total += tx["amount"]
            if tx["from_wallet"] == name:
                total -= tx["amount"] + tx["fee"]
        return total + self.on_disk[name].get("coinbase", Decimal(0))


class RpcFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def _arg(params, index, name, default=None):
    if isinstance(params, dict):
        return params.get(name, default)
    return params[index] if len(params) > index else default


def handle(chain: FakeChain, method: str, params, wallet: str | None):
    chain.calls.append((method, wallet, params))

    def need_wallet():
        if wallet is None:
            raise RpcFailure(-19, "Wallet file not specified")
        if wallet not in chain.loaded:
            raise RpcFailure(-18, f"Requested wallet does not exist or is not loaded: {wallet}")
        return chain.on_disk[wallet]

    if method == "listwallets":
        return sorted(chain.loaded)
    if method == "listwalletdir":
        return {"wallets": [{"name": n} for n in chain.on_disk]}
    if method == "createwallet":
        name = _arg(params, 0, "wallet_name")
        if name in chain.on_disk:
            raise RpcFailure(-4, "Wallet file verification failed. Database already exists.")
        chain.on_disk[name] = {"addresses": {}}
        chain.loaded.add(name)
        return {"name": name}
    if method == "loadwallet":
        name = _arg(params, 0, "filename")
        if name not in chain.on_disk:
            raise RpcFailure(-18, f"Wallet file verification failed. Failed to load database path '{name}'.")
        if name in chain.loaded:
            raise RpcFailure(-35, "Wallet is already loaded.")
        chain.loaded.add(name)
        return {"name": name}
    if method == "getwalletinfo":
        need_wallet()
        count = sum(1 for t in chain.txs.values() if wallet in (t["to_wallet"], t["from_wallet"]))
        return {"walletname": wallet, "txcount": count}
    if method == "getbalances":
        need_wallet()
        trusted = chain.balance(wallet, confirmed=True)
        pending = chain.balance(wallet, confirmed=False) - trusted
        return {"mine": {"trusted": trusted, "untrusted_pending": pending, "immature": Decimal(0)}}
    if method == "getnewaddress":
        w = need_wallet()
        address = f"bcrt1q{hashlib.sha256((wallet + str(next(chain._n))).encode()).hexdigest()[:38]}"
        w["addresses"][address] = _arg(params, 0, "label", "")
        return address
    if method == "walletcreatefundedpsbt":
        need_wallet()
        (address, amount), = _arg(params, 1, "outputs")[0].items()
        if Decimal(amount) + FEE > chain.balance(wallet):
            raise RpcFailure(-4, "Insufficient funds")
        return {"psbt": "cHNidP8BAH...", "fee": FEE, "changepos": 1}
    if method == "sendtoaddress":
        need_wallet()
        address = _arg(params, 0, "address")
        amount = Decimal(str(_arg(params, 1, "amount")))
        if amount + FEE > chain.balance(wallet):
            raise RpcFailure(-6, "Insufficient funds")
        txid = chain._txid()
        chain.txs[txid] = {"to_wallet": chain.owner_of(address), "to_address": address,
                           "from_wallet": wallet, "amount": amount, "fee": FEE, "height": None}
        return txid
    if method == "gettransaction":
        need_wallet()
        txid = _arg(params, 0, "txid")
        tx = chain.txs.get(txid)
        if tx is None or wallet not in (tx["to_wallet"], tx["from_wallet"]):
            raise RpcFailure(-5, "Invalid or non-wallet transaction id")
        result = {"txid": txid, "confirmations": 0 if tx["height"] is None else chain.height - tx["height"] + 1}
        if tx["from_wallet"] == wallet:
            result["fee"] = -tx["fee"]
        return result
    if method == "listreceivedbyaddress":
        need_wallet()
        flt = _arg(params, 3, "address_filter")
        rows = []
        for tx_id, tx in chain.txs.items():
            if tx["to_wallet"] == wallet and (flt is None or tx["to_address"] == flt):
                rows.append({"address": tx["to_address"], "amount": tx["amount"], "txids": [tx_id],
                             "confirmations": 0 if tx["height"] is None else chain.height - tx["height"] + 1})
        return rows
    if method == "generatetoaddress":
        blocks = _arg(params, 0, "nblocks")
        address = _arg(params, 1, "address")
        miner = chain.owner_of(address)
        for _ in range(blocks):
            chain.height += 1
            for tx in chain.txs.values():
                if tx["height"] is None:
                    tx["height"] = chain.height
        if chain.height > 100:
            chain.on_disk[miner]["coinbase"] = chain.on_disk[miner].get("coinbase", Decimal(0)) + Decimal(50)
        return ["00" * 32] * blocks
    if method == "getblockcount":
        return chain.height
    if method == "getblockchaininfo":
        return {"chain": "regtest", "blocks": chain.height}
    raise RpcFailure(-32601, "Method not found")


def start_fake_bitcoind(user="u", password="p"):
    chain = FakeChain()
    import base64
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.end_headers()
                return
            wallet = unquote(self.path[len("/wallet/"):]) if self.path.startswith("/wallet/") else None
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])), parse_float=Decimal)
            try:
                result, error, code = handle(chain, req["method"], req.get("params", []), wallet), None, 200
            except RpcFailure as exc:
                result, error, code = None, {"code": exc.code, "message": exc.message}, 500
            body = json.dumps({"result": result, "error": error, "id": req["id"]},
                              default=lambda d: float(d)).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, chain, url

#!/usr/bin/env python3
"""A2A Merchant Agent 服务（最小实现 · x402 TRON USDT 付费）

端点:
- GET  /.well-known/agent-card.json  -> A2A Agent Card（发现入口）
- POST /a2a/v1                      -> JSON-RPC 2.0 message/send（调用 + 付费闭环）

付费: TRON USDT(TRC-20)「唯一金额尾数匹配」，与 revenue 收款底座同款机制。
第一个付费能力: 每日情报简报（$5/次）。

协议参考: A2A v1.0 + A2A-x402 v0.1 扩展。
"""
import json
import os
import random
import sqlite3
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
CARD_PATH = os.path.join(BASE, "agent-card.json")
STATE_DIR = os.path.join(BASE, "state")
DB_PATH = os.path.join(STATE_DIR, "a2a_orders.db")

RECEIVE_ADDRESS = "TLnnmqLiY4CE8bnekow6ikpjDHoytJP5p3"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_API = "https://api.trongrid.io/v1"
INTEL_DB = "/home/jam/.hermes/profiles/alpha-scout/state/intel_library.db"

PRICE_USD = 5          # 每日情报简报定价
PAYMENT_WINDOW = 900   # 付款窗口 15 分钟
LISTEN = ("127.0.0.1", 8898)


def _db():
    os.makedirs(STATE_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        task_id TEXT PRIMARY KEY,
        payment_id TEXT,
        tail TEXT,
        amount REAL,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        paid_at TEXT,
        tx_hash TEXT
    )""")
    c.commit()
    return c


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def gen_tail():
    """生成不重复的唯一金额尾数 (0.01-0.99)。"""
    db = _db()
    while True:
        tail = round(random.uniform(0.01, 0.99), 2)
        if not db.execute("SELECT 1 FROM orders WHERE tail=?", (str(tail),)).fetchone():
            return tail


def gen_intel_brief() -> str:
    """生成每日情报简报（读真实 intel_library.db 的最新 GO 信号）。"""
    lines = []
    lines.append("## 千里眼 · 每日情报简报")
    lines.append("")
    try:
        c = sqlite3.connect(INTEL_DB)
        rows = c.execute(
            "SELECT date, memo_title, verdict, category FROM intel_memo "
            "WHERE verdict='GO' ORDER BY created_at DESC LIMIT 6"
        ).fetchall()
        if rows:
            lines.append("### 当前 GO 级信号（可落地方向）")
            for date, title, verdict, cat in rows:
                lines.append(f"- [{cat or 'N/A'}] {title}（{date}）")
        else:
            lines.append("（暂无可落地信号，详见后续更新）")
        c.close()
    except Exception as e:
        lines.append(f"（情报库读取失败，占位内容）")
    lines.append("")
    lines.append("---")
    lines.append("本简报由千里眼 agent 集群自动生成，不构成投资建议。")
    return "\n".join(lines)


def fetch_incoming_since(min_ts: int) -> list:
    """查收款地址最近到账的 USDT-TRC20。"""
    url = (f"{TRON_API}/accounts/{RECEIVE_ADDRESS}/transactions/trc20"
           f"?only_confirmed=true&limit=50&contract_address={USDT_CONTRACT}"
           f"&min_timestamp={min_ts}&order_by=block_timestamp,asc")
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("data", [])


def check_payment(order: dict) -> dict | None:
    """查 TronGrid 验证一笔订单是否已到账（按唯一金额精确匹配）。"""
    target = order["amount"]  # 如 5.37
    try:
        txs = fetch_incoming_since(int(time.time()) - PAYMENT_WINDOW * 4)
    except Exception:
        return None
    for tx in txs:
        tok = tx.get("token_info", {})
        if tok.get("to") != RECEIVE_ADDRESS:
            continue
        try:
            amt = round(int(tok.get("value", "0")) / 1e6, 2)
        except (ValueError, TypeError):
            continue
        if abs(amt - target) < 0.001:
            return {"tx_hash": tx.get("transaction_id", ""), "amount": amt}
    return None


def _payment_required(task_id: str, payment_id: str, tail: float) -> dict:
    """构造 x402 payment-required Task。"""
    amount = round(PRICE_USD + tail, 2)          # 如 5.37
    raw = str(int(round(amount * 1e6)))           # 5370000（6 位小数最小单位）
    return {
        "kind": "task",
        "id": task_id,
        "status": {
            "state": "input-required",
            "message": {
                "kind": "message",
                "role": "agent",
                "parts": [{
                    "kind": "text",
                    "text": (f"解锁「每日情报简报」需支付 {amount} USDT (TRC-20) 到 "
                             f"{RECEIVE_ADDRESS}。付款后回传 paymentId 即可获取简报。")
                }],
                "metadata": {
                    "x402.payment.status": "payment-required",
                    "x402.payment.required": {
                        "x402Version": 1,
                        "accepts": [{
                            "scheme": "trc20-usdt-exact",
                            "network": "tron-mainnet",
                            "asset": USDT_CONTRACT,
                            "payTo": RECEIVE_ADDRESS,
                            "maxAmountRequired": raw,
                            "maxTimeoutSeconds": PAYMENT_WINDOW,
                            "extra": {
                                "name": "Tether USD (TRC-20)",
                                "paymentId": payment_id,
                                "tail": str(tail),
                                "amount": str(amount),
                                "matching": "unique-decimal-tail (0.01-0.99)"
                            }
                        }]
                    }
                }
            }
        }
    }


def _completed(task_id: str, text: str, receipt: dict) -> dict:
    """构造 payment-completed Task（含交付内容）。"""
    return {
        "kind": "task",
        "id": task_id,
        "status": {
            "state": "completed",
            "message": {
                "kind": "message",
                "role": "agent",
                "parts": [{"kind": "text", "text": text}],
                "metadata": {
                    "x402.payment.status": "payment-completed",
                    "x402.payment.receipts": [receipt]
                }
            }
        },
        "artifacts": [{
            "kind": "text",
            "name": "daily-intel-brief",
            "parts": [{"kind": "text", "text": text}]
        }]
    }


def _err(id_, text: str):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": -32000, "message": text}}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass  # 静默

    def do_GET(self):
        if self.path.rstrip("/") == "/.well-known/agent-card.json":
            if not os.path.exists(CARD_PATH):
                self._send(404, {"error": "agent card not found"})
                return
            with open(CARD_PATH, encoding="utf-8") as f:
                card = json.load(f)
            self._send(200, card)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/a2a/v1":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            return

        rid = req.get("id")
        method = req.get("method")
        if method != "message/send":
            self._send(200, _err(rid, f"unsupported method: {method}"))
            return

        params = req.get("params", {})
        msg = params.get("message", {})
        task_id = msg.get("taskId") or f"task-{os.urandom(4).hex()}"
        meta = msg.get("metadata", {})
        db = _db()

        # --- 分支1: 带付款提交 -> 验证到账 -> 交付 ---
        if meta.get("x402.payment.status") == "payment-submitted":
            payload = meta.get("x402.payment.payload", {})
            pid = payload.get("paymentId", "")
            row = db.execute("SELECT * FROM orders WHERE task_id=? OR payment_id=?",
                             (task_id, pid)).fetchone()
            if not row:
                self._send(200, _err(rid, "unknown payment id"))
                return
            order = dict(zip(
                ["task_id", "payment_id", "tail", "amount", "status", "created_at", "paid_at", "tx_hash"],
                row))
            if order["status"] == "paid":
                brief = gen_intel_brief()
                self._send(200, _completed(task_id, brief,
                    {"success": True, "transaction": order["tx_hash"], "network": "tron-mainnet"}))
                return
            hit = check_payment(order)
            if hit:
                db.execute("UPDATE orders SET status='paid', paid_at=?, tx_hash=? WHERE task_id=?",
                           (_now(), hit["tx_hash"], task_id))
                db.commit()
                brief = gen_intel_brief()
                self._send(200, _completed(task_id, brief,
                    {"success": True, "transaction": hit["tx_hash"], "network": "tron-mainnet"}))
            else:
                self._send(200, {
                    "kind": "task", "id": task_id, "status": {
                        "state": "working",
                        "message": {"kind": "message", "role": "agent",
                                    "parts": [{"kind": "text", "text": "尚未检测到付款，请转账后重试。"}],
                                    "metadata": {"x402.payment.status": "payment-submitted"}}}})
            return

        # --- 分支2: 新请求 -> 返回付款要求 ---
        tail = gen_tail()
        payment_id = f"pay-{os.urandom(6).hex()}"
        amount = round(PRICE_USD + tail, 2)
        db.execute("INSERT INTO orders (task_id, payment_id, tail, amount, status, created_at) "
                   "VALUES (?,?,?,?,?,?)",
                   (task_id, payment_id, str(tail), amount, "pending", _now()))
        db.commit()
        self._send(200, _payment_required(task_id, payment_id, tail))


def main():
    srv = ThreadingHTTPServer(LISTEN, Handler)
    print(f"[A2A] 服务启动 http://{LISTEN[0]}:{LISTEN[1]}")
    srv.serve_forever()


if __name__ == "__main__":
    main()

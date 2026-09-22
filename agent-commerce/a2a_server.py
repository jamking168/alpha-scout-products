#!/usr/bin/env python3
"""A2A Merchant Agent 服务（x402 TRON USDT 付费）— v2 修复版

端点:
- GET  /.well-known/agent-card.json  -> A2A Agent Card（发现入口）
- POST /a2a/v1                      -> JSON-RPC 2.0 message/send（调用 + 付费闭环）

付费: TRON USDT(TRC-20)「唯一金额尾数匹配」。
金额全程用整数「分」(cents) 计算，杜绝浮点误差。

v2 修复（对应审查报告 P0）:
- P0-3  支付字段从交易顶层读（to/value/type），非 token_info
- P0-2  min_timestamp 用毫秒；查询窗口对齐订单创建时间
- P0-2  尾数改整数槽位 + 过期回收 + 容量上限（无死循环，满则 503）
- P0-1  tx_hash 唯一约束 + 条件更新（乐观锁）原子认领
- P0    订单所有权：高熵 task_id/payment_id（secrets.token_urlsafe）
- P0-5  交付失败不结单（paid_undelivered），不返回占位内容
- P0    交付物固化（artifact 落库），按次购买不变成无限订阅
- P0    JSON-RPC 成功响应统一 envelope；错误用标准错误码
- P0    请求体大小校验、幂等、每 IP 限流
- P1    SQLite WAL + busy_timeout + 连接治理
"""
import datetime
import json
import logging
import os
import secrets
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
CARD_PATH = os.path.join(BASE, "agent-card.json")
STATE_DIR = os.path.join(BASE, "state")
DB_PATH = os.path.join(STATE_DIR, "a2a_orders.db")

RECEIVE_ADDRESS = "TLnnmqLiY4CE8bnekow6ikpjDHoytJP5p3"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_API = "https://api.trongrid.io/v1"
INTEL_DB = "/home/jam/.hermes/profiles/alpha-scout/state/intel_library.db"

PRICE_CENTS = 500          # $5.00 = 500 分
TAIL_MIN, TAIL_MAX = 1, 99  # 尾数 1–99 分（即 $0.01–$0.99）
PAYMENT_WINDOW = 3600        # 付款窗口 60 分钟(TRON 出块 3s,手动转账+交易所提币常超时,15 分钟太紧)
MAX_BODY = 64 * 1024        # 请求体上限 64 KiB
MAX_DELIVER = 3             # 单订单交付次数上限
RATE_LIMIT = 5              # 每 IP 每窗口请求数
RATE_WINDOW = 60            # 限流窗口秒
NEW_ORDER_LIMIT = 3         # 每 IP 每窗口「创建订单」配额(与通用 RPC 限流解耦)
NEW_ORDER_WINDOW = 600      # 创建订单限流窗口秒(10 分钟)
MAX_PENDING_GLOBAL = 60     # 全局 pending 订单上限,留 39 个尾数槽给真实买家
TRUSTED_PROXIES = {"127.0.0.1", "::1"}  # 可信反代来源(仅本机 Caddy)
CHECK_COOLDOWN_MS = 20000   # 每订单到账查询冷却(防 TronGrid 被刷)
LISTEN = ("127.0.0.1", 8898)

log = logging.getLogger("a2a")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    task_id        TEXT PRIMARY KEY,
    payment_id     TEXT UNIQUE,
    tail_cents     INTEGER,
    amount_cents   INTEGER,
    status         TEXT DEFAULT 'pending',
    created_at_ms  INTEGER,
    expires_at_ms  INTEGER,
    paid_at        TEXT,
    tx_hash        TEXT UNIQUE,
    artifact       TEXT,
    delivered_count INTEGER DEFAULT 0,
    refund_address TEXT,
    last_check_ms  INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_tail_active
    ON orders(tail_cents) WHERE status='pending';
"""


@contextmanager
def db_conn(path=DB_PATH, readonly=False):
    os.makedirs(STATE_DIR, exist_ok=True)
    if readonly:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    else:
        c = sqlite3.connect(path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=8000")
        c.execute("PRAGMA synchronous=NORMAL")
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def init_db():
    with db_conn() as c:
        c.executescript(SCHEMA)
        # 迁移: 旧库补列(幂等)
        cols = {r[1] for r in c.execute("PRAGMA table_info(orders)")}
        if "last_check_ms" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN last_check_ms INTEGER DEFAULT 0")
        c.commit()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class BriefUnavailable(Exception):
    """情报源暂不可用，不能交付。"""


def gen_tail(db) -> int | None:
    """从未过期且未占用的尾数中随机取一个；满则返回 None（触发 503 排队）。
    本函数不 commit，由调用方在事务内控制；先回收过期订单。"""
    now_ms = _now_ms()
    db.execute("UPDATE orders SET status='expired' WHERE status='pending' AND expires_at_ms <= ?",
               (now_ms,))
    used = {r[0] for r in db.execute("SELECT tail_cents FROM orders WHERE status='pending'")}
    free = [t for t in range(TAIL_MIN, TAIL_MAX + 1) if t not in used]
    if not free:
        return None
    return secrets.choice(free)


def gen_intel_brief() -> str:
    """生成简报。情报源不可用或无可交付信号时抛 BriefUnavailable，绝不返回占位。"""
    try:
        with db_conn(path=INTEL_DB, readonly=True) as c:
            rows = c.execute(
                "SELECT date, memo_title, category FROM intel_memo "
                "WHERE verdict='GO' ORDER BY created_at DESC LIMIT 6"
            ).fetchall()
    except Exception as e:
        raise BriefUnavailable(f"情报库读取失败: {e}") from e
    if not rows:
        raise BriefUnavailable("当前无可交付的 GO 级信号")
    lines = ["## 千里眼 · 每日情报简报", "", "### 当前 GO 级信号（可落地方向）"]
    for r in rows:
        lines.append(f"- [{r['category'] or 'N/A'}] {r['memo_title']}（{r['date']}）")
    lines += ["", "---", "本简报由千里眼 agent 集群自动生成，不构成投资建议。"]
    return "\n".join(lines)


def fetch_incoming_since(min_ts_ms: int, max_ts_ms: int | None = None) -> list:
    """查收款地址在 [min_ts_ms, max_ts_ms] 窗口内到账的 USDT-TRC20。"""
    url = (f"{TRON_API}/accounts/{RECEIVE_ADDRESS}/transactions/trc20"
           f"?only_confirmed=true&limit=200&contract_address={USDT_CONTRACT}"
           f"&min_timestamp={min_ts_ms}&order_by=block_timestamp,asc")
    if max_ts_ms:
        url += f"&max_timestamp={max_ts_ms}"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        payload = json.load(r)
    if not payload.get("success", True):
        raise RuntimeError(f"trongrid error: {payload.get('error')}")
    return payload.get("data", [])


def check_payment(db, order) -> dict | None:
    """按整数金额精确匹配 + tx_hash 去重 + 时间窗校验。"""
    target_cents = order["amount_cents"]
    created_ms = order["created_at_ms"]
    try:
        txs = fetch_incoming_since(created_ms, created_ms + PAYMENT_WINDOW * 1000)
    except Exception as e:
        log.warning("trongrid 查询失败: %s", e)
        return None
    for tx in txs:
        txid = tx.get("transaction_id", "")
        if not txid:
            continue
        # 该交易是否已被任何订单消费
        if db.execute("SELECT 1 FROM orders WHERE tx_hash=?", (txid,)).fetchone():
            continue
        # 字段在交易顶层，非 token_info
        if tx.get("to") != RECEIVE_ADDRESS:
            continue
        if tx.get("from") == RECEIVE_ADDRESS:   # 排除自转/退款
            continue
        t = tx.get("type")
        if t is not None and t != "Transfer":   # type 缺失不跳过(部分接口不返回)
            continue
        tok = tx.get("token_info") or {}
        if tok.get("address") != USDT_CONTRACT:
            continue
        blk_ms = int(tx.get("block_timestamp", 0) or 0)
        # 时间窗: 订单创建后 PAYMENT_WINDOW 内(覆盖正常 + 过期补救)
        if not (created_ms <= blk_ms <= created_ms + PAYMENT_WINDOW * 1000):
            continue
        try:
            dec = int(tok.get("decimals", 6))
            raw = int(tx.get("value", "0"))
        except (ValueError, TypeError):
            continue
        # 固定 USDT 精度为 6(不依赖上游 metadata 决定指数运算)
        if dec != 6:
            continue
        # 精确 raw 比较(不整除截断,杜绝少付 0.000001 也通过)
        expected_raw = target_cents * 10000
        if raw != expected_raw:
            continue
        return {"tx_hash": txid, "amount_cents": target_cents}
    return None


def _rpc_result(rid, value):
    return {"jsonrpc": "2.0", "id": rid, "result": value}


def _rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _payment_required(task_id: str, payment_id: str, amount_cents: int) -> dict:
    amount_usd = amount_cents / 100
    raw = str(amount_cents * 10000)  # 6 位小数最小单位
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
                    "text": (f"解锁「每日情报简报」需支付 {amount_usd:.2f} USDT (TRC-20) 到 "
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
                                "amountCents": str(amount_cents),
                                "amountUsd": f"{amount_usd:.2f}",
                                "exactAmountRaw": raw,
                                "note": "必须按 exactAmountRaw/amountUsd 精确支付(多付少付均无法匹配);maxAmountRequired 即本订单精确金额"
                            }
                        }]
                    }
                }
            }
        }
    }


def _completed(task_id: str, text: str, receipt: dict) -> dict:
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


def _working(task_id: str, text: str) -> dict:
    return {
        "kind": "task", "id": task_id, "status": {
            "state": "working",
            "message": {"kind": "message", "role": "agent",
                        "parts": [{"kind": "text", "text": text}],
                        "metadata": {"x402.payment.status": "payment-submitted"}}}
    }


# 内存限流（单进程；生产建议 Redis / Nginx limit_req）
_RATE = {}


def _allow(ip: str, limit: int = RATE_LIMIT, window: int = RATE_WINDOW) -> bool:
    now = time.time()
    # LRU 式清理: 只删真正过期的,不做全量 clear(防被利用重置限流)
    if len(_RATE) > 4096:
        for k in [k for k, v in _RATE.items() if not v or now - v[-1] > window]:
            _RATE.pop(k, None)
    q = [t for t in _RATE.get(ip, []) if now - t < window]
    if len(q) >= limit:
        _RATE[ip] = q
        return False
    q.append(now)
    _RATE[ip] = q
    return True


_ORDER_RATE = {}


def _allow_order(ip: str) -> bool:
    """开单限流(与通用 RPC 限流解耦,防匿名占满尾数槽)。"""
    now = time.time()
    q = [t for t in _ORDER_RATE.get(ip, []) if now - t < NEW_ORDER_WINDOW]
    if len(q) >= NEW_ORDER_LIMIT:
        _ORDER_RATE[ip] = q
        return False
    q.append(now)
    _ORDER_RATE[ip] = q
    return True


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _client_ip(self) -> str:
        # 只有来自可信反代(本机 Caddy)的连接才采信 XFF;取最右一跳(Caddy 追加的,客户端不可伪造)。
        peer = self.client_address[0]
        if peer in TRUSTED_PROXIES:
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                parts = [p.strip() for p in xff.split(",") if p.strip()]
                if parts:
                    return parts[-1]
        return peer

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
        ip = self._client_ip()
        if not _allow(ip):
            self._send(429, _rpc_error(None, -32005, "rate limited"))
            return
        if self.path.rstrip("/") != "/a2a/v1":
            self._send(404, _rpc_error(None, -32601, "not found"))
            return

        # 请求体大小校验
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, _rpc_error(None, -32700, "bad Content-Length"))
            return
        if length <= 0 or length > MAX_BODY:
            self._send(413, _rpc_error(None, -32700, "payload too large"))
            return
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._send(400, _rpc_error(None, -32700, "incomplete body"))
            return

        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, _rpc_error(None, -32700, "parse error"))
            return
        if not isinstance(req, dict):
            self._send(400, _rpc_error(None, -32600, "invalid request"))
            return

        rid = req.get("id")
        method = req.get("method")
        if method != "message/send":
            self._send(200, _rpc_error(rid, -32601, f"unsupported method: {method}"))
            return

        params = req.get("params") or {}
        if not isinstance(params, dict):
            self._send(200, _rpc_error(rid, -32602, "invalid params"))
            return
        msg = params.get("message") or {}
        if not isinstance(msg, dict):
            self._send(200, _rpc_error(rid, -32602, "invalid message"))
            return
        meta = msg.get("metadata") or {}
        if not isinstance(meta, dict):
            self._send(200, _rpc_error(rid, -32602, "invalid metadata"))
            return
        client_task_id = msg.get("taskId")
        if client_task_id is not None and not isinstance(client_task_id, str):
            self._send(200, _rpc_error(rid, -32602, "invalid taskId"))
            return
        if client_task_id is not None and len(client_task_id) > 128:
            self._send(200, _rpc_error(rid, -32602, "taskId too long"))
            return

        with db_conn() as db:
            # --- 分支1: 付款提交 -> 验证 -> 交付 ---
            if meta.get("x402.payment.status") == "payment-submitted":
                payload = meta.get("x402.payment.payload") or {}
                if not isinstance(payload, dict):
                    self._send(200, _rpc_error(rid, -32602, "invalid payload"))
                    return
                pid = payload.get("paymentId", "")
                if not isinstance(pid, str) or not pid:
                    self._send(200, _rpc_error(rid, -32602, "invalid paymentId"))
                    return
                # 取货凭证 = payment_id(保密); task_id 仅做一致性校验,不再是凭证
                row = db.execute("SELECT * FROM orders WHERE payment_id=?", (pid,)).fetchone()
                if not row:
                    self._send(200, _rpc_error(rid, -32001, "unknown payment id"))
                    return
                if client_task_id and client_task_id != row["task_id"]:
                    self._send(200, _rpc_error(rid, -32001, "task/payment mismatch"))
                    return

                if row["status"] == "paid" and row["artifact"]:
                    # 已固化内容: 幂等重取(不设终身三次硬上限,防止网络重试被永久锁死)
                    db.execute("UPDATE orders SET delivered_count=delivered_count+1 WHERE task_id=?",
                               (row["task_id"],))
                    db.commit()
                    self._send(200, _rpc_result(rid, _completed(
                        row["task_id"], row["artifact"],
                        {"success": True, "transaction": row["tx_hash"], "network": "tron-mainnet"})))
                    return

                if row["status"] == "refunded":
                    self._send(200, _rpc_error(rid, -32007, "order refunded"))
                    return

                if row["status"] in ("paid", "paid_undelivered", "refund_requested"):
                    # 已付款但未交付: 尝试补发(退款申请中也可补发,人工退款前先交付)
                    try:
                        brief = gen_intel_brief()
                    except BriefUnavailable as e:
                        self._send(200, _rpc_result(rid, _working(row["task_id"], str(e))))
                        return
                    # 条件更新: 只固化仍为空的 artifact,防并发覆盖 + 状态倒退
                    cur = db.execute(
                        "UPDATE orders SET status='paid', artifact=?, "
                        "paid_at=COALESCE(paid_at, ?), delivered_count=delivered_count+1 "
                        "WHERE task_id=? AND status IN ('paid','paid_undelivered','refund_requested') "
                        "AND artifact IS NULL",
                        (brief, _now_str(), row["task_id"]))
                    if cur.rowcount == 0:
                        # 并发下已有其他请求固化,读取获胜内容返回
                        db.rollback()
                        won = db.execute("SELECT artifact FROM orders WHERE task_id=?", (row["task_id"],)).fetchone()
                        if won and won["artifact"]:
                            self._send(200, _rpc_result(rid, _completed(
                                row["task_id"], won["artifact"],
                                {"success": True, "transaction": row["tx_hash"], "network": "tron-mainnet"})))
                            return
                        self._send(200, _rpc_error(rid, -32006, "order state changed, retry"))
                        return
                    db.commit()
                    self._send(200, _rpc_result(rid, _completed(
                        row["task_id"], brief,
                        {"success": True, "transaction": row["tx_hash"], "network": "tron-mainnet"})))
                    return

                # 每订单到账查询冷却(防 TronGrid 被刷)
                if _now_ms() - (row["last_check_ms"] or 0) < CHECK_COOLDOWN_MS:
                    self._send(200, _rpc_result(rid, _working(row["task_id"], "付款检测中,请稍后重试。")))
                    return
                db.execute("UPDATE orders SET last_check_ms=? WHERE task_id=?", (_now_ms(), row["task_id"]))
                db.commit()

                hit = check_payment(db, row)
                if not hit:
                    self._send(200, _rpc_result(rid, _working(
                        row["task_id"], "尚未检测到付款，请转账后重试。")))
                    return

                # 原子认领(含过期订单补救): 写 paid_undelivered(已付款未交付),rowcount==0 说明被并发抢先
                try:
                    cur = db.execute(
                        "UPDATE orders SET status='paid_undelivered', paid_at=?, tx_hash=? "
                        "WHERE task_id=? AND status IN ('pending','expired')",
                        (_now_str(), hit["tx_hash"], row["task_id"]))
                except sqlite3.IntegrityError:
                    db.rollback()
                    self._send(200, _rpc_error(rid, -32003, "payment already claimed by another order"))
                    return
                if cur.rowcount == 0:
                    db.rollback()
                    self._send(200, _rpc_error(rid, -32003, "payment already claimed"))
                    return
                # 关键: 认领后立即 commit(先落账,再干活),把认领与交付拆成两个事务
                db.commit()
                # 生成并固化交付物;失败则保持 paid_undelivered(不返回占位)
                try:
                    brief = gen_intel_brief()
                except BriefUnavailable as e:
                    refund_addr = payload.get("refundAddress", "")
                    if not isinstance(refund_addr, str):
                        refund_addr = ""
                    if refund_addr:
                        db.execute("UPDATE orders SET status='refund_requested', refund_address=? "
                                   "WHERE task_id=?", (refund_addr, row["task_id"]))
                    # 无退款地址则保持 paid_undelivered(已是该状态,无需再写)
                    db.commit()
                    notice = (f"付款已确认（tx {hit['tx_hash'][:12]}…），但内容源暂不可用：{e}。"
                              f"系统将补发；如需退款请回传 refundAddress。")
                    if refund_addr:
                        notice += f" 已收到退款地址 {refund_addr[:8]}…，退款待人工处理。"
                    self._send(200, _rpc_result(rid, _working(row["task_id"], notice)))
                    return
                # 固化交付物: 条件更新(artifact IS NULL),并发下只允许写入一次
                cur2 = db.execute(
                    "UPDATE orders SET artifact=?, delivered_count=1, status='paid' "
                    "WHERE task_id=? AND artifact IS NULL AND tx_hash IS NOT NULL",
                    (brief, row["task_id"]))
                if cur2.rowcount == 0:
                    db.rollback()
                    won = db.execute("SELECT artifact FROM orders WHERE task_id=?", (row["task_id"],)).fetchone()
                    if won and won["artifact"]:
                        brief = won["artifact"]
                    # else: 极端并发下 artifact 未落库,仍返回本请求生成内容(下次可幂等重取)
                db.commit()
                self._send(200, _rpc_result(rid, _completed(
                    row["task_id"], brief,
                    {"success": True, "transaction": hit["tx_hash"], "network": "tron-mainnet"})))
                return

            # --- 分支2: 新请求 ---
            # task_id 是公开标识,不是凭证。带 task_id 但无 payment_id 时,
            # 不重新披露 payment_id、不返回 artifact(防凭证泄露/白嫖)。
            if client_task_id:
                existing = db.execute("SELECT * FROM orders WHERE task_id=?",
                                      (client_task_id,)).fetchone()
                if existing:
                    self._send(200, _rpc_error(rid, -32008,
                        "order exists; resubmit with your paymentId to complete payment or claim"))
                    return

            # 开单配额: 全局 pending 上限 + 每 IP 开单限流(与通用 RPC 限流解耦)
            npend = db.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
            if npend >= MAX_PENDING_GLOBAL:
                self._send(503, _rpc_error(rid, -32004, "too many pending orders, retry later"))
                return
            if not _allow_order(ip):
                self._send(429, _rpc_error(rid, -32005, "new order rate limited"))
                return

            # 原子化: 唯一索引 ix_tail_active 是尾数冲突的最终防线,冲突则重试
            task_id = payment_id = amount_cents = None
            for _attempt in range(5):
                tail_c = gen_tail(db)
                if tail_c is None:
                    db.rollback()
                    self._send(503, _rpc_error(rid, -32004, "payment slots exhausted, retry later"))
                    return
                task_id = f"task-{secrets.token_urlsafe(24)}"
                payment_id = f"pay-{secrets.token_urlsafe(24)}"
                amount_cents = PRICE_CENTS + tail_c
                now_ms = _now_ms()
                try:
                    db.execute(
                        "INSERT INTO orders (task_id, payment_id, tail_cents, amount_cents, status, "
                        "created_at_ms, expires_at_ms) VALUES (?,?,?,?,?,?,?)",
                        (task_id, payment_id, tail_c, amount_cents, "pending",
                         now_ms, now_ms + PAYMENT_WINDOW * 1000))
                    db.commit()
                    break
                except sqlite3.IntegrityError:
                    db.rollback()
                    continue
            else:
                self._send(503, _rpc_error(rid, -32004, "payment slots exhausted, retry later"))
                return
            self._send(200, _rpc_result(rid, _payment_required(task_id, payment_id, amount_cents)))


def main():
    init_db()
    srv = ThreadingHTTPServer(LISTEN, Handler)
    log.info("A2A 服务启动 http://%s:%s", LISTEN[0], LISTEN[1])
    srv.serve_forever()


if __name__ == "__main__":
    main()

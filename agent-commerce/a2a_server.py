#!/usr/bin/env python3
"""A2A Merchant Agent 服务（x402 TRON USDT 付费）— v2 修复版

端点:
- GET  /.well-known/agent-card.json  -> A2A Agent Card（发现入口）
- POST /a2a/v1                      -> JSON-RPC 2.0 message/send（调用 + 付费闭环）

付费: TRON USDT(TRC-20)「每订单独立收款地址」(HD 派生 m/44'/195'/0'/0/N, 无尾数匹配)。
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
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.request
from contextlib import contextmanager

from hd_wallet import derive_address, load_mnemonic, verify_wallet
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn

BASE = os.path.dirname(os.path.abspath(__file__))
CARD_PATH = os.path.join(BASE, "agent-card.json")
STATE_DIR = os.path.join(BASE, "state")
HWM_PATH = os.path.join(STATE_DIR, "hd_index.hwm")  # HD 高水位独立文件(DB 被删/重建也不回退, 防地址复用)
DB_PATH = os.path.join(STATE_DIR, "a2a_orders.db")

RECEIVE_ADDRESS = "TLnnmqLiY4CE8bnekow6ikpjDHoytJP5p3"  # 归集主地址(子地址收到的 USDT 最终归集到这里)
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_API = "https://api.trongrid.io/v1"
INTEL_DB = "/home/jam/.hermes/profiles/alpha-scout/state/intel_library.db"
TRON_ADDR_RE = __import__("re").compile(r'^T[1-9A-HJ-NP-Za-km-z]{33}$')  # TRON base58check 地址格式

# ── 价格体系（分层结构固定, 具体数字可调）────────────────────────
# 体系(固定): 免费 Sample / 单期 Single / 月订 Monthly / 年订 Annual / 定制 Custom
# 用户只调下面各数字, 不改分层结构。
PRICE_CENTS = {
    "single":  900,      # $9 单期(指定日期快照, 试用入口)
    "weekly":  1900,     # $19 周订(7 天, 判断"每天是否有料"的关键试用层)
    "monthly": 4900,     # $49 月订(30 天, 主力 SKU)
    "annual":  49000,    # $490 年订(365 天, 暂缓上架: 需先积累交付记录+续费观察)
}
# 早鸟价(未实现, 禁止对外宣传): 锁价需要"锁价表+周期计数+续费系统", 实现前不得承诺
# EARLYBIRD_CENTS = 2900   # $29 早鸟月订锁价(前 50 名, 锁 6 个续费周期)
# EARLYBIRD_LIMIT = 50
SKU_DAYS = {
    "single": 0,
    "weekly": 7,
    "monthly": 30,
    "annual": 365,
}
DEFAULT_SKU = "single"
SKU_NAME = {
    "single": "单期简报（指定日期快照）",
    "weekly": "周订（每日简报 · 7 天）",
    "monthly": "月订（每日简报 · 30 天）",
    "annual": "年订（每日简报 · 365 天）",
}
# 服务端禁售 SKU(年订暂缓: 需先积累交付记录+续费观察, 避免提前收取一年履约款)
ENABLED_SKUS = {"single", "weekly", "monthly"}


def _is_sub(sku: str) -> bool:
    """统一订阅判定: SKU_DAYS[sku] > 0 即订阅(weekly/monthly/annual), 杜绝散落硬编码列表。"""
    return SKU_DAYS.get(sku or DEFAULT_SKU, 0) > 0
# 独立地址架构: 每订单一个 HD 派生子地址(m/44'/195'/0'/0/N), 替代共享地址+尾数匹配。
# master mnemonic 从环境变量 TRON_MASTER_MNEMONIC 读取(私钥级, 不落盘, 开单时懒加载)。
PAYMENT_WINDOW = 3600        # 付款窗口 60 分钟(TRON 出块 3s,手动转账+交易所提币常超时,15 分钟太紧)
LATE_PAY_GRACE = 6 * 3600    # 迟到付款宽限 6 小时(交易所 TRC20 提币经风控审核常超 1 小时, 过期后到账仍需可认领)
MAX_BODY = 64 * 1024        # 请求体上限 64 KiB
MAX_DELIVER = 5             # 单订单交付次数硬上限(含网络重试余量; 防 task_id 泄露无限白嫖)
SUB_DAY_LIMIT = 3           # 订阅每日交付上限(简报日更, 正常 1 次/天; 防 payment_id 泄露无限白嫖)
RATE_LIMIT = 5              # 每 IP 每窗口请求数
RATE_WINDOW = 60            # 限流窗口秒
NEW_ORDER_LIMIT = 3         # 每 IP 每窗口「创建订单」配额(与通用 RPC 限流解耦)
NEW_ORDER_WINDOW = 600      # 创建订单限流窗口秒(10 分钟)
MAX_PENDING_GLOBAL = 60     # 全局 pending 订单上限
MAX_PENDING_PER_IP = 2      # 每 IP 同时 pending 订单上限(DB 为准,防单 IP 占槽)
TRUSTED_PROXIES = {"127.0.0.1", "::1"}  # 可信反代来源(仅本机 Caddy)
CHECK_COOLDOWN_MS = 20000   # 每订单到账查询冷却(防 TronGrid 被刷)
LISTEN = ("127.0.0.1", 8898)

log = logging.getLogger("a2a")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    task_id        TEXT PRIMARY KEY,
    payment_id     TEXT UNIQUE,
    receive_address TEXT,     -- 订单专属收款地址(HD 派生)
    address_index  INTEGER,    -- HD 派生序号 m/44'/195'/0'/0/{index}
    amount_cents   INTEGER,
    status         TEXT DEFAULT 'pending',
    created_at_ms  INTEGER,
    expires_at_ms  INTEGER,
    paid_at        TEXT,
    tx_hash        TEXT UNIQUE,
    artifact       TEXT,
    delivered_count INTEGER DEFAULT 0,
    refund_address TEXT,
    last_check_ms  INTEGER DEFAULT 0,
    creator_ip     TEXT,
    swept          INTEGER DEFAULT 0,   -- 归集状态(0 未归集, 1 已归集)
    sweep_tx       TEXT,                 -- 归集交易 txid
    sweep_at       TEXT,
    fund_tx        TEXT,                 -- TRX 充值交易 txid(幂等标记, 防重复充值)
    fund_at        TEXT,                 -- fund_tx 落盘时间(判断充值交易是否已过 TRON 有效期, 防未失效重出款)
    sku            TEXT DEFAULT 'single', -- SKU(single/monthly/annual)
    access_until_ms INTEGER DEFAULT 0,    -- 订阅有效期截止毫秒(订阅订单才有值)
    last_deliver_day TEXT,                -- 订阅最近一次交付的自然日(按日频控用)
    day_deliver_count INTEGER DEFAULT 0    -- 订阅当日交付次数(按日频控, 防 payment_id 泄露无限白嫖)
);
CREATE TABLE IF NOT EXISTS hd_counter (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    next_index INTEGER NOT NULL
);
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
        if "creator_ip" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN creator_ip TEXT")
        if "receive_address" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN receive_address TEXT")
        if "address_index" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN address_index INTEGER")
        if "swept" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN swept INTEGER DEFAULT 0")
        if "sweep_tx" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN sweep_tx TEXT")
        if "sweep_at" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN sweep_at TEXT")
        if "fund_tx" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN fund_tx TEXT")
        if "fund_at" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN fund_at TEXT")
        # 回填历史行(独立于"新增列"条件, 每次启动幂等执行):
        # 旧充值 fund_tx 非空但 fund_at 为 NULL(第15轮之前落盘) 以首次观察时间为准回填。
        # 必须放在这里而非"新增列"分支内——否则 server/reconcile 先启动补了列但不回填,
        # sweep 的 ensure_schema 看到列已存在就跳过, 历史行 fund_at 永久 NULL → 永久 FUND-HOLD。
        c.execute(
            "UPDATE orders SET fund_at=? WHERE fund_tx IS NOT NULL AND fund_tx <> '' AND fund_at IS NULL",
            (_now_str(),),
        )
        if "payment_exception" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN payment_exception TEXT")
        if "refund_requested_at" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN refund_requested_at TEXT")
        if "sku" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN sku TEXT DEFAULT 'single'")
        if "access_until_ms" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN access_until_ms INTEGER DEFAULT 0")
        if "last_deliver_day" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN last_deliver_day TEXT")
        if "day_deliver_count" not in cols:
            c.execute("ALTER TABLE orders ADD COLUMN day_deliver_count INTEGER DEFAULT 0")
        # 迁移完成后再建索引(先补列再建索引, 避免旧库无列时报错)
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_addr_unique ON orders(receive_address)")
        # 旧架构迁移: 无独立地址的旧 pending 订单作废(旧共享地址+尾数架构已废弃)
        c.execute("UPDATE orders SET status='expired' WHERE receive_address IS NULL AND status='pending'")
        # hd_counter 初始化 + 对齐到 MAX(address_index)+1 与独立高水位文件的 max(单调递增, 删行/清理/DB重建都不回退)
        c.execute("INSERT OR IGNORE INTO hd_counter (id, next_index) VALUES (1, 1)")
        mx = c.execute("SELECT COALESCE(MAX(address_index), 0) FROM orders").fetchone()[0]
        floor = max(mx + 1, _read_hwm() + 1, 1)
        c.execute("UPDATE hd_counter SET next_index = MAX(next_index, ?) WHERE id = 1", (floor,))
        c.commit()


def _read_hwm() -> int:
    """读 HD 高水位(独立文件, DB 删除/重建也不回退)。"""
    try:
        with open(HWM_PATH) as f:
            return int(f.read().strip() or 0)
    except Exception:
        return 0


def _bump_hwm(idx: int) -> None:
    """单调水位线落独立文件: 宁可跳号, 不可复用地址。commit 前调用。"""
    if idx <= _read_hwm():
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = HWM_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(idx))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, HWM_PATH)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class BriefUnavailable(Exception):
    """情报源暂不可用，不能交付。"""


def gen_intel_brief() -> str:
    """生成简报。情报源不可用或无可交付信号时抛 BriefUnavailable，绝不返回占位。"""
    try:
        with db_conn(path=INTEL_DB, readonly=True) as c:
            rows = c.execute(
                "SELECT date, memo_title, category, evidence_grade, body, created_at "
                "FROM intel_memo WHERE verdict='GO' ORDER BY created_at DESC LIMIT 6"
            ).fetchall()
    except Exception as e:
        raise BriefUnavailable(f"情报库读取失败: {e}") from e
    if not rows:
        raise BriefUnavailable("当前无可交付的 GO 级信号")
    header = "### 当前 GO 级信号（可落地方向）"
    # 新鲜度标注: 24h 内无新增则诚实说明, 不把旧内容冒充新信号(履约底线)
    newest = rows[0]['created_at'] or ''
    try:
        newest_dt = datetime.datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
        if newest_dt.tzinfo is None:
            newest_dt = newest_dt.replace(tzinfo=datetime.timezone.utc)
        age_s = (datetime.datetime.now(datetime.timezone.utc) - newest_dt).total_seconds()
        if age_s > 24 * 3600:
            header += ("\n\n> ⚠️ 过去 24 小时无新增 GO 级信号，以下为最近 6 条（最新入库 "
                       f"{newest_dt.strftime('%Y-%m-%d %H:%M')} UTC）。\n"
                       "> 无料时我们不编内容——按证据筛选，不保证每日必有新条目。")
    except Exception:
        pass
    lines = ["## 千里眼 · 每日情报简报", "", header, ""]
    for i, r in enumerate(rows, 1):
        title = (r['memo_title'] or '').strip()
        grade = r['evidence_grade'] or '未分级'
        cat = r['category'] or 'N/A'
        lines.append(f"### {i}. {title}")
        lines.append(f"*{cat} · 证据级 {grade} · {r['date']}*")
        body = (r['body'] or '').strip()
        if body:
            lines.append("")
            lines.append(body)
        lines.append("")
    lines += ["---", "本简报由千里眼 agent 集群自动生成，不构成投资建议。"]
    return "\n".join(lines)


def gen_sample_brief() -> str:
    """免费 Sample: 取较早的历史 GO 信号完整版(质量证明, 不泄露最新时效价值)。

    最新 = 最值钱, 保留在付费侧。Sample 展示的是"判断质量"(证据链+落地路径), 不是实时性。"""
    try:
        with db_conn(path=INTEL_DB, readonly=True) as c:
            row = c.execute(
                "SELECT date, memo_title, category, evidence_grade, body, created_at "
                "FROM intel_memo WHERE verdict='GO' ORDER BY created_at DESC LIMIT 1 OFFSET 7"
            ).fetchone()
            if not row:  # 少于 8 条则回退最新(保证 sample 可用)
                row = c.execute(
                    "SELECT date, memo_title, category, evidence_grade, body, created_at "
                    "FROM intel_memo WHERE verdict='GO' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
    except Exception as e:
        raise BriefUnavailable(f"情报库读取失败: {e}") from e
    if not row:
        raise BriefUnavailable("当前无可交付的 GO 级信号")
    title = (row['memo_title'] or '').strip()
    grade = row['evidence_grade'] or '未分级'
    cat = row['category'] or 'N/A'
    body = (row['body'] or '').strip()
    lines = ["## 千里眼 · 免费样报（历史 GO 信号 · 完整版）", "",
             f"### {title}", f"*{cat} · 证据级 {grade} · {row['date']}*", "", body, "",
             "---",
             "这是免费样报（历史信号完整版，含证据链与落地路径）。",
             "订阅可获取每日最新 GO 信号 + 持续跟踪 + 历史。",
             "本简报由千里眼 agent 集群自动生成，不构成投资建议。"]
    return "\n".join(lines)


def fetch_incoming_since(address: str, min_ts_ms: int, max_ts_ms: int | None = None) -> list:
    """查指定地址在 [min_ts_ms, max_ts_ms] 窗口内到账的 USDT-TRC20, 自动遍历分页。

    单页最多 200 条; 窗口内 >200 条时若不分页, 第 201 条后的足额付款会永久漏认。"""
    base = (f"{TRON_API}/accounts/{address}/transactions/trc20"
            f"?only_confirmed=true&limit=200&contract_address={USDT_CONTRACT}"
            f"&min_timestamp={min_ts_ms}&order_by=block_timestamp,asc")
    if max_ts_ms:
        base += f"&max_timestamp={max_ts_ms}"
    all_txs = []
    fingerprint = None
    while True:
        url = base + (f"&fingerprint={fingerprint}" if fingerprint else "")
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = json.load(r)
        if not payload.get("success", True):
            raise RuntimeError(f"trongrid error: {payload.get('error')}")
        page = payload.get("data", [])
        all_txs.extend(page)
        # 分页续查: meta.fingerprint 存在则还有下一页
        fingerprint = (payload.get("meta") or {}).get("fingerprint")
        if not fingerprint or len(page) < 200:
            break
    return all_txs


def check_payment(db, order) -> dict | None:
    """查订单专属地址的入账: 每地址只服务一个订单, 无需尾数匹配/tx 去重。

    足额或多付(>=)即认领(交易所提币常扣 1 USDT 手续费, 精确匹配会导致"收了钱不给货")。
    少付返回 short 提示差额。"""
    receive_addr = order["receive_address"]
    target_cents = order["amount_cents"]
    created_ms = order["created_at_ms"]
    window_end = created_ms + (PAYMENT_WINDOW + LATE_PAY_GRACE) * 1000
    try:
        txs = fetch_incoming_since(receive_addr, created_ms, window_end)
    except Exception as e:
        log.warning("trongrid 查询失败: %s", e)
        return None
    expected_raw = target_cents * 10000
    best_short = None          # 记录最大的「不足额」入账, 用于告知用户差额
    for tx in txs:
        txid = tx.get("transaction_id", "")
        if not txid:
            continue
        # 字段在交易顶层，非 token_info
        if tx.get("to") != receive_addr:
            continue
        if tx.get("from") == receive_addr:   # 排除自转/退款
            continue
        t = tx.get("type")
        if t is not None and t != "Transfer":   # type 缺失不跳过(部分接口不返回)
            continue
        tok = tx.get("token_info") or {}
        if tok.get("address") != USDT_CONTRACT:
            continue
        blk_ms = int(tx.get("block_timestamp", 0) or 0)
        # 时间窗: [订单创建, 创建+付款窗口+迟到宽限](交易所提币风控审核常超 1 小时, 过期后到账仍需可认领)
        if not (created_ms <= blk_ms <= window_end):
            continue
        try:
            dec = int(tok.get("decimals", 6))
            raw = int(tx.get("value", "0"))
        except (ValueError, TypeError):
            continue
        # 固定 USDT 精度为 6(不依赖上游 metadata 决定指数运算)
        if dec != 6:
            continue
        # 足额或多付 -> 认领(多付部分记录, 人工退差/记 credit)
        if raw >= expected_raw:
            return {"tx_hash": txid, "amount_cents": target_cents,
                    "paid_raw": raw, "overpaid_raw": raw - expected_raw,
                    "blk_ms": blk_ms}
        # 不足额 -> 记录最大一笔, 供提示补差
        if best_short is None or raw > best_short[1]:
            best_short = (txid, raw)
    if best_short:
        return {"short": True, "tx_hash": best_short[0], "paid_raw": best_short[1],
                "expected_raw": expected_raw}
    return None


def _rpc_result(rid, value):
    return {"jsonrpc": "2.0", "id": rid, "result": value}


def _rpc_error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _payment_required(task_id: str, payment_id: str, amount_cents: int, receive_address: str, sku: str = "single") -> dict:
    amount_usd = amount_cents / 100
    raw = str(amount_cents * 10000)  # 6 位小数最小单位
    name = SKU_NAME.get(sku, "简报")
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
                    "text": (f"解锁「{name}」需支付 {amount_usd:.2f} USDT (TRC-20) 到 "
                             f"{receive_address}。\n"
                             f"⚠️ 若从交易所提币，请把提币数量设为能让到账 ≥ {amount_usd:.2f} USDT 的值"
                             f"（多数交易所 TRC20 提币扣 1 USDT 手续费，即需提 {amount_usd + 1:.2f}）。\n"
                             f"付款后回传 paymentId 即可获取简报。")
                }],
                "metadata": {
                    "x402.payment.status": "payment-required",
                    "x402.payment.required": {
                        "x402Version": 1,
                        "accepts": [{
                            "scheme": "exact",
                            "network": "tron",
                            "asset": USDT_CONTRACT,
                            "payTo": receive_address,
                            "maxAmountRequired": raw,
                            "maxTimeoutSeconds": PAYMENT_WINDOW,
                            "extra": {
                                "name": "Tether USD (TRC-20)",
                                "paymentId": payment_id,
                                "amountCents": str(amount_cents),
                                "amountUsd": f"{amount_usd:.2f}",
                                "exactAmountRaw": raw,
                                "note": "本地址为订单专属收款地址; 按到账金额 ≥ exactAmountRaw 支付即可(多付部分留人工退差/记 credit)"
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

_SAMPLE_RATE = {}            # 免费样报内存限流(每 IP 每 SAMPLE_COOLDOWN 秒 1 次, 有界)
SAMPLE_COOLDOWN = 7 * 24 * 3600  # 免费样报冷却 7 天


def _allow_order(ip: str) -> bool:
    """开单限流(与通用 RPC 限流解耦,防匿名占满尾数槽)。"""
    now = time.time()
    # 与 _RATE 同样做有界清理,防无界内存增长
    if len(_ORDER_RATE) > 4096:
        for k in [k for k, v in _ORDER_RATE.items() if not v or now - v[-1] > NEW_ORDER_WINDOW]:
            _ORDER_RATE.pop(k, None)
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

    def _handle_sample(self) -> dict:
        """免费 Sample: 返回最新 1 条 GO 信号完整版(不收款), 展示真实质量。每 IP 每 7 天 1 次。"""
        ip = _ip_key(self._client_ip())  # 用 XFF 解析后的真实 IP, 反代后不能用 client_address[0](恒 127.0.0.1)
        key = f"sample:{ip}"
        now = time.time()
        if len(_SAMPLE_RATE) > 8192:  # 有界清理, 防无界内存增长
            for k in [k for k, v in _SAMPLE_RATE.items() if now - v > SAMPLE_COOLDOWN]:
                _SAMPLE_RATE.pop(k, None)
        last = _SAMPLE_RATE.get(key, 0)
        if now - last < SAMPLE_COOLDOWN:
            return _working("sample", "免费样报每 7 天可领取 1 次，请稍后再试；或直接订阅获取每日最新。")
        try:
            brief = gen_sample_brief()
        except BriefUnavailable as e:
            return _working("sample", f"免费样报暂不可用：{e}")
        _SAMPLE_RATE[key] = now
        return _completed("sample", brief, {"success": True, "free": True})

    def do_POST(self):
        ip = _ip_key(self._client_ip())
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
        if method == "sample":
            # 免费 Sample: 返回昨日完整简报(不收款), 展示真实质量 = 唯一信任货币
            self._send(200, _rpc_result(rid, self._handle_sample()))
            return
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

                # 退款申请独立处理: 校验地址 + 登记, 不进入补发分支(退款与交付不能竞争)
                refund_addr = payload.get("refundAddress")
                if refund_addr is not None:
                    if not isinstance(refund_addr, str) or not TRON_ADDR_RE.match(refund_addr):
                        self._send(200, _rpc_error(rid, -32602, "invalid TRON refundAddress"))
                        return
                    if row["status"] == "refunded":
                        self._send(200, _rpc_error(rid, -32007, "order refunded"))
                        return
                    # 条件 UPDATE 原子登记退款: 已交付/退款中的订单拒绝自助退款(防"既拿货又退款"的 TOCTOU 竞态)
                    # 同时冻结权益(status='refund_requested' + access_until_ms=0), 防人工退款后误清
                    # refund_requested_at 导致订阅权益复活(第二道独立防线)
                    cur = db.execute(
                        "UPDATE orders SET refund_address=?, refund_requested_at=?, "
                        "status='refund_requested', access_until_ms=0 "
                        "WHERE task_id=? AND status IN ('pending','expired','paid','paid_undelivered') "
                        "AND COALESCE(delivered_count,0)=0 AND (artifact IS NULL OR artifact='')",
                        (refund_addr, _now_str(), row["task_id"]),
                    )
                    if cur.rowcount == 0:
                        db.rollback()
                        self._send(200, _rpc_result(rid, _working(
                            row["task_id"],
                            "订单已交付内容或状态已变化，自助退款通道不适用；如有争议请联系支持人工处理。")))
                        return
                    db.commit()
                    self._send(200, _rpc_result(rid, _working(
                        row["task_id"],
                        "退款申请已登记，尚未执行退款。请勿继续补款；"
                        "人工核验到账、退款金额和地址后处理。")))
                    return

                if row["refund_requested_at"]:
                    self._send(200, _rpc_result(rid, _working(
                        row["task_id"], "订单处于退款审核中，暂停自动交付；请联系支持。")))
                    return

                # 订阅订单(weekly/monthly/annual): 有效期内每次现取最新, 不固化快照
                if _is_sub(row["sku"]) and row["status"] == "paid":
                    if _now_ms() > (row["access_until_ms"] or 0):
                        self._send(200, _rpc_error(rid, -32011,
                                    "subscription expired; send a new message WITHOUT taskId to create a renewal order"))
                        return
                    # 订阅总量封顶(防 payment_id 泄露后无限白嫖): SKU_DAYS × 每日上限
                    sub_total = SKU_DAYS.get(row["sku"] or DEFAULT_SKU, 0) * SUB_DAY_LIMIT
                    if sub_total > 0 and (row["delivered_count"] or 0) >= sub_total:
                        self._send(200, _rpc_error(rid, -32010, "delivery limit reached; contact support"))
                        return
                    try:
                        brief = gen_intel_brief()
                    except BriefUnavailable as e:
                        self._send(200, _rpc_result(rid, _working(row["task_id"], str(e))))
                        return
                    # 按日频控 + 退款/状态守卫: 原子扣额度, 超限/退款中返回(COALESCE 防 NULL 三值逻辑)
                    today = _now_str()[:10]
                    cur = db.execute(
                        "UPDATE orders SET "
                        "day_deliver_count = CASE WHEN COALESCE(last_deliver_day,'') = ? THEN day_deliver_count + 1 ELSE 1 END, "
                        "last_deliver_day = ?, delivered_count = delivered_count + 1 "
                        "WHERE task_id = ? AND status = 'paid' AND refund_requested_at IS NULL "
                        "AND (COALESCE(last_deliver_day,'') <> ? OR day_deliver_count < ?)",
                        (today, today, row["task_id"], today, SUB_DAY_LIMIT))
                    if cur.rowcount == 0:
                        db.rollback()
                        self._send(200, _rpc_error(rid, 429, "daily delivery limit reached; try again tomorrow"))
                        return
                    db.commit()
                    self._send(200, _rpc_result(rid, _completed(
                        row["task_id"], brief,
                        {"success": True, "transaction": row["tx_hash"], "network": "tron-mainnet"})))
                    return

                if row["status"] == "paid" and row["artifact"]:
                    # 已固化内容: 幂等重取, 条件 UPDATE 加退款守卫 + 硬上限(原子, 防并发突破)
                    cur = db.execute(
                        "UPDATE orders SET delivered_count=delivered_count+1 "
                        "WHERE task_id=? AND status='paid' AND refund_requested_at IS NULL "
                        "AND delivered_count < ?",
                        (row["task_id"], MAX_DELIVER))
                    if cur.rowcount == 0:
                        db.rollback()
                        self._send(200, _rpc_error(rid, -32010, "delivery limit reached; contact support"))
                        return
                    db.commit()
                    self._send(200, _rpc_result(rid, _completed(
                        row["task_id"], row["artifact"],
                        {"success": True, "transaction": row["tx_hash"], "network": "tron-mainnet"})))
                    return

                if row["status"] == "refunded":
                    self._send(200, _rpc_error(rid, -32007, "order refunded"))
                    return

                if row["status"] in ("paid", "paid_undelivered"):
                    # 订阅订单补发: 激活 status=paid, 现取最新 + 扣日额度(不固化快照, 与正常复取一致)
                    if _is_sub(row["sku"]):
                        if _now_ms() > (row["access_until_ms"] or 0):
                            self._send(200, _rpc_error(rid, -32011,
                                        "subscription expired; send a new message WITHOUT taskId to create a renewal order"))
                            return
                        try:
                            brief = gen_intel_brief()
                        except BriefUnavailable as e:
                            self._send(200, _rpc_result(rid, _working(row["task_id"], str(e))))
                            return
                        today = _now_str()[:10]
                        cur = db.execute(
                            "UPDATE orders SET status='paid', "
                            "day_deliver_count = CASE WHEN COALESCE(last_deliver_day,'') = ? THEN day_deliver_count + 1 ELSE 1 END, "
                            "last_deliver_day = ?, delivered_count = delivered_count + 1 "
                            "WHERE task_id = ? AND status IN ('paid','paid_undelivered') "
                            "AND refund_requested_at IS NULL "
                            "AND (COALESCE(last_deliver_day,'') <> ? OR day_deliver_count < ?)",
                            (today, today, row["task_id"], today, SUB_DAY_LIMIT))
                        if cur.rowcount == 0:
                            db.rollback()
                            self._send(200, _rpc_error(rid, -32006, "order state changed, retry"))
                            return
                        db.commit()
                        self._send(200, _rpc_result(rid, _completed(
                            row["task_id"], brief,
                            {"success": True, "transaction": row["tx_hash"], "network": "tron-mainnet"})))
                        return
                    # 单次订单补发: 固化 artifact(退款申请中也可补发,人工退款前先交付)
                    try:
                        brief = gen_intel_brief()
                    except BriefUnavailable as e:
                        self._send(200, _rpc_result(rid, _working(row["task_id"], str(e))))
                        return
                    # 条件更新: 只固化仍为空的 artifact,防并发覆盖 + 状态倒退
                    cur = db.execute(
                        "UPDATE orders SET status='paid', artifact=?, "
                        "paid_at=COALESCE(paid_at, ?), delivered_count=delivered_count+1 "
                        "WHERE task_id=? AND status IN ('paid','paid_undelivered') "
                        "AND artifact IS NULL AND refund_requested_at IS NULL",
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

                # 每订单到账查询冷却(原子认领,防并发一起通过)
                now_ms = _now_ms()
                cur_cool = db.execute(
                    "UPDATE orders SET last_check_ms=? "
                    "WHERE task_id=? AND status IN ('pending','expired') "
                    "AND COALESCE(last_check_ms, 0) <= ?",
                    (now_ms, row["task_id"], now_ms - CHECK_COOLDOWN_MS))
                if cur_cool.rowcount != 1:
                    db.rollback()
                    self._send(200, _rpc_result(rid, _working(row["task_id"], "付款检测中或订单状态已变化,请稍后重试。")))
                    return
                db.commit()

                hit = check_payment(db, row)
                if not hit:
                    self._send(200, _rpc_result(rid, _working(
                        row["task_id"], "尚未检测到付款，请转账后重试。")))
                    return

                # 少付: 记录异常, 不提示"补差"(自动路径只认单笔足额, 分笔累计需人工), 给人工/退款指引
                if hit.get("short"):
                    db.execute(
                        "UPDATE orders SET payment_exception=? WHERE task_id=?",
                        (json.dumps(hit, ensure_ascii=False), row["task_id"]),
                    )
                    db.commit()
                    shortfall = (hit["expected_raw"] - hit["paid_raw"]) / 1e6
                    self._send(200, _rpc_result(rid, _working(
                        row["task_id"],
                        f"检测到一笔不足额到账 {hit['paid_raw']/1e6:.6f} USDT，"
                        f"少于订单金额 {hit['expected_raw']/1e6:.2f} USDT（差 {shortfall:.6f}）。"
                        f"当前自动支付不支持分笔累计，请勿继续补款；"
                        f"请携 paymentId 联系支持，或回传 refundAddress 登记退款。")))
                    return

                # 多付: 记录告警(不自动退回, 留人工退差/记 credit)
                if hit.get("overpaid_raw", 0) > 0:
                    log.warning("OVERPAY task=%s tx=%s overpaid_raw=%s",
                                row["task_id"], hit["tx_hash"], hit["overpaid_raw"])

                # 原子认领: BEGIN IMMEDIATE 写锁内复查 tx_hash 未被占用 + 条件更新
                try:
                    db.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError:
                    self._send(503, _rpc_error(rid, -32003, "busy, retry later"))
                    return
                try:
                    dup = db.execute("SELECT task_id FROM orders WHERE tx_hash=?", (hit["tx_hash"],)).fetchone()
                    if dup:
                        db.rollback()
                        self._send(200, _rpc_error(rid, -32003, "payment already claimed by another order"))
                        return
                    # 订阅有效期从付款确认时刻起算(而非开单时刻, 避免迟到付款吃掉用户已购天数)
                    sub_days = SKU_DAYS.get(row["sku"] or "single", 0)
                    new_until = _now_ms() + sub_days * 24 * 3600 * 1000 if sub_days > 0 else 0
                    cur = db.execute(
                        "UPDATE orders SET status='paid_undelivered', paid_at=?, tx_hash=?, access_until_ms=? "
                        "WHERE task_id=? AND status IN ('pending','expired') AND tx_hash IS NULL",
                        (_now_str(), hit["tx_hash"], new_until, row["task_id"]))
                    if cur.rowcount == 0:
                        db.rollback()
                        self._send(200, _rpc_error(rid, -32003, "payment already claimed"))
                        return
                    db.commit()
                except sqlite3.IntegrityError:
                    db.rollback()
                    self._send(200, _rpc_error(rid, -32003, "payment already claimed by another order"))
                    return

                # 生成并固化交付物;失败则保持 paid_undelivered(不返回占位)
                try:
                    brief = gen_intel_brief()
                except BriefUnavailable as e:
                    # 不在此路径收集 refundAddress(会绕过正规退款通道的地址校验, 造成既交付又退款)
                    # 保持 paid_undelivered, 用户下次重试会补发; 退款走独立 refundAddress 通道
                    notice = (f"付款已确认（tx {hit['tx_hash'][:12]}…），但内容源暂不可用：{e}。"
                              f"系统将补发；如需退款请回传 refundAddress（独立退款流程，人工处理）。")
                    self._send(200, _rpc_result(rid, _working(row["task_id"], notice)))
                    return
                # 固化交付物: 订阅订单不固化快照(每次现取最新), 单次订单固化 artifact
                is_sub = _is_sub(row["sku"])
                if is_sub:
                    cur2 = db.execute(
                        "UPDATE orders SET delivered_count=delivered_count+1, status='paid' "
                        "WHERE task_id=? AND artifact IS NULL AND tx_hash=? "
                        "AND status='paid_undelivered' AND refund_requested_at IS NULL",
                        (row["task_id"], hit["tx_hash"]))
                else:
                    cur2 = db.execute(
                        "UPDATE orders SET artifact=?, delivered_count=delivered_count+1, status='paid' "
                        "WHERE task_id=? AND artifact IS NULL AND tx_hash=? "
                        "AND status='paid_undelivered' AND refund_requested_at IS NULL",
                        (brief, row["task_id"], hit["tx_hash"]))
                if cur2.rowcount == 0:
                    db.rollback()
                    won = db.execute("SELECT status, artifact FROM orders WHERE task_id=?", (row["task_id"],)).fetchone()
                    if won and won["status"] == "refunded":
                        self._send(200, _rpc_error(rid, -32007, "order refunded"))
                        return
                    if won and won["artifact"]:
                        brief = won["artifact"]
                    elif is_sub and won and won["status"] == "paid":
                        pass  # 订阅已并发激活, 返回现取的 brief
                    else:
                        self._send(200, _rpc_error(rid, -32006, "order state changed, retry"))
                        return
                db.commit()
                self._send(200, _rpc_result(rid, _completed(
                    row["task_id"], brief,
                    {"success": True, "transaction": hit["tx_hash"], "network": "tron-mainnet"})))
                return

            # --- 分支2: 新请求 ---
            # task_id 是公开标识(144bit 熵,不可枚举)。带 task_id 但无 payment_id 时:
            # - 已付款订单: 允许凭 task_id 取货(不泄露 payment_id)——否则丢凭证的付费用户永久拿不到货
            # - 未付款订单: 拒绝(不泄露 payment_id,防白嫖)
            if client_task_id:
                existing = db.execute("SELECT * FROM orders WHERE task_id=?",
                                      (client_task_id,)).fetchone()
                if existing:
                    st = existing["status"]
                    if st == "refunded":
                        self._send(200, _rpc_error(rid, -32007, "order refunded"))
                        return
                    # 订阅已过期: 放行开新单(续订死路修复)。否则用户带旧 taskId 续订时,
                    # 会命中"订阅过期 -32011"提示但无续订路径, 形成无限重试死循环。
                    sub_dead = (_is_sub(existing["sku"]) and st == "paid"
                                and _now_ms() > (existing["access_until_ms"] or 0))
                    if not sub_dead:
                        # task_id 是公开标识(会进 trace/日志/relay), 只披露订单状态, 不交付内容。
                        # 取货唯一凭证 = payment_id(144bit 高熵 secret); tx_hash 是公开链上数据, 不作身份证明。
                        # 丢失 payment_id 走人工恢复(不提供 tx_hash 自助取货旁路)。
                        if st in ("paid", "paid_undelivered", "refund_requested"):
                            hint = "内容已就绪，请回传 paymentId 获取；丢失 paymentId 请联系支持人工恢复。"
                        else:
                            hint = "订单待付款，请付款后回传 paymentId 获取内容。"
                        self._send(200, _rpc_result(rid, _working(client_task_id,
                            f"订单状态: {st}。{hint}")))
                        return
                    # sub_dead: 订阅已过期, 落到下面正常开单流程(新 task_id / 新地址 / 新 payment_id)

            # 开单配额(DB 为准): 每 IP pending 上限 + 内存开单限流(辅助)
            ip_pend = db.execute("SELECT COUNT(*) FROM orders WHERE status='pending' AND creator_ip=?",
                                 (ip,)).fetchone()[0]
            if ip_pend >= MAX_PENDING_PER_IP:
                self._send(429, _rpc_error(rid, -32005, "too many open orders from your address"))
                return
            if not _allow_order(ip):
                self._send(429, _rpc_error(rid, -32005, "new order rate limited"))
                return

            # SKU 校验前移到事务外(禁售 SKU 不进入写事务, 避免 rollback 时 HWM 文件与 DB 计数器失配)
            sku = meta.get("x402.product") or DEFAULT_SKU
            if sku not in PRICE_CENTS:
                sku = DEFAULT_SKU
            if sku not in ENABLED_SKUS:
                self._send(200, _rpc_error(rid, -32004, "unknown or unavailable SKU"))
                return

            # 原子开单: BEGIN IMMEDIATE 内完成「回收过期 -> 计数 -> 派生独立地址 -> 插入」
            now_ms = _now_ms()
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                self._send(503, _rpc_error(rid, -32004, "busy, retry later"))
                return
            try:
                db.execute("UPDATE orders SET status='expired' WHERE status='pending' AND expires_at_ms <= ?",
                           (now_ms,))
                npend = db.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
                if npend >= MAX_PENDING_GLOBAL:
                    db.commit()  # 保留已完成的过期回收
                    self._send(503, _rpc_error(rid, -32004, "too many pending orders, retry later"))
                    return
                # 派生下一个独立收款地址(HD 序号单调递增, 每个地址只服务一个订单, 无尾数归属歧义)
                try:
                    mnemonic = load_mnemonic()
                except RuntimeError:
                    db.rollback()
                    self._send(503, _rpc_error(rid, -32004, "payment unavailable, retry later"))
                    return
                # 派生下一个独立收款地址: 从单调计数器取(独立于 orders 行, 删行/清理/回滚不会回退导致地址复用)。
                # index 0 = 归集主地址(RECEIVE_ADDRESS), 子地址从 index 1 开始, 避免撞主地址。
                row = db.execute("SELECT next_index FROM hd_counter WHERE id = 1").fetchone()
                next_idx = row[0]
                if db.execute("SELECT 1 FROM orders WHERE address_index = ?", (next_idx,)).fetchone():
                    db.rollback()
                    log.error("HD index %s 已被占用, 计数器不一致, 拒绝开单", next_idx)
                    self._send(503, _rpc_error(rid, -32004, "payment unavailable, retry later"))
                    return
                db.execute("UPDATE hd_counter SET next_index = ? WHERE id = 1", (next_idx + 1,))
                _bump_hwm(next_idx)   # commit 前落独立水位线(宁可跳号, 不可复用)
                receive_addr = derive_address(mnemonic, next_idx)
                task_id = f"task-{secrets.token_urlsafe(24)}"
                payment_id = f"pay-{secrets.token_urlsafe(24)}"
                # SKU 已在事务外校验并拒绝禁售项, 这里只取价
                amount_cents = PRICE_CENTS[sku]
                access_until_ms = 0  # 订阅有效期在付款确认时起算(见认领 SQL), 开单时仅占位
                db.execute(
                    "INSERT INTO orders (task_id, payment_id, receive_address, address_index, amount_cents, status, "
                    "created_at_ms, expires_at_ms, creator_ip, sku, access_until_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, payment_id, receive_addr, next_idx, amount_cents, "pending",
                     now_ms, now_ms + PAYMENT_WINDOW * 1000, ip, sku, access_until_ms))
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                log.error("开单 IntegrityError: 地址撞车或唯一约束冲突, 需人工核查")
                self._send(503, _rpc_error(rid, -32004, "payment unavailable, retry later"))
                return
            except sqlite3.OperationalError:
                db.rollback()
                self._send(503, _rpc_error(rid, -32004, "busy, retry later"))
                return
            self._send(200, _rpc_result(rid, _payment_required(task_id, payment_id, amount_cents, receive_addr, sku)))


def _ip_key(ip: str) -> str:
    """限流键: IPv6 归一到 /64(防 /64 子网刷单绕过), IPv4 保持原样。"""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 6:
        return str(ipaddress.ip_network(f"{a}/64", strict=False).network_address) + "/64"
    return str(a)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """带并发线程上限的 HTTP 服务: 防慢 TronGrid 查询(8s 超时)占满线程导致内存打爆。

    注意: 只继承 ThreadingHTTPServer(它已含 ThreadingMixIn), 再加 ThreadingMixIn 会 MRO 冲突。"""
    daemon_threads = True
    _sem = threading.BoundedSemaphore(32)  # 并发上限 32

    def process_request(self, request, client_address):
        if not self._sem.acquire(blocking=False):
            # 并发已满: 直接关闭连接。不能走 shutdown_request(它的 finally 会 release, 造成许可记账错误)
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            except Exception:
                pass
            try:
                request.close()
            except Exception:
                pass
            return
        super().process_request(request, client_address)

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            # 只有真正取得许可的工作线程走到这里才 release(acquire 失败路径不经过 shutdown_request)
            self._sem.release()


def main():
    init_db()
    verify_wallet(RECEIVE_ADDRESS)  # index0 自检: 助记词错误拒绝启动, 防止收款到不可控地址
    srv = BoundedThreadingHTTPServer(LISTEN, Handler)
    log.info("A2A 服务启动 http://%s:%s", LISTEN[0], LISTEN[1])
    srv.serve_forever()


if __name__ == "__main__":
    main()

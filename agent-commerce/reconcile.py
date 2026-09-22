#!/usr/bin/env python3
"""孤儿付款对账脚本 — 独立地址架构下的链上事件驱动兜底。

独立地址架构下, 每订单一个专属地址, 付款归属无歧义。本脚本扫描
pending/expired 订单的专属地址, 发现「已付款但用户未回传凭证」的,
自动认领(标记 paid_undelivered), 用户下次轮询即可取货。

每 5 分钟跑一次:
1. 扫描 status IN ('pending','expired') 且近 2 小时内的订单
2. 查每个订单 receive_address 的入账
3. 金额精确匹配 -> 自动认领(paid_undelivered)
4. 金额不符 -> 告警(多付/少付, 记录 from 地址)
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a2a_server import (  # noqa: E402
    DB_PATH,
    DEFAULT_SKU,
    PAYMENT_WINDOW,
    LATE_PAY_GRACE,
    SKU_DAYS,
    STATE_DIR,
    USDT_CONTRACT,
    _now_ms,
    _now_str,
    fetch_incoming_since,
    init_db,
)

LOOKBACK_MS = 24 * 3600 * 1000  # 回看 24 小时(原 2 小时覆盖不到交易所提币风控审核导致的迟到付款)
LOG_PATH = os.path.join(STATE_DIR, "reconcile.log")


def log(msg: str) -> None:
    line = f"[{_now_str()}] {msg}"
    print(line, flush=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> None:
    init_db()  # 单一 schema 权威源, 幂等补全所有列(消除三份迁移清单漂移导致的 IndexError 崩溃)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=8000")
    now_ms = _now_ms()
    orders = db.execute(
        "SELECT * FROM orders WHERE status IN ('pending','expired','refund_requested') "
        "AND created_at_ms >= ?",
        (now_ms - LOOKBACK_MS,),
    ).fetchall()
    if not orders:
        db.close()
        return

    for order in orders:
        addr = order["receive_address"]
        tid = order["task_id"][:12]
        target_raw = order["amount_cents"] * 10000
        window_end = order["created_at_ms"] + (PAYMENT_WINDOW + LATE_PAY_GRACE) * 1000
        try:
            txs = fetch_incoming_since(
                addr, order["created_at_ms"], window_end
            )
        except Exception as e:
            log(f"ERROR: {tid}… TronGrid 查询失败: {e}")
            continue
        found = None
        wrong = None
        for tx in txs:
            txid = tx.get("transaction_id", "")
            if not txid:
                continue
            if tx.get("to") != addr:
                continue
            if tx.get("from") == addr:
                continue
            t = tx.get("type")
            if t is not None and t != "Transfer":
                continue
            tok = tx.get("token_info") or {}
            if tok.get("address") != USDT_CONTRACT:
                continue
            try:
                dec = int(tok.get("decimals", 6))
                raw = int(tx.get("value", "0"))
            except (ValueError, TypeError):
                continue
            if dec != 6:
                continue
            if raw >= target_raw:
                found = txid
                break
            wrong = (txid, raw, tx.get("from", ""))

        if found:
            if order["status"] == "refund_requested":
                # 退款审核中到账: 只记录到账, 不认领(用户要退款不要货), 告警人工处理
                db.execute(
                    "UPDATE orders SET payment_exception = "
                    "COALESCE(payment_exception || char(10), '') || ? "
                    "WHERE task_id=? AND COALESCE(payment_exception,'') NOT LIKE '%REFUND-PAID%'",
                    (f"REFUND-PAID: tx={found} 退款审核中到账, 人工核验后退款", order["task_id"]))
                db.commit()
                log(f"REFUND-PAID-ALERT: {tid}… tx {found[:12]}… 退款审核中到账, 人工核验")
                continue
            try:
                # 订阅有效期从付款确认时刻起算(与 HTTP 认领一致, 防后台认领订阅被判过期)
                sub_days = SKU_DAYS.get(order["sku"] or DEFAULT_SKU, 0)
                new_until = _now_ms() + sub_days * 24 * 3600 * 1000 if sub_days > 0 else 0
                cur = db.execute(
                    "UPDATE orders SET status='paid_undelivered', paid_at=?, tx_hash=?, access_until_ms=? "
                    "WHERE task_id=? AND status IN ('pending','expired') AND tx_hash IS NULL",
                    (_now_str(), found, new_until, order["task_id"]),
                )
                db.commit()
                if cur.rowcount == 1:
                    log(f"AUTO-CLAIM: {tid}… tx {found[:12]}… -> paid_undelivered")
            except sqlite3.IntegrityError:
                db.rollback()
                log(f"ALERT: {tid}… tx {found[:12]}… 已被其他订单认领, 人工核查!")
        elif wrong:
            log(
                f"AMOUNT-MISMATCH(少付): {tid}… addr={addr} tx {wrong[0][:12]}… "
                f"raw={wrong[1]} (期望 ≥ {target_raw}) from {wrong[2]}"
            )
    db.close()


if __name__ == "__main__":
    main()

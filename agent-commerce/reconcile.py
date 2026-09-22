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
    PAYMENT_WINDOW,
    STATE_DIR,
    USDT_CONTRACT,
    _now_ms,
    _now_str,
    fetch_incoming_since,
)

LOOKBACK_MS = 2 * 3600 * 1000  # 2 小时
LOG_PATH = os.path.join(STATE_DIR, "reconcile.log")


def log(msg: str) -> None:
    line = f"[{_now_str()}] {msg}"
    print(line, flush=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> None:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=8000")
    now_ms = _now_ms()
    orders = db.execute(
        "SELECT * FROM orders WHERE status IN ('pending','expired') "
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
        try:
            txs = fetch_incoming_since(
                addr, order["created_at_ms"], order["created_at_ms"] + PAYMENT_WINDOW * 1000
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
            try:
                cur = db.execute(
                    "UPDATE orders SET status='paid_undelivered', paid_at=?, tx_hash=? "
                    "WHERE task_id=? AND status IN ('pending','expired') AND tx_hash IS NULL",
                    (_now_str(), found, order["task_id"]),
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

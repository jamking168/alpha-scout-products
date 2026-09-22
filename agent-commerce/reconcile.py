#!/usr/bin/env python3
"""孤儿付款对账脚本 — 把「用户轮询驱动」补上「链上事件驱动」的兜底。

每 5 分钟跑一次:
1. 拉最近 2 小时收款地址的 USDT-TRC20 入账
2. 找出 tx_hash 未被任何订单认领的
3. 按金额尾数反查 pending/expired 订单
   - 唯一匹配 -> 自动认领(标记 paid_undelivered,等用户下次轮询取货)
   - 多个匹配 -> 告警(人工裁决)
   - 无匹配 -> 告警(孤儿付款,记录 from 地址用于退款)
"""
import sys
import os
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a2a_server import (RECEIVE_ADDRESS, USDT_CONTRACT, DB_PATH,
                        STATE_DIR, _now_ms, _now_str, fetch_incoming_since)

LOOKBACK_MS = 2 * 3600 * 1000  # 2 小时
LOG_PATH = os.path.join(STATE_DIR, "reconcile.log")


def log(msg):
    line = f"[{_now_str()}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    min_ts = _now_ms() - LOOKBACK_MS
    try:
        txs = fetch_incoming_since(min_ts)
    except Exception as e:
        log(f"ERROR: TronGrid 查询失败: {e}")
        db.close()
        return

    for tx in txs:
        txid = tx.get("transaction_id", "")
        if not txid:
            continue
        if tx.get("to") != RECEIVE_ADDRESS:
            continue
        if tx.get("from") == RECEIVE_ADDRESS:
            continue
        tok = tx.get("token_info") or {}
        if tok.get("address") != USDT_CONTRACT:
            continue
        # 已认领?
        if db.execute("SELECT 1 FROM orders WHERE tx_hash=?", (txid,)).fetchone():
            continue
        try:
            dec = int(tok.get("decimals", 6))
            raw = int(tx.get("value", "0"))
        except (ValueError, TypeError):
            continue
        if dec != 6:
            continue
        amount_cents = raw // 10000
        from_addr = tx.get("from", "")
        blk_ms = int(tx.get("block_timestamp", 0) or 0)
        # 反查 pending/expired 订单
        cands = db.execute(
            "SELECT * FROM orders WHERE amount_cents=? AND status IN ('pending','expired') "
            "AND created_at_ms <= ? AND expires_at_ms >= ?",
            (amount_cents, blk_ms, blk_ms - LOOKBACK_MS)).fetchall()
        if len(cands) == 0:
            log(f"ORPHAN: tx {txid[:12]}… 金额 {amount_cents}分 无匹配订单 (from {from_addr})")
            continue
        if len(cands) > 1:
            log(f"AMBIGUOUS: tx {txid[:12]}… 金额 {amount_cents}分 匹配 {len(cands)} 单,人工裁决")
            continue
        # 唯一匹配 -> 自动认领
        row = cands[0]
        cur = db.execute(
            "UPDATE orders SET status='paid_undelivered', paid_at=?, tx_hash=? "
            "WHERE task_id=? AND status IN ('pending','expired') AND tx_hash IS NULL",
            (_now_str(), txid, row["task_id"]))
        db.commit()
        if cur.rowcount == 1:
            log(f"AUTO-CLAIM: tx {txid[:12]}… -> order {row['task_id'][:12]}… (金额 {amount_cents}分)")
        else:
            log(f"SKIP: tx {txid[:12]}… order {row['task_id'][:12]}… 认领失败(可能并发已认领)")
    db.close()


if __name__ == "__main__":
    main()

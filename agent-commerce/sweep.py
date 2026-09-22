#!/usr/bin/env python3
"""归集脚本: 把订单专属子地址收到的 USDT 归集到主地址。

独立地址架构下, 每订单一个 HD 派生子地址。用户付款后 USDT 落在子地址上,
需要定期归集到主地址(RECEIVE_ADDRESS)统一管理。

安全边界(真金白银红线):
  - 默认 --dry-run: 只扫描 + 构建交易 + 签名, 不广播。
  - 真实归集需显式 --execute(广播到主网), 由用户逐单确认后执行。
  - 子地址转出 USDT 需要能量/带宽(TRC20 转账约 31,895 能量 ≈ 13.3 TRX),
    子地址须先有少量 TRX 供烧, 否则归集失败(insufficient balance)。

用法:
  python3 sweep.py              # dry-run: 扫描 + 构建签名, 不广播(默认)
  python3 sweep.py --execute    # 真实广播归集(须用户确认)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from tronpy import Tron
from tronpy.keys import PrivateKey
from tronpy.providers import HTTPProvider

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hd_wallet  # noqa: E402
from a2a_server import (  # noqa: E402
    DB_PATH,
    RECEIVE_ADDRESS,
    STATE_DIR,
    USDT_CONTRACT,
    _now_str,
    db_conn,
)

LOG_PATH = os.path.join(STATE_DIR, "sweep.log")
FEE_LIMIT_SUN = 30_000_000  # 30 TRX 烧 TRX 上限(能量不足时兜底)


def log(msg: str) -> None:
    line = f"[{_now_str()}] {msg}"
    print(line, flush=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def ensure_schema(db) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(orders)")}
    if "swept" not in cols:
        db.execute("ALTER TABLE orders ADD COLUMN swept INTEGER DEFAULT 0")
    if "sweep_tx" not in cols:
        db.execute("ALTER TABLE orders ADD COLUMN sweep_tx TEXT")
    if "sweep_at" not in cols:
        db.execute("ALTER TABLE orders ADD COLUMN sweep_at TEXT")
    db.commit()


def pending_orders(db):
    return db.execute(
        "SELECT * FROM orders WHERE status='paid' AND artifact IS NOT NULL "
        "AND COALESCE(swept, 0) = 0"
    ).fetchall()


def build_transfer(client: Tron, from_addr: str, priv: bytes, to_addr: str, amount_raw: int):
    contract = client.get_contract(USDT_CONTRACT)
    txn = (
        contract.functions.transfer(to_addr, amount_raw)
        .with_owner(from_addr)
        .fee_limit(FEE_LIMIT_SUN)
    )
    # 离线构建 + 签名: 绕过 getsignweight 网络调用(公共 TronGrid 严格 429 限流)。
    # 子地址是普通账户(owner 单私钥), 无需链上权限查询即可离线签名。
    ref_block_id = client.get_latest_solid_block_id()
    built = txn.build(offline=True, ref_block_id=ref_block_id)
    signed = built.sign(PrivateKey(priv))
    signed._client = client  # 广播时挂回 client
    return signed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="真实广播(默认 dry-run)")
    args = ap.parse_args()

    try:
        mnemonic = hd_wallet.load_mnemonic()
    except RuntimeError as e:
        log(f"ERROR: {e}")
        return

    client = Tron(HTTPProvider(endpoint_uri="https://api.trongrid.io"))
    with db_conn() as db:
        ensure_schema(db)
        orders = pending_orders(db)
        if not orders:
            log("无待归集订单")
            return
        log(f"待归集订单 {len(orders)} 笔")
        for order in orders:
            idx = order["address_index"]
            from_addr = order["receive_address"]
            amount_raw = order["amount_cents"] * 10000
            tid = order["task_id"][:12]
            try:
                priv = hd_wallet.derive_private_key(mnemonic, idx)
                txn = build_transfer(client, from_addr, priv, RECEIVE_ADDRESS, amount_raw)
            except Exception as e:
                log(f"BUILD FAIL {tid}… addr={from_addr}: {e}")
                continue
            if not args.execute:
                log(f"DRY-RUN {tid}… from={from_addr} -> {RECEIVE_ADDRESS} amount_raw={amount_raw}")
                continue
            try:
                result = txn.broadcast()
                txid = result.get("txid")
                db.execute(
                    "UPDATE orders SET swept=1, sweep_tx=?, sweep_at=? WHERE task_id=?",
                    (txid, _now_str(), order["task_id"]),
                )
                db.commit()
                log(f"SWEPT {tid}… tx={txid}")
            except Exception as e:
                log(f"SWEEP FAIL {tid}…: {e}")


if __name__ == "__main__":
    main()

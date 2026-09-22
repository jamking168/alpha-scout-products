#!/usr/bin/env python3
"""归集脚本: 把订单专属子地址收到的 USDT 归集到主地址。

独立地址架构下, 每订单一个 HD 派生子地址。用户付款后 USDT 落在子地址上,
需要定期归集到主地址(RECEIVE_ADDRESS)统一管理。

安全边界(真金白银红线):
  - 默认 --dry-run: 只扫描 + 构建交易 + 签名, 不广播。
  - 真实归集需显式 --execute(广播到主网)。
  - 归集后必须验证链上回执(receipt.result == SUCCESS)才标记 swept, 否则资金滞留。
  - 每笔归集烧约 13.3 TRX, 低于 MIN_SWEEP_RAW 阈值的不归集(等积累, 摊薄成本)。

用法:
  python3 sweep.py                    # dry-run(默认)
  python3 sweep.py --execute          # 真实广播归集
  python3 sweep.py --execute --min-sweep 50000000   # 自定义归集阈值(raw, 默认 $50)
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sqlite3
import sys
import time

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
LOCK_PATH = os.path.join(STATE_DIR, "sweep.lock")
FEE_LIMIT_SUN = 8_000_000        # 8 TRX 烧 TRX 上限(实际约 3.2 TRX + 波动余量, 原 30 TRX 过宽)
TRX_FUND_SUN = 5_000_000         # 充值目标 5 TRX: 3.2 TRX 转账 + 1.8 TRX 余量(原 15 TRX 会沉没 11.8 TRX/单)
TRX_FUND_MIN_DEFICIT = 500_000   # 差额 < 0.5 TRX 不补(避免碎片充值)
MIN_SWEEP_RAW = 4_500_000        # 归集阈值 $4.5: 每地址只收一张 $5 订单, 单笔即归集(原 $50 会让归集从不发生)
TRX_DUST_SUN = 1_500_000         # 归集后子地址残余 TRX > 1.5 则回收(留 1.1 TRX 覆盖回收转账成本)
MAIN_TRX_FLOOR_SUN = 50_000_000  # 主地址 TRX 低于 50 则停止充值(防主地址 TRX 漏光)


def log(msg: str) -> None:
    line = f"[{_now_str()}] {msg}"
    print(line, flush=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def acquire_lock():
    """flock 非阻塞锁: 防止两个归集实例并发重复广播。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("另一个 sweep 实例正在运行, 退出")
        sys.exit(0)
    return fh


def ensure_schema(db) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(orders)")}
    if "swept" not in cols:
        db.execute("ALTER TABLE orders ADD COLUMN swept INTEGER DEFAULT 0")
    if "sweep_tx" not in cols:
        db.execute("ALTER TABLE orders ADD COLUMN sweep_tx TEXT")
    if "sweep_at" not in cols:
        db.execute("ALTER TABLE orders ADD COLUMN sweep_at TEXT")
    if "fund_tx" not in cols:
        db.execute("ALTER TABLE orders ADD COLUMN fund_tx TEXT")
    db.commit()


def pending_orders(db):
    """扫所有收到钱但未归集的订单(含 paid_undelivered / refund_requested, 这些地址上也有真金白银)。

    含 NO_BALANCE 复查: 余额 0 标记过的地址, 后续可能又收到钱(重复付款/延迟到账), 必须复查。"""
    return db.execute(
        "SELECT * FROM orders WHERE tx_hash IS NOT NULL AND receive_address IS NOT NULL "
        "AND (COALESCE(swept, 0) = 0 OR sweep_tx = 'NO_BALANCE')"
    ).fetchall()


def get_trx_balance(client: Tron, addr: str):
    """查 TRX 余额(sun)。返回 None 表示查询失败(不能当成余额 0)。"""
    try:
        acc = client.get_account(addr)
        return int(acc.get("balance", 0))
    except Exception:
        return None


def get_usdt_balance(client: Tron, addr: str):
    """查 USDT 余额(raw)。返回 None 表示查询失败。"""
    try:
        contract = client.get_contract(USDT_CONTRACT)
        return int(contract.functions.balanceOf(addr))
    except Exception:
        return None


def build_transfer(client: Tron, from_addr: str, priv: bytes, to_addr: str, amount_raw: int):
    contract = client.get_contract(USDT_CONTRACT)
    txn = (
        contract.functions.transfer(to_addr, amount_raw)
        .with_owner(from_addr)
        .fee_limit(FEE_LIMIT_SUN)
    )
    # 离线构建 + 签名: 绕过 getsignweight 网络调用(公共 TronGrid 严格 429 限流)。
    ref_block_id = client.get_latest_solid_block_id()
    built = txn.build(offline=True, ref_block_id=ref_block_id)
    signed = built.sign(PrivateKey(priv))
    signed._client = client
    return signed


def build_trx_transfer(client: Tron, from_addr: str, priv: bytes, to_addr: str, amount_sun: int):
    txn = client.trx.transfer(from_addr, to_addr, amount_sun).fee_limit(1_000_000)
    ref_block_id = client.get_latest_solid_block_id()
    built = txn.build(offline=True, ref_block_id=ref_block_id)
    signed = built.sign(PrivateKey(priv))
    signed._client = client
    return signed


def wait_for_result(client: Tron, txid: str, timeout: int = 90):
    """轮询链上回执, 返回 (success: bool, receipt: dict|None)。广播成功 ≠ 执行成功。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = client.get_transaction_info(txid)
            if info and info.get("receipt"):
                receipt = info["receipt"]
                return receipt.get("result") == "SUCCESS", receipt
        except Exception:
            pass
        time.sleep(3)
    return False, None  # 超时未确认, 视为失败(保守, 不标 swept)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="真实广播(默认 dry-run)")
    ap.add_argument("--min-sweep", type=int, default=MIN_SWEEP_RAW,
                    help=f"归集阈值(raw, 默认 {MIN_SWEEP_RAW} = $50)")
    args = ap.parse_args()

    lock = acquire_lock()
    try:
        mnemonic = hd_wallet.load_mnemonic()
    except RuntimeError as e:
        log(f"ERROR: {e}")
        return

    # 启动自检: index0 == RECEIVE_ADDRESS, 错了就往黑洞打 TRX
    hd_wallet.verify_wallet(RECEIVE_ADDRESS)

    client = Tron(HTTPProvider(endpoint_uri="https://api.trongrid.io"))
    with db_conn() as db:
        ensure_schema(db)
        orders = pending_orders(db)
        if not orders:
            log("无待归集订单")
            return
        log(f"待归集订单 {len(orders)} 笔, 归集阈值 ${args.min_sweep / 1e6:.0f}")
        for order in orders:
            idx = order["address_index"]
            from_addr = order["receive_address"]
            tid = order["task_id"][:12]
            if idx is None or from_addr is None:
                log(f"SKIP {tid}… 缺 address_index/receive_address")
                continue
            try:
                priv = hd_wallet.derive_private_key(mnemonic, idx)
                main_priv = hd_wallet.derive_private_key(mnemonic, 0)
            except Exception as e:
                log(f"BUILD FAIL {tid}… {e}")
                continue
            # 0.5 上次归集广播后崩溃恢复: sweep_tx 已落盘但未确认, 验回执, 不重复归集
            if order["sweep_tx"] and order["sweep_tx"] != "NO_BALANCE" and not order["swept"]:
                ok, receipt = wait_for_result(client, order["sweep_tx"])
                if ok:
                    db.execute("UPDATE orders SET swept=1, sweep_at=? WHERE task_id=?",
                               (_now_str(), order["task_id"]))
                    db.commit()
                    log(f"RECONFIRM SWEPT {tid}… tx={order['sweep_tx'][:12]}… 上次已上链, 补记 swept")
                else:
                    log(f"HOLD {tid}… 上次归集 tx={order['sweep_tx'][:12]}… 状态未知, 人工核查")
                continue
            # 1. 查 USDT 实际余额(不是订单金额, 多付/重复付款都归集)
            bal = get_usdt_balance(client, from_addr)
            if bal is None:
                log(f"SKIP {tid}… USDT 余额查询失败(429/超时), 保留待重试")
                continue
            if bal <= 0:
                # 余额 0 不标 swept(可能是延迟到账), 标 NO_BALANCE + sweep_at 作上次检查时间, 保留复查
                log(f"SKIP {tid}… 余额 0(保留复查, 地址可能后续到账)")
                db.execute(
                    "UPDATE orders SET sweep_tx='NO_BALANCE', sweep_at=? WHERE task_id=?",
                    (_now_str(), order["task_id"]),
                )
                db.commit()
                continue
            if bal < args.min_sweep:
                log(f"SKIP {tid}… 余额 {bal / 1e6:.2f} USDT < 阈值 ${args.min_sweep / 1e6:.0f}, 等积累")
                continue
            # 2. TRX 差额充值(幂等: 已有 fund_tx 则跳过)
            trx = get_trx_balance(client, from_addr)
            if trx is None:
                log(f"SKIP {tid}… TRX 余额查询失败, 保留待重试")
                continue
            deficit = TRX_FUND_SUN - trx
            need_fund = deficit > TRX_FUND_MIN_DEFICIT and (order["fund_tx"] is None)
            # 3. dry-run
            if not args.execute:
                fund_note = f" + 充值 {deficit / 1e6:.1f} TRX" if need_fund else ""
                log(f"DRY-RUN {tid}… from={from_addr} -> {RECEIVE_ADDRESS} USDT={bal / 1e6:.2f}{fund_note}")
                continue
            # 4. 充值(差额, 广播后先记 fund_tx 再验回执, 防崩溃重跑重复充值)
            if need_fund:
                # 充值前校验主地址余额: 主地址 TRX 不足则停止本轮充值(防主地址 TRX 漏光)
                main_trx = get_trx_balance(client, RECEIVE_ADDRESS)
                if main_trx is None:
                    log(f"SKIP {tid}… 主地址 TRX 查询失败, 保留待重试")
                    continue
                if main_trx - deficit < MAIN_TRX_FLOOR_SUN:
                    log(f"HALT {tid}… 主地址 TRX {main_trx/1e6:.1f} 不足(下限 {MAIN_TRX_FLOOR_SUN/1e6:.0f}), "
                        f"停止本轮充值, 请人工补 TRX")
                    break  # 主地址没钱了, 后续订单也充不了, 直接停(不是 continue)
                try:
                    fund_txn = build_trx_transfer(client, RECEIVE_ADDRESS, main_priv, from_addr, deficit)
                    ftx = fund_txn.txid  # 离线计算 txid, 广播前先落盘(防广播后崩溃导致重复充值)
                    db.execute("UPDATE orders SET fund_tx=? WHERE task_id=?", (ftx, order["task_id"]))
                    db.commit()
                    fr = fund_txn.broadcast()
                    log(f"TRX-FUND {tid}… tx={ftx} amount={deficit / 1e6:.2f}")
                    ok, _ = wait_for_result(client, ftx)
                    if not ok:
                        log(f"TRX-FUND 未确认 {tid}… 保留待重试(已有 fund_tx, 不会重复充)")
                        continue
                except Exception as e:
                    log(f"TRX-FUND FAIL {tid}…: {e}")
                    continue
            # 5. 归集 USDT(全部实际余额)
            try:
                usdt_txn = build_transfer(client, from_addr, priv, RECEIVE_ADDRESS, bal)
                txid = usdt_txn.txid  # 离线计算 txid, 广播前先落盘(防广播后崩溃, 下轮 RECONFIRM 不重复归集)
                db.execute("UPDATE orders SET sweep_tx=? WHERE task_id=?", (txid, order["task_id"]))
                db.commit()
                result = usdt_txn.broadcast()
            except Exception as e:
                log(f"SWEEP FAIL {tid}…: {e}")
                continue
            # 6. 验回执: 只有链上 SUCCESS 才标 swept(广播成功 ≠ 执行成功)
            ok, receipt = wait_for_result(client, txid)
            if ok:
                db.execute(
                    "UPDATE orders SET swept=1, sweep_tx=?, sweep_at=? WHERE task_id=?",
                    (txid, _now_str(), order["task_id"]),
                )
                db.commit()
                log(f"SWEPT {tid}… tx={txid} amount={bal / 1e6:.2f}")
                # 回收残余 TRX(留 1.1 TRX 覆盖这笔 TRX 转账本身的带宽/激活成本, 防沉没)
                rest = get_trx_balance(client, from_addr)
                if rest is not None and rest > TRX_DUST_SUN:
                    try:
                        back = rest - 1_100_000
                        bt = build_trx_transfer(client, from_addr, priv, RECEIVE_ADDRESS, back)
                        br = bt.broadcast()
                        log(f"TRX-RECLAIM {tid}… tx={br.get('txid')} amount={back / 1e6:.2f}")
                    except Exception as e:
                        log(f"TRX-RECLAIM FAIL {tid}…: {e}")  # 非致命, 下轮再试
            else:
                log(f"SWEEP REVERT {tid}… tx={txid} receipt={receipt}, 保留待人工核查")


if __name__ == "__main__":
    main()

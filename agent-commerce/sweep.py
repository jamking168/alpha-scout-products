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
import zlib

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
    _now_ms,
    _now_str,
    db_conn,
)

LOG_PATH = os.path.join(STATE_DIR, "sweep.log")
LOCK_PATH = os.path.join(STATE_DIR, "sweep.lock")
FEE_LIMIT_SUN = 8_000_000        # 8 TRX 烧 TRX 上限(实际约 3.2 TRX + 波动余量, 原 30 TRX 过宽)
TRX_FUND_SUN = 9_000_000         # 充值目标 9 TRX: 必须严格 > FEE_LIMIT_SUN(8)+带宽余量, 否则能量价格波动期必 OUT_OF_ENERGY
TRX_FUND_MIN_DEFICIT = 500_000   # 差额 < 0.5 TRX 不补(避免碎片充值)
MIN_SWEEP_RAW = 2_500_000        # 归集阈值 $2.5: 覆盖 9 TRX 成本(~$1.8) + 余量(每地址只收一单, 无"等积累")
DUST_RAW = 1_500_000             # < $1.5 视为尘埃: 归集成本 > 币值, 标 DUST 终结不再复查(防 TronGrid 429 累积)
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
    """扫收到钱但未归集的订单(含 paid_undelivered / refund_requested, 这些地址上也有真金白银)。

    排除 DUST/REVIEW 终结态(归集成本 > 币值, 永久放弃复查)。
    NO_BALANCE 地址做时间衰减复查: 创建 7 天内才复查(7 天后仍无钱基本不会再有了),
    否则订单累积后每轮 sweep 的 TronGrid 调用线性增长, 必撞 429。"""
    cutoff_ms = _now_ms() - 7 * 24 * 3600 * 1000
    return db.execute(
        "SELECT * FROM orders WHERE tx_hash IS NOT NULL AND receive_address IS NOT NULL "
        "AND COALESCE(sweep_tx, '') NOT IN ('DUST', 'REVIEW') "
        "AND (COALESCE(swept, 0) = 0 "
        "     OR (sweep_tx = 'NO_BALANCE' AND created_at_ms >= ?))",
        (cutoff_ms,),
    ).fetchall()


def orphan_orders(db):
    """孤儿扫描: 未认领(tx_hash IS NULL)但地址可能有余额的订单(付款超 7h 窗/对账漏扫导致)。

    排除 ORPHAN_RESOLVED(人工已结案), 而非排除所有 ORPHAN(否则标记一次即永久失明)。"""
    cutoff_ms = _now_ms() - 365 * 24 * 3600 * 1000  # 放宽到 1 年(资金滞留不应因窗口消失)
    return db.execute(
        "SELECT * FROM orders WHERE receive_address IS NOT NULL AND tx_hash IS NULL "
        "AND status IN ('pending','expired','refund_requested') AND created_at_ms >= ? "
        "AND COALESCE(payment_exception,'') NOT LIKE 'ORPHAN_RESOLVED%'",
        (cutoff_ms,),
    ).fetchall()


def recent_swept_orders(db):
    """成功归集后 90 天内的地址, 复查是否再入账(重复付款/延迟到账的第二笔)。

    不把"某次归集成功"解释为"这个地址终生不再有余额": 归集后仍有到账会重新打开归集。
    90 天覆盖交易所提币风控 + 用户手动重复付款的长尾延迟。

    防 429: 分层复查。7 天内每轮查; 7-90 天用 task_id CRC32 分桶, 每 24 轮(≈每天)查一次,
    避免"全量每轮扫"导致 TronGrid 调用无界增长 → 吃光配额 → 全局归集瘫痪。"""
    rows = db.execute(
        "SELECT * FROM orders WHERE swept=1 AND sweep_tx != 'NO_BALANCE' "
        "AND sweep_at IS NOT NULL AND receive_address IS NOT NULL AND address_index IS NOT NULL"
    ).fetchall()
    out = []
    hour_now = int(time.time() // 3600) % 24
    for r in rows:
        age = _age_seconds(r["sweep_at"])
        if age >= 90 * 24 * 3600:
            continue
        if age < 7 * 24 * 3600:
            out.append(r)  # 新鲜期: 每轮查
        else:
            bucket = zlib.crc32(r["task_id"].encode()) % 24  # 长尾期: 分桶摊薄
            if bucket == hour_now:
                out.append(r)
    return out


def get_trx_balance(client: Tron, addr: str):
    """查 TRX 余额(sun)。返回 None 表示查询失败(不能当成余额 0); 返回 0 表示未激活/确实无 TRX。

    关键陷阱: 不能用宽泛子串 "account"/"not found" 判未激活——tronpy 错误消息含 endpoint
    /wallet/getaccount, 任何 500/502/超时都会命中而被误判为余额 0 → 重复充值 TRX。"""
    try:
        acc = client.get_account(addr)
        return int(acc.get("balance", 0))
    except Exception as e:
        if type(e).__name__ == "AddressNotFound":
            return 0
        s = str(e).lower()
        # 只认明确"账户不存在"措辞, 不用宽泛子串
        if "account not found" in s or "does not exist" in s:
            return 0
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


def tx_exists(client: Tron, txid: str):
    """返回 True=链上存在, False=链上明确不存在, None=查询失败(不确定)。

    关键陷阱: tronpy 对未上链 txid 的 get_transaction_info 返回 {} (HTTP 200, 非 None,
    不抛异常)。必须用 `if not info` 判定, 否则空 dict 被当成"存在" → 归集恢复分支恒不触发。"""
    try:
        info = client.get_transaction_info(txid)
        if not info:  # {} / None 都视为「链上无此交易」
            return False
        return True
    except Exception as e:
        if type(e).__name__ == "TransactionNotFound":
            return False
        s = str(e).lower()
        # 只认明确"交易不存在"措辞, 不用宽泛 "not found"(会命中 404 Not Found 网络错误)
        if "transaction not found" in s:
            return False
        return None


def _age_seconds(ts_str):
    """把 _now_str() 的 ISO 时间戳解析成距今秒数; 解析失败返回 0(不触发自动恢复, 保守)。"""
    if not ts_str:
        return 0
    try:
        from datetime import datetime, timezone
        ts = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, time.time() - dt.timestamp())
    except Exception:
        return 0.0


def _sweep_single(client: Tron, mnemonic: str, task_id: str, execute: bool) -> None:
    """单笔强制归集: 人工确认归属后, 归集指定订单地址上的全部 USDT。

    孤儿订单(tx_hash IS NULL 但地址有余额)的人工出路: 把"拿助记词手写脚本"降为"跑一条命令"。
    用法: python3 sweep.py --task-id=<task_id> [--execute]  (默认 dry-run)"""
    with db_conn() as db:
        row = db.execute("SELECT * FROM orders WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        log(f"ERROR: 无此订单 {task_id}")
        return
    idx = row["address_index"]
    addr = row["receive_address"]
    if idx is None or addr is None:
        log(f"ERROR: 订单 {task_id} 缺 address_index/receive_address")
        return
    priv = hd_wallet.derive_private_key(mnemonic, idx)
    main_priv = hd_wallet.derive_private_key(mnemonic, 0)
    tid = task_id[:12]
    bal = get_usdt_balance(client, addr)
    if bal is None:
        log(f"ERROR: {tid}… USDT 余额查询失败(429/超时), 稍后重试")
        return
    if bal <= 0:
        log(f"SKIP {tid}… 地址无 USDT 余额")
        return
    if not execute:
        log(f"DRY-RUN {tid}… from={addr} -> {RECEIVE_ADDRESS} USDT={bal / 1e6:.2f}")
        return
    # 充值 TRX(若不足): 复用主循环差额充值逻辑
    trx = get_trx_balance(client, addr)
    if trx is None:
        log(f"ERROR: {tid}… TRX 余额查询失败")
        return
    deficit = TRX_FUND_SUN - trx
    if deficit > TRX_FUND_MIN_DEFICIT:
        main_trx = get_trx_balance(client, RECEIVE_ADDRESS)
        if main_trx is None or main_trx - deficit < MAIN_TRX_FLOOR_SUN:
            log(f"ERROR: {tid}… 主地址 TRX 不足, 请先补 TRX")
            return
        try:
            fund_txn = build_trx_transfer(client, RECEIVE_ADDRESS, main_priv, addr, deficit)
            ftx = fund_txn.txid
            # 广播前先持久化 fund_tx(防崩溃重跑重复充值, 与主循环幂等一致)
            with db_conn() as db:
                db.execute("UPDATE orders SET fund_tx=? WHERE task_id=?", (ftx, task_id))
                db.commit()
            fund_txn.broadcast()
            wait_for_result(client, ftx)
            log(f"TRX-FUND {tid}… amount={deficit / 1e6:.2f}")
        except Exception as e:
            log(f"TRX-FUND FAIL {tid}…: {e}")
            return
    # 归集 USDT
    try:
        usdt_txn = build_transfer(client, addr, priv, RECEIVE_ADDRESS, bal)
        txid = usdt_txn.txid
        with db_conn() as db:
            db.execute("UPDATE orders SET sweep_tx=?, sweep_at=? WHERE task_id=?", (txid, _now_str(), task_id))
            db.commit()
        usdt_txn.broadcast()
    except Exception as e:
        log(f"SWEEP FAIL {tid}…: {e}")
        return
    ok, _ = wait_for_result(client, txid)
    if ok:
        with db_conn() as db:
            db.execute("UPDATE orders SET swept=1, sweep_tx=?, sweep_at=? WHERE task_id=?",
                       (txid, _now_str(), task_id))
            # 审计痕迹: 孤儿已人工归集结案。前置 ORPHAN_RESOLVED 前缀, 使 orphan_orders 前缀匹配终结扫描
            db.execute(
                "UPDATE orders SET payment_exception = 'ORPHAN_RESOLVED: ' || COALESCE(payment_exception,'') || char(10) || ? WHERE task_id=?",
                (f"手动归集 tx={txid} at={_now_str()}", task_id))
            db.commit()
        log(f"SWEPT {tid}… tx={txid} amount={bal / 1e6:.2f}")
    else:
        log(f"SWEEP 未确认 {tid}… tx={txid}, 保留待复核")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="真实广播(默认 dry-run)")
    ap.add_argument("--min-sweep", type=int, default=MIN_SWEEP_RAW,
                    help=f"归集阈值(raw, 默认 {MIN_SWEEP_RAW} = $50)")
    ap.add_argument("--task-id", type=str, default=None,
                    help="单笔强制归集指定订单地址的 USDT(孤儿资金人工出路; 配合 --execute 才真实广播)")
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

    # 单笔强制归集: 人工确认归属后, 归集指定订单地址(孤儿资金出路), 不跑全量
    if args.task_id:
        _sweep_single(client, mnemonic, args.task_id, args.execute)
        return

    with db_conn() as db:
        ensure_schema(db)
        # 孤儿扫描: 未认领但地址有余额的订单(付款超窗/对账漏扫) → 告警, 不自动归集(归属未定, 人工决定)
        for o in orphan_orders(db):
            addr = o["receive_address"]
            if not addr:
                continue
            bal = get_usdt_balance(client, addr)
            if bal is None:
                log(f"ORPHAN-RECHECK-FAIL {o['task_id'][:12]}… 余额查询失败(429/超时), 下轮重试")
                continue
            if bal > 0:
                log(f"ORPHAN-ALERT: {o['task_id'][:12]}… addr={addr} 未认领但有 USDT 余额 {bal/1e6:.6f}, 人工决定补发/退款")
                # 追加不覆写: payment_exception 里可能已有少付明细(人工退款唯一凭证), 覆写会丢凭证
                # 余额不变时不重复追加(避免每轮刷屏)
                db.execute(
                    "UPDATE orders SET payment_exception = "
                    "COALESCE(payment_exception || char(10), '') || ? "
                    "WHERE task_id=? AND COALESCE(payment_exception,'') NOT LIKE '%ORPHAN: bal=' || ? || '%'",
                    (f"ORPHAN: bal={bal} at={_now_str()}", o["task_id"], str(bal)))
                db.commit()
        orders = pending_orders(db)
        if orders:
            log(f"待归集订单 {len(orders)} 笔, 归集阈值 ${args.min_sweep / 1e6:.0f}")
        else:
            log("无待归集订单")
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
                    continue
                # 区分"已确认执行失败"(receipt 非 None 且 result != SUCCESS) vs "未决"(receipt None)
                if receipt is not None:
                    # 链上已确认执行失败(OUT_OF_ENERGY/REVERT): 记录 + 有界重试(最多 3 次, 防反复烧手续费)
                    fail_cnt = (order["payment_exception"] or "").count("SWEEP-FAILED attempt")
                    if fail_cnt >= 3:
                        log(f"SWEEP-FAILED-HOLD {tid}… 连续 {fail_cnt} 次链上执行失败, 转人工处理(资源/权限核查)")
                        continue
                    log(f"SWEEP-FAILED {tid}… tx={order['sweep_tx'][:12]}… 链上执行失败(result={receipt.get('result')}), 清 sweep_tx+fund_tx 重试({fail_cnt+1}/3)")
                    db.execute(
                        "UPDATE orders SET sweep_tx=NULL, sweep_at=NULL, fund_tx=NULL, "
                        "payment_exception = COALESCE(payment_exception || char(10), '') || ? WHERE task_id=?",
                        (f"SWEEP-FAILED attempt: result={receipt.get('result')} at={_now_str()}", order["task_id"]))
                    db.commit()
                    # 不 continue, 继续走下面正常归集流程(重新查余额/充值/归集)
                else:
                    # 自动恢复: 广播后未上链(网络抖动/交易过期) → 链上查无此 tx 且超 5min, 清 sweep_tx 重走归集
                    exists = tx_exists(client, order["sweep_tx"])
                    if exists is False and _age_seconds(order["sweep_at"]) > 300:
                        db.execute("UPDATE orders SET sweep_tx=NULL, sweep_at=NULL WHERE task_id=?",
                                   (order["task_id"],))
                        db.commit()
                        log(f"RECOVER {tid}… 上次归集 tx={order['sweep_tx'][:12]}… 链上查无此交易, 清 sweep_tx 重新归集")
                        # 不 continue, 继续走下面正常归集流程
                    else:
                        log(f"HOLD {tid}… 上次归集 tx={order['sweep_tx'][:12]}… 状态未知(存在={exists}), 人工核查")
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
                if bal < DUST_RAW:
                    # 尘埃: 归集成本 > 币值, 永久放弃并终结复查(避免每轮打 TronGrid 累积 429)
                    log(f"DUST {tid}… 余额 {bal / 1e6:.4f} USDT < ${DUST_RAW / 1e6:.2f}, "
                        f"标记 DUST 不再复查(归集成本高于币值)")
                    db.execute(
                        "UPDATE orders SET swept=1, sweep_tx='DUST', sweep_at=? WHERE task_id=?",
                        (_now_str(), order["task_id"]),
                    )
                    db.commit()
                else:
                    # 灰区: 余额低于归集阈值但高于尘埃, 不自动归集(成本不划算), 标 REVIEW 终结复查
                    # (不写状态会让 swept=0 永久驻留 pending_orders, 每轮打 TronGrid → 429 → 全局瘫痪)
                    log(f"REVIEW {tid}… 余额 {bal / 1e6:.2f} USDT 在灰区 "
                        f"[${DUST_RAW / 1e6:.2f}, ${args.min_sweep / 1e6:.2f}), 人工决定是否 --min-sweep 强制归集")
                    db.execute(
                        "UPDATE orders SET swept=1, sweep_tx='REVIEW', sweep_at=? WHERE task_id=?",
                        (_now_str(), order["task_id"]),
                    )
                    db.commit()
                continue
            # 2. TRX 差额充值(幂等 + 恢复: 已有 fund_tx 但余额仍不足 → 链上查无此 tx 则清标记重充)
            trx = get_trx_balance(client, from_addr)
            if trx is None:
                log(f"SKIP {tid}… TRX 余额查询失败, 保留待重试")
                continue
            deficit = TRX_FUND_SUN - trx
            if order["fund_tx"] and deficit > TRX_FUND_MIN_DEFICIT:
                # 有充值记录但余额仍不足: 上次充值可能未上链(广播前崩溃/交易过期)
                fexists = tx_exists(client, order["fund_tx"])
                if fexists is False:
                    log(f"FUND-RECOVER {tid}… 充值 tx={order['fund_tx'][:12]}… 链上查无此交易, 清 fund_tx 重新充")
                    db.execute("UPDATE orders SET fund_tx=NULL WHERE task_id=?", (order["task_id"],))
                    db.commit()
                    # 本轮跳过: order 是本轮读取的旧 Row, fund_tx 仍非空 → need_fund 仍 False,
                    # 若继续会用不足的 TRX 构建失败归集交易。下轮重新读 order(fund_tx=NULL) 再充值。
                    continue
                else:
                    # 已上链但余额仍不足: 区分"已确认执行失败"(TRX 转账 REVERT) vs "存在但到账延迟/未决"
                    ok_f, rcpt_f = wait_for_result(client, order["fund_tx"], timeout=15)
                    if rcpt_f is not None and not ok_f:
                        # 充值链上执行失败(REVERT): 清 fund_tx 重试(与 SWEEP-FAILED 同构, 防永久 FUND-HOLD)
                        log(f"FUND-FAILED {tid}… 充值 tx={order['fund_tx'][:12]}… 链上执行失败(result={rcpt_f.get('result')}), 清 fund_tx 重试")
                        db.execute(
                            "UPDATE orders SET fund_tx=NULL, "
                            "payment_exception = COALESCE(payment_exception || char(10), '') || ? WHERE task_id=?",
                            (f"FUND-FAILED: result={rcpt_f.get('result')} at={_now_str()}", order["task_id"]))
                        db.commit()
                        continue  # 下轮重读 order 再充
                    log(f"FUND-HOLD {tid}… 充值 tx={order['fund_tx'][:12]}… 状态={fexists}, 保留待复核")
                    continue
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
                db.execute("UPDATE orders SET sweep_tx=?, sweep_at=? WHERE task_id=?",
                           (txid, _now_str(), order["task_id"]))
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

        # 再入账复查(主循环之后): 让真金白银的待归集订单优先消耗 API 配额。
        # 最近归集成功的地址, 检查是否重复付款/延迟到账(第二笔), 有则重新打开归集
        for o in recent_swept_orders(db):
            addr = o["receive_address"]
            bal = get_usdt_balance(client, addr)
            if bal is None:
                log(f"RE-ENTRY-RECHECK-FAIL {o['task_id'][:12]}… 余额查询失败(429/超时), 下轮重试")
                continue
            # 只有超过尘埃阈值的余额才重开归集(尘埃不再重开, 避免"DUST→复查→又DUST"的每轮循环)
            if bal > DUST_RAW:
                if o["tx_hash"]:
                    # 已认领订单: 重开归集(pending_orders 下轮会扫到)
                    log(f"RE-ENTRY-ALERT: {o['task_id'][:12]}… addr={addr} 已归集后再入账 {bal/1e6:.6f}, 重新打开归集")
                    # 一并清 fund_tx: 否则历史成功充值会阻塞新一轮充值(FUND-HOLD 永久卡死)
                    db.execute("UPDATE orders SET swept=0, sweep_tx=NULL, sweep_at=NULL, fund_tx=NULL WHERE task_id=?",
                               (o["task_id"],))
                    db.commit()
                else:
                    # 未认领孤儿(tx_hash IS NULL): 撤销 ORPHAN_RESOLVED 结案, 重新进入孤儿扫描(不伪造 tx_hash)
                    log(f"ORPHAN-RE-ENTRY: {o['task_id'][:12]}… 已结案孤儿再入账 {bal/1e6:.6f}, 撤销结案重新进入孤儿扫描")
                    db.execute(
                        "UPDATE orders SET swept=0, sweep_tx=NULL, sweep_at=NULL, "
                        "payment_exception = 'ORPHAN-RE-ENTRY: ' || COALESCE(payment_exception,'') WHERE task_id=?",
                        (o["task_id"],))
                    db.commit()


if __name__ == "__main__":
    main()

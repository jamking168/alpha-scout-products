#!/usr/bin/env python3
"""DeFi 异常监控：TVL 单日骤降 + 新 hack 事件检测，告警推 Telegram @news94168_bot。

数据源：DeFiLlama 免费 API（api.llama.fi），无需注册/付费。
规则引擎（MVP 两条）：
  1. TVL 骤降：大协议（>= $10M）单日 change_1d <= -15%
  2. 新 hack 事件：最近 7 天内新增的 hack（去重，首次跑只初始化不告警）
后续扩展：预言机喂价偏离、巨鲸大额转出（需链上 RPC）。

状态：SQLite 存已见过 hack + 上次快照，避免重复告警。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from bisect import bisect_right

SCRIPTS_DIR = os.path.expanduser("~/.hermes/profiles/alpha-scout/scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
from intel_write import write_intel  # noqa: E402

BASE = "https://api.llama.fi"
UA = {"User-Agent": "curl/8.5.0"}
STATE_DB = os.path.expanduser("~/projects/defi-monitor/state/defi_monitor.db")
SELF_DELIVER = os.path.expanduser("~/.hermes/scripts/self_deliver.py")

# 阈值（可调）
TVL_MIN = 10_000_000.0      # $10M，过滤小协议噪声
CHANGE_1D_THRESHOLD = -15.0  # 单日 -15%
DETERIORATE_STEP = 10.0      # 同一事件跌幅再恶化 >=10 个百分点才二次告警
HACK_WINDOW = 7 * 86400      # 7 天窗口


def _get(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def init_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(STATE_DB), exist_ok=True)
    c = sqlite3.connect(STATE_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS seen_hacks(
        defillama_id TEXT PRIMARY KEY, date INT, name TEXT, amount REAL)""")
    # TVL 骤降是持续性状态(跌幅挂好几天), 必须按"事件"去重, 否则每小时告警同一件事。
    c.execute("""CREATE TABLE IF NOT EXISTS tvl_events(
        slug TEXT PRIMARY KEY, name TEXT, last_alert_pct REAL,
        last_alert_ts INT, last_tvl REAL)""")
    c.commit()
    return c


def filter_tvl_events(alerts, c, now=None):
    """事件驱动去重: 同一协议只在「首次跌破阈值」或「跌幅再恶化 >= DETERIORATE_STEP 个百分点」时告警。
    恢复到阈值以上(不再出现在 alerts 里)即视为事件结束, 清掉记录, 下次跌破重新算新事件。"""
    now = now or int(time.time())
    fresh = []
    for a in alerts:
        slug = a.get("slug") or a.get("name")
        row = c.execute("SELECT last_alert_pct FROM tvl_events WHERE slug=?", (slug,)).fetchone()
        if row is None:
            fresh.append(a)
        elif a["change_1d"] <= row[0] - DETERIORATE_STEP:
            fresh.append(a)
    # 恢复判定: 记录里有、本次没触发的 = 跌幅已回到阈值以上 → 清记录
    triggered = {(a.get("slug") or a.get("name")) for a in alerts}
    for (slug,) in c.execute("SELECT slug FROM tvl_events").fetchall():
        if slug not in triggered:
            c.execute("DELETE FROM tvl_events WHERE slug=?", (slug,))
    c.commit()
    return fresh


def mark_tvl_events(alerts, c, now=None):
    now = now or int(time.time())
    for a in alerts:
        slug = a.get("slug") or a.get("name")
        c.execute("INSERT OR REPLACE INTO tvl_events VALUES (?,?,?,?,?)",
                  (slug, a.get("name"), a["change_1d"], now, a["tvl"]))
    c.commit()
    return len(alerts)


def detect_tvl_drop(protocols):
    alerts = []
    for p in protocols:
        tvl = p.get("tvl") or 0
        chg = p.get("change_1d")
        if chg is None:
            continue
        cat = (p.get("category") or "").lower()
        if "cex" in cat or "chain" in cat:
            continue  # 排除 CEX 和公链
        if tvl >= TVL_MIN and chg <= CHANGE_1D_THRESHOLD:
            chains = p.get("chains") or ["?"]
            alerts.append({
                "name": p.get("name"),
                "slug": p.get("slug"),
                "tvl": tvl,
                "change_1d": chg,
                "chain": chains[0] if chains else "?",
            })
    alerts.sort(key=lambda x: x["change_1d"])
    return alerts


def detect_new_hacks(hacks, c):
    now = int(time.time())
    cutoff = now - HACK_WINDOW
    alerts = []
    for h in hacks:
        d = h.get("date") or 0
        if d < cutoff:
            continue
        hid = str(h.get("defillamaId") or h.get("name"))
        if c.execute("SELECT 1 FROM seen_hacks WHERE defillama_id=?", (hid,)).fetchone():
            continue
        chains = h.get("chain") or ["?"]
        alerts.append({
            "name": h.get("name"),
            "amount": h.get("amount"),
            "date": d,
            "technique": h.get("technique"),
            "chain": chains[0] if chains else "?",
            "classification": h.get("classification"),
        })
    alerts.sort(key=lambda x: x["date"], reverse=True)
    return alerts


def mark_seen_hacks(hacks, c):
    now = int(time.time())
    cutoff = now - HACK_WINDOW
    n = 0
    for h in hacks:
        d = h.get("date") or 0
        if d < cutoff:
            continue
        hid = str(h.get("defillamaId") or h.get("name"))
        try:
            c.execute(
                "INSERT OR IGNORE INTO seen_hacks VALUES (?,?,?,?)",
                (hid, d, h.get("name"), h.get("amount")),
            )
            n += 1
        except Exception:
            pass
    c.commit()
    return n


def send_alert(text: str):
    tmp = "/tmp/defi_alert.md"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    r = subprocess.run(
        ["python3", SELF_DELIVER, "--profile", "alpha-scout", "--text-file", tmp],
        capture_output=True, text=True, timeout=90,
    )
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


# ---- RWA 板块资金流快照（原 rwa-monitor 逻辑，已合并进 --rwa 模式）----
RWA_WATCHLIST = [
    "ondo-global-markets", "ondo-yield-assets", "centrifuge-protocol", "tinlake",
    "maple-rwa", "mountain-protocol", "blackrock-buidl", "tether-gold",
    "invesco-ustb", "ethena-usdtb", "huma-finance-v2", "anemoy-capital",
    "plume-vaults", "digift", "realt-tokens",
]
RWA_SNAPSHOT_DIR = os.path.expanduser("~/projects/defi-monitor/state/snapshots")


def _nearest_ts_value(tvl_points, target_ts):
    """升序 tvl_points 中找 date <= target_ts 的最后一个点的 totalLiquidityUSD。"""
    if not tvl_points:
        return None
    seen = {}
    for p in tvl_points:
        seen[p["date"]] = p
    pts = sorted(seen.values(), key=lambda p: p["date"])
    dates = [p["date"] for p in pts]
    idx = bisect_right(dates, target_ts) - 1
    if idx < 0:
        return None
    return pts[idx]["totalLiquidityUSD"]


def run_rwa_mode(top=40, windows=(7, 30)) -> int:
    """RWA 板块资金流快照：筛 RWA 协议 → 拉历史 TVL → 算 7/30 天变化 → 摘要 + 写情报库。"""
    now = int(time.time())
    protocols = _get(f"{BASE}/protocols")
    if not protocols:
        print("[fatal] 无法获取 /protocols", file=sys.stderr)
        return 1

    rwa = [p for p in protocols if (p.get("category") or "") == "RWA"]
    tracked = [p for p in rwa if p.get("tvl") is not None]
    tracked.sort(key=lambda x: -(x["tvl"] or 0))
    untracked = [p for p in rwa if p.get("tvl") is None]
    total_tvl = sum(p["tvl"] for p in tracked)

    targets = {}
    for p in tracked[:top]:
        targets[p["slug"]] = p
    for slug in RWA_WATCHLIST:
        if slug not in targets:
            info = next((q for q in rwa if q["slug"] == slug), None)
            targets[slug] = info or {"slug": slug, "name": slug, "chain": None, "tvl": None}

    details = []
    for slug, meta in targets.items():
        try:
            hist = _get(f"{BASE}/protocol/{slug}")
        except Exception:
            hist = None
        if not hist:
            details.append({"slug": slug, "name": meta.get("name"),
                            "tvl_now": meta.get("tvl"), "error": "fetch_failed", "changes": {}})
            continue
        tvl_points = hist.get("tvl") or []
        latest = tvl_points[-1]["totalLiquidityUSD"] if tvl_points else None
        changes = {}
        for w in windows:
            prev = _nearest_ts_value(tvl_points, now - w * 86400)
            changes[f"{w}d_abs"] = round(latest - prev, 2) if (latest is not None and prev is not None) else None
            changes[f"{w}d_pct"] = round((latest - prev) / prev * 100.0, 2) if (latest is not None and prev not in (None, 0)) else None
        details.append({"slug": slug, "name": meta.get("name") or hist.get("name"),
                        "chain": meta.get("chain") or hist.get("chain"),
                        "tvl_now": latest if latest is not None else meta.get("tvl"), "changes": changes})
        time.sleep(0.15)

    total_defi = None
    try:
        charts = _get(f"{BASE}/charts")
        if charts:
            total_defi = charts[-1]["totalLiquidityUSD"]
    except (IndexError, KeyError, TypeError, Exception):
        pass

    flow = {}
    for w in windows:
        vals = [d["changes"].get(f"{w}d_abs") for d in details if d["changes"].get(f"{w}d_abs") is not None]
        flow[f"{w}d_tvl_change_usd"] = round(sum(vals), 2) if vals else None
        flow[f"{w}d_protocols_count"] = len(vals)

    covered_tvl = sum(d["tvl_now"] for d in details if d["tvl_now"] is not None and not d.get("error"))
    coverage = round(covered_tvl / total_tvl * 100, 1) if total_tvl else 0.0

    os.makedirs(RWA_SNAPSHOT_DIR, exist_ok=True)
    snapshot = {
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "source": "DeFiLlama free API (api.llama.fi)",
        "rwa": {
            "protocol_count": len(rwa), "tracked_count": len(tracked),
            "untracked_count": len(untracked), "total_tvl_usd": round(total_tvl, 2),
            "total_defi_tvl_usd": round(total_defi, 2) if total_defi else None,
            "rwa_share_of_defi_pct": round(total_tvl / total_defi * 100, 2) if total_defi else None,
            "coverage_pct": coverage, "flow": flow,
        },
        "protocols": details,
    }
    fname = time.strftime("rwa_snapshot_%Y%m%d_%H%M%S.json", time.gmtime(now))
    tmp = os.path.join(RWA_SNAPSHOT_DIR, fname + ".tmp")
    out = os.path.join(RWA_SNAPSHOT_DIR, fname)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out)

    def _rank(w):
        rows = [d for d in details if d["changes"].get(f"{w}d_abs") is not None]
        return sorted(rows, key=lambda x: -x["changes"][f"{w}d_abs"])

    lines = ["📊 RWA 板块 TVL 快照（DeFiLlama 一手数据）", ""]
    lines.append(f"RWA 协议 {len(rwa)} 个（有 TVL {len(tracked)} / 未跟踪 {len(untracked)}）")
    lines.append(f"RWA 总量 TVL ${total_tvl:,.0f}")
    if total_defi:
        lines.append(f"全 DeFi ${total_defi:,.0f}，RWA 占比 {total_tvl/total_defi*100:.2f}%")
    for w in windows:
        net = flow.get(f"{w}d_tvl_change_usd")
        if net is not None:
            direction = "↑" if net > 0 else "↓"
            lines.append(f"{w}天 TVL 变化 {direction} ${net:+,.0f}（覆盖 {flow[f'{w}d_protocols_count']} 协议 / {coverage:.1f}%）")
        top3 = _rank(w)[:3]
        if top3:
            lines.append(f"{w}天流入前三: " + "；".join(f"{d['name']} ${d['changes'][f'{w}d_abs']:+,.0f}" for d in top3))
    lines.append("")
    lines.append("⚠️ TVL 变化仅反映锁仓价值变动，不构成资金流结论；异动需第二源核验。")
    brief = "\n".join(lines)
    print(brief)

    import datetime
    today = datetime.date.today().isoformat()
    title = f"RWA 板块资金流：总量 ${total_tvl/1e9:.2f}B"
    if total_defi:
        title += f"（占 DeFi {total_tvl/total_defi*100:.2f}%）"
    write_intel(today, title, "HOLD", "RWA", "A级", brief, "defi-monitor", "defi-monitor")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DeFi 异常监控 + RWA 板块快照")
    ap.add_argument("--rwa", action="store_true", help="RWA 板块资金流快照模式")
    ap.add_argument("--top", type=int, default=40, help="RWA 历史拉取头部协议数")
    args = ap.parse_args()
    if args.rwa:
        return run_rwa_mode(top=args.top)

    c = init_db()
    protocols = _get(f"{BASE}/protocols")
    hacks = _get(f"{BASE}/hacks")

    tvl_detected = detect_tvl_drop(protocols)
    hack_alerts = detect_new_hacks(hacks, c)
    # 首次初始化：seen_hacks 为空时只初始化不告警 hack，避免历史事件轰炸
    is_first = c.execute("SELECT COUNT(*) FROM seen_hacks").fetchone()[0] == 0
    if is_first:
        hack_alerts = []

    # TVL 骤降按事件去重：首次部署(表为空)只建基线不告警，避免上线即刷一条
    tvl_first = c.execute("SELECT COUNT(*) FROM tvl_events").fetchone()[0] == 0
    if tvl_first:
        tvl_alerts = []
    else:
        tvl_alerts = filter_tvl_events(tvl_detected, c)

    def _commit_seen():
        """标记已见。只在「确认无需发送」或「发送成功」后调用——发送失败就不标记，下轮重试。"""
        mark_seen_hacks(hacks, c)
        mark_tvl_events(tvl_detected, c)

    if not tvl_alerts and not hack_alerts:
        _commit_seen()   # 静默也要落基线，否则下轮又被当成首次
        print("无异常（正常巡检）")
        return 0

    lines = ["🚨 DeFi 异常监控告警", ""]
    if hack_alerts:
        lines.append(f"**新 hack 事件 · {len(hack_alerts)} 起**")
        for h in hack_alerts:
            amt = f"${h['amount']:,.0f}" if h["amount"] else "金额未知"
            lines.append(f"• {h['name']}（{h['chain']}）— {amt} — {h['technique'] or h['classification'] or '未知手法'}")
        lines.append("")
    if tvl_alerts:
        lines.append(f"**TVL 单日骤降 · {len(tvl_alerts)} 个协议**")
        for a in tvl_alerts[:10]:
            lines.append(f"• {a['name']}（{a['chain']}）— TVL ${a['tvl']/1e6:.1f}M — 24h {a['change_1d']:.1f}%")
        lines.append("")
    lines.append("数据源：DeFiLlama 免费 API · 自动巡检 · 不构成投资建议")

    code, out, err = send_alert("\n".join(lines))
    if code == 0:
        _commit_seen()   # 送达后才标记；失败就不标记，下轮同一事件会重试
    else:
        print("⚠️ 告警发送失败 → 本轮不标记，下轮重试")
    print(f"告警发送: code={code} {out} {err[:120]}")
    print(f"内容: {len(hack_alerts)} hack + {len(tvl_alerts)} TVL骤降")
    # 写入情报库（集成闭环：监控产出 → 情报简报内容源）
    import datetime
    today = datetime.date.today().isoformat()
    title = f"DeFi 异常监控：{len(hack_alerts)} 起新 hack + {len(tvl_alerts)} 协议 TVL 骤降"
    write_intel(today, title, "GO", "DeFi", "A级", "\n".join(lines),
                "defi-monitor", "defi-monitor")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RWA 链上资金流监控脚本 (DeFiLlama 免费 API)

用途:
  - 抓取 DeFiLlama 的 RWA 板块协议列表与当前 TVL
  - 对 TVL 排名靠前的协议逐个拉取历史 TVL,计算 7 天 / 30 天资金变化
  - 汇总 RWA 板块总量 TVL 与净流入/流出,输出结构化 JSON + 中文摘要

数据源: https://api.llama.fi/  (免费公开,无需注册)
  GET /protocols           -> 全部协议列表(含 category / 当前 tvl / slug)
  GET /protocol/{slug}     -> 单协议历史 TVL 时序 [{date, totalLiquidityUSD}, ...]
  GET /charts              -> 全 DeFi 总 TVL 时序(用于算 RWA 占比背景)

运行:
  python3 rwa_monitor.py                  # 默认: 拉 top 40 个 RWA 协议历史
  python3 rwa_monitor.py --top 50         # 自定义历史深度
  python3 rwa_monitor.py --outdir out     # 自定义快照输出目录

适合 cron 化: 无交互,幂等,失败自动重试/跳过,快照文件名带时间戳。
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from bisect import bisect_right

BASE = "https://api.llama.fi"
USER_AGENT = "rwa-monitor/1.0 (personal intel script)"
TIMEOUT = 45
RETRIES = 3

# 重点观察名单(任务点名 + 历史上 RWA 头部协议),无论 TVL 排名都会拉历史
WATCHLIST = [
    "ondo-global-markets",   # Ondo Finance 主协议(USDY/OUSG 等)
    "ondo-yield-assets",     # Ondo 收益资产(DeFiLlama 未直接跟踪)
    "centrifuge-protocol",   # Centrifuge(旧入口,可能未跟踪)
    "tinlake",               # Centrifuge Tinlake(旧借贷入口)
    "maple-rwa",             # Maple(已基本清盘)
    "mountain-protocol",     # Mountain Protocol(USDm)
    "blackrock-buidl",       # BlackRock BUIDL(DeFiLlama 未直接跟踪)
    "tether-gold",           # Tether Gold XAUt
    "invesco-ustb",          # Invesco USTB
    "ethena-usdtb",          # Ethena USDtb
    "huma-finance-v2",
    "anemoy-capital",        # Centrifuge/Janus Henderson 关联
    "plume-vaults",
    "digift",
    "realt-tokens",
]


def _get(url, retries=RETRIES):
    """GET 并返回解析后的 JSON;失败重试,最终失败返回 None。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    print(f"[warn] 拉取失败 {url}: {last_err}", file=sys.stderr)
    return None


def _nearest_ts_value(tvl_points, target_ts):
    """在升序 tvl_points 中找 date <= target_ts 的最后一个点的 totalLiquidityUSD。
    若最早点都比 target 晚(协议刚上线),返回 None 表示无更早基线。
    内部先按 date 排序去重,避免乱序/重复数据误导。"""
    if not tvl_points:
        return None
    # 排序去重(按 date 升序,同 date 保留最后一个)
    seen = {}
    for p in tvl_points:
        seen[p["date"]] = p
    pts = sorted(seen.values(), key=lambda p: p["date"])
    dates = [p["date"] for p in pts]
    idx = bisect_right(dates, target_ts) - 1
    if idx < 0:
        return None
    return pts[idx]["totalLiquidityUSD"]


def _pct(cur, prev):
    if prev is None or prev == 0:
        return None
    return round((cur - prev) / prev * 100.0, 2)


def fetch_protocols():
    data = _get(f"{BASE}/protocols")
    if data is None:
        return None
    rwa = []
    for p in data:
        if p.get("category") == "RWA":
            rwa.append({
                "slug": p.get("slug"),
                "name": p.get("name"),
                "chain": p.get("chain"),
                "tvl": p.get("tvl"),           # 当前 TVL,可能为 None(未跟踪)
                "url": p.get("url"),
            })
    return rwa


def fetch_total_defi_tvl():
    data = _get(f"{BASE}/charts")
    if not data:
        return None
    # charts 返回 [{date, totalLiquidityUSD}],取最后一点
    try:
        return data[-1]["totalLiquidityUSD"]
    except (IndexError, KeyError, TypeError):
        return None


def main():
    ap = argparse.ArgumentParser(description="RWA 链上资金流监控")
    ap.add_argument("--top", type=int, default=40,
                    help="拉取历史 TVL 的头部 RWA 协议数量(默认 40)")
    ap.add_argument("--outdir", type=str, default="snapshots",
                    help="快照输出目录(默认 snapshots)")
    ap.add_argument("--window", type=int, nargs="*", default=[7, 30],
                    help="变化窗口天数(默认 7 30)")
    args = ap.parse_args()

    now = int(time.time())
    os.makedirs(args.outdir, exist_ok=True)

    print("[1/4] 拉取 DeFiLlama 协议列表...")
    rwa = fetch_protocols()
    if rwa is None:
        print("[fatal] 无法获取 /protocols", file=sys.stderr)
        sys.exit(1)

    # 当前总量 TVL(排除 None)
    total_tvl = sum(p["tvl"] for p in rwa if p["tvl"] is not None)
    tracked = [p for p in rwa if p["tvl"] is not None]
    untracked = [p for p in rwa if p["tvl"] is None]
    tracked.sort(key=lambda x: -(x["tvl"] or 0))

    # 历史拉取范围: top N + 观察名单
    targets = {}
    for p in tracked[: args.top]:
        targets[p["slug"]] = p
    for slug in WATCHLIST:
        if slug not in targets:
            # 从 rwa 全量里找元信息;找不到就构造一个占位
            info = next((q for q in rwa if q["slug"] == slug), None)
            targets[slug] = info or {"slug": slug, "name": slug, "chain": None, "tvl": None}

    print(f"[2/4] RWA 协议 {len(rwa)} 个(有 TVL {len(tracked)} / 未跟踪 {len(untracked)})")
    print(f"[2/4] 当前 RWA 总量 TVL = ${total_tvl:,.0f}")
    print(f"[3/4] 拉取 {len(targets)} 个协议的历史 TVL(窗口 {args.window} 天)...")

    details = []
    fetched_ok = 0
    for i, (slug, meta) in enumerate(targets.items(), 1):
        hist = _get(f"{BASE}/protocol/{slug}")
        if hist is None:
            details.append({
                "slug": slug, "name": meta.get("name"),
                "chain": meta.get("chain"),
                "tvl_now": meta.get("tvl"),
                "error": "fetch_failed",
                "changes": {},
            })
            continue
        tvl_points = hist.get("tvl") or []
        latest = tvl_points[-1]["totalLiquidityUSD"] if tvl_points else None
        changes = {}
        for w in args.window:
            prev = _nearest_ts_value(tvl_points, now - w * 86400)
            changes[f"{w}d_abs"] = round(latest - prev, 2) if (latest is not None and prev is not None) else None
            changes[f"{w}d_pct"] = _pct(latest, prev)
        details.append({
            "slug": slug,
            "name": meta.get("name") or hist.get("name"),
            "chain": meta.get("chain") or hist.get("chain"),
            "tvl_now": latest if latest is not None else meta.get("tvl"),
            "tvl_list_now": meta.get("tvl"),   # /protocols 列表里的值,便于对照
            "changes": changes,
        })
        fetched_ok += 1
        time.sleep(0.15)  # 温和限速
        if i % 10 == 0:
            print(f"    ... {i}/{len(targets)}")

    # 板块 30d/7d TVL 变化: 用有历史数据的头部协议求和(近似全板块)
    # ⚠️ TVL 变化 ≠ 真实资金流: 可能来自资产价格/汇率、利息累计、抵押结构、
    #    协议迁移/重分类等,不可直接解读为"申购/赎回净额"。
    flow = {}
    for w in args.window:
        vals = [d["changes"].get(f"{w}d_abs") for d in details if d["changes"].get(f"{w}d_abs") is not None]
        flow[f"{w}d_tvl_change_usd"] = round(sum(vals), 2) if vals else None
        flow[f"{w}d_protocols_count"] = len(vals)

    # 覆盖度: 有有效历史数据的协议当前 TVL 之和 / 板块总量
    # (排除历史抓取失败的协议,避免虚高覆盖率)
    covered_tvl = sum(d["tvl_now"] for d in details
                      if d["tvl_now"] is not None and not d.get("error"))
    coverage = round(covered_tvl / total_tvl * 100, 1) if total_tvl else 0.0

    # 涨跌榜
    def _rank(w):
        rows = [d for d in details if d["changes"].get(f"{w}d_abs") is not None]
        return sorted(rows, key=lambda x: -x["changes"][f"{w}d_abs"])

    total_defi = fetch_total_defi_tvl()

    snapshot = {
        "generated_at_utc": now,
        "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "source": "DeFiLlama free API (api.llama.fi)",
        "rwa": {
            "protocol_count": len(rwa),
            "tracked_count": len(tracked),
            "untracked_count": len(untracked),
            "total_tvl_usd": round(total_tvl, 2),
            "total_defi_tvl_usd": round(total_defi, 2) if total_defi else None,
            "rwa_share_of_defi_pct": round(total_tvl / total_defi * 100, 2) if total_defi else None,
            "coverage_of_tvl_change_estimate_pct": coverage,
            "flow": flow,
        },
        "windows_days": args.window,
        "protocols": details,
        "untracked_notable": [
            {"slug": p["slug"], "name": p["name"]} for p in untracked
            if p["slug"] in WATCHLIST
        ],
    }

    fname = time.strftime("snapshot_%Y%m%d_%H%M%S.json", time.gmtime(now))
    out_path = os.path.join(args.outdir, fname)
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
    print(f"[4/4] 快照已写入 {out_path}")

    # ---- 中文摘要 ----
    print("\n" + "=" * 62)
    print("RWA 板块 TVL 变化快照(DeFiLlama 一手数据)")
    print("=" * 62)
    print(f"生成时间(UTC) : {snapshot['generated_at_iso']}")
    print(f"RWA 协议总数   : {len(rwa)} (有 TVL {len(tracked)},未跟踪 {len(untracked)})")
    print(f"RWA 总量 TVL   : ${total_tvl:,.0f} ({total_tvl/1e9:.3f}B)")
    if total_defi:
        print(f"全 DeFi 总 TVL : ${total_defi:,.0f} ({total_defi/1e9:.3f}B), RWA 占比 {total_tvl/total_defi*100:.2f}%")
    for w in args.window:
        net = flow.get(f"{w}d_tvl_change_usd")
        if net is not None:
            direction = "增加" if net > 0 else "减少"
            print(f"{w:2d}天 TVL 变化: {direction} ${net:+,.0f} ({net/1e6:+.1f}M), 覆盖 {flow[f'{w}d_protocols_count']} 个头部协议 / 覆盖率 {coverage:.1f}%")
    print("-" * 62)
    for w in args.window:
        top3 = _rank(w)[:3]
        bot3 = _rank(w)[-3:]
        print(f"{w}天 TVL 增加前三: " + " ; ".join(
            f"{d['name']} ${d['changes'][f'{w}d_abs']:+,.0f}" for d in top3))
        print(f"{w}天 TVL 减少前三: " + " ; ".join(
            f"{d['name']} ${d['changes'][f'{w}d_abs']:+,.0f}" for d in bot3))
    print("=" * 62)
    print("⚠️ TVL 变化仅反映锁仓价值变动,不构成资金流入/流出结论;异动需第二源核验。")
    return snapshot


if __name__ == "__main__":
    main()

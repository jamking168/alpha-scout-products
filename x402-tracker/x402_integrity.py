#!/usr/bin/env python3
"""x402_integrity.py — 用 top_buyer_share 对 x402 目录收入做「刷量调整」分析。

动机：x402-list.com 给每个 listing 提供 30 天成交额、笔数、独立买家数、以及
top_buyer_share_30d（单一买家占该服务成交额比例）。头部服务的成交额很大，但如果
top_buyer_share 接近 1，那笔钱几乎来自一个买家——可能是自买自卖/合成流量，
不能当作「外部需求」证据。本脚本把链上口径再切一刀，给出三档口径：

  gross        原始口径（等于现有 tracker 的口径）
  diversified  只算 top_buyer_share < 0.5 的服务（多元买家，可信度最高）
  single_buyer top_buyer_share >= 0.8 的服务成交额单列（高度疑似自成交/单一对手）

用法：python3 x402_integrity.py [--snapshot PATH] [--json]
"""
from __future__ import annotations
import argparse, json, os, glob
from datetime import datetime, timezone

DEFAULT_DIR = os.path.expanduser("~/projects/alpha-scout-products/x402-tracker/data")

BUCKETS = [(0.0, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 0.999), (0.999, 1.01)]


def load(snap: str | None) -> dict:
    if snap:
        return json.load(open(snap))
    files = sorted(glob.glob(os.path.join(DEFAULT_DIR, "snapshots", "*.json")))
    if not files:
        raise SystemExit("no snapshot found")
    return json.load(open(files[-1]))


def trac(r):
    return ((r.get("assessment") or {}).get("traction") or {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    snap = load(a.snapshot)
    recs = snap["services"]

    rows = []
    for r in recs:
        t = trac(r)
        if t.get("status") != "measured":
            continue
        v = t.get("volume_usd_30d") or 0.0
        if v <= 0:
            continue
        rows.append({
            "name": r.get("name"), "slug": r.get("slug"), "cat": r.get("category"),
            "vol": v, "tx": t.get("tx_count_30d") or 0,
            "buyers": t.get("unique_buyers_30d") or 0,
            "top_share": t.get("top_buyer_share_30d"),
            "last": t.get("last_settlement_at"),
        })

    priced = [x for x in rows if isinstance(x["top_share"], (int, float))]
    unpriced = [x for x in rows if not isinstance(x["top_share"], (int, float))]

    gross = sum(x["vol"] for x in rows)
    div = sum(x["vol"] for x in priced if x["top_share"] < 0.5)
    mid = sum(x["vol"] for x in priced if 0.5 <= x["top_share"] < 0.8)
    single = sum(x["vol"] for x in priced if x["top_share"] >= 0.8)
    unk = sum(x["vol"] for x in unpriced)

    out = {
        "captured_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot_captured": snap.get("summary", {}).get("captured_at_utc"),
        "source": "x402-list.com (CC BY 4.0)",
        "paying_services": len(rows),
        "gross_usd_30d": round(gross, 2),
        "diversified_usd_30d": round(div, 2),
        "diversified_pct": round(100.0 * div / gross, 1) if gross else None,
        "mid_usd_30d": round(mid, 2),
        "single_buyer_usd_30d": round(single, 2),
        "single_buyer_pct": round(100.0 * single / gross, 1) if gross else None,
        "no_share_reported_usd_30d": round(unk, 2),
        "buckets": [],
        "top_single_buyer": [],
        "top_diversified": [],
    }
    for lo, hi in BUCKETS:
        sel = [x for x in priced if lo <= x["top_share"] < hi]
        out["buckets"].append({
            "range": f"{lo:.2f}-{hi:.2f}", "n": len(sel),
            "vol": round(sum(x["vol"] for x in sel), 2),
        })
    out["top_single_buyer"] = sorted(
        [x for x in priced if x["top_share"] >= 0.8], key=lambda x: -x["vol"])[:8]
    out["top_diversified"] = sorted(
        [x for x in priced if x["top_share"] < 0.5], key=lambda x: -x["vol"])[:10]

    for x in out["top_single_buyer"] + out["top_diversified"]:
        x["vol"] = round(x["vol"], 2)
        x["top_share"] = round(x["top_share"], 3)

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    L = []
    L.append(f"# x402 收入「刷量调整」口径 · {out['captured_at_utc']}")
    L.append(f"_基于快照 {out['snapshot_captured']}，来源 {out['source']}_")
    L.append("")
    L.append(f"- 付费服务数: **{out['paying_services']}**")
    L.append(f"- 毛口径 30 天成交: **${out['gross_usd_30d']:,.0f}**")
    L.append(f"- 其中【买家分散 top_buyer_share<0.5】: **${out['diversified_usd_30d']:,.0f}**"
             f"（占 {out['diversified_pct']}%）")
    L.append(f"- 其中【疑似单一买家 >=0.8】: **${out['single_buyer_usd_30d']:,.0f}**"
             f"（占 {out['single_buyer_pct']}%，不计入可信需求）")
    L.append(f"- 中间档 0.5-0.8: ${out['mid_usd_30d']:,.0f}")
    L.append(f"- 未报告 share: ${out['no_share_reported_usd_30d']:,.0f}")
    L.append("")
    L.append("## 按 top_buyer_share 分档")
    L.append("| 区间 | 服务数 | 30天成交额 |")
    L.append("|---|---|---|")
    for b in out["buckets"]:
        L.append(f"| {b['range']} | {b['n']} | ${b['vol']:,.0f} |")
    L.append("")
    L.append("## 单一买家最集中的（成交额大但对手方单一）")
    for x in out["top_single_buyer"]:
        L.append(f"- {x['name']} — ${x['vol']:,.0f} / {x['tx']} 笔 / {x['buyers']} 买家 /"
                 f" top1 占 {x['top_share']*100:.1f}%  [{x['cat']}]")
    L.append("")
    L.append("## 买家最分散的（更可能是真实外部需求）")
    for x in out["top_diversified"]:
        L.append(f"- {x['name']} — ${x['vol']:,.0f} / {x['tx']} 笔 / {x['buyers']} 买家 /"
                 f" top1 占 {x['top_share']*100:.1f}%  [{x['cat']}]")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

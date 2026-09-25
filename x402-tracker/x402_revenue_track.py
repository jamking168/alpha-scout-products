#!/usr/bin/env python3
"""x402_revenue_track.py — 每日抓取 x402-list.com 的链上收入口径，产出市场真实收入快照。

数据源：https://x402-list.com/api/v1/services （公开、无需鉴权，CC-BY-4.0，署名 "Data: x402-list.com (CC BY 4.0)"）
口径：每个 listing 的 traction.volume_usd_30d 是「该目录观测到的 facilitator 上已结算 USDC」，
     是下限（floor）而非估计；status != measured 的 listing 记为「未测量」，不计入 0。

产出：
  <out>/snapshots/<UTC日期>.json  当日快照（全量）
  <out>/series.jsonl              时间序列（每日一行汇总）
  <out>/latest.md                 人类可读摘要

用法：python3 x402_revenue_track.py [--out DIR] [--pages 9]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.request
from datetime import datetime, timezone

API = "https://x402-list.com/api/v1/services"
UA = "alpha-scout-x402-tracker/1.0 (research; Data: x402-list.com CC BY 4.0)"


def get_json(url: str, tries: int = 3) -> dict:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # pragma: no cover
            last = e
            time.sleep(2 + 3 * i)
    raise RuntimeError(f"fetch failed: {url}: {last}")


def fetch_all(pages: int) -> tuple[list[dict], dict]:
    recs: list[dict] = []
    meta = {}
    for p in range(1, pages + 1):
        d = get_json(f"{API}?per_page=100&page={p}")
        meta = d.get("meta", meta)
        data = d.get("data") or []
        recs.extend(data)
        if len(data) < 100:
            break
        time.sleep(1.2)
    return recs, meta


def num(x, default=None):
    return default if x is None else x


def summarize(recs: list[dict]) -> dict:
    tot_listed = len(recs)
    measured, unmeasured, paying = [], [], []
    for r in recs:
        t = ((r.get("assessment") or {}).get("traction") or {})
        st = t.get("status")
        if st == "measured":
            measured.append(r)
            v = t.get("volume_usd_30d")
            if v is not None and v > 0:
                paying.append(r)
        else:
            unmeasured.append(r)

    def vol(r):
        return ((r.get("assessment") or {}).get("traction") or {}).get("volume_usd_30d") or 0.0

    def tx(r):
        return ((r.get("assessment") or {}).get("traction") or {}).get("tx_count_30d") or 0

    paying.sort(key=vol, reverse=True)
    total_vol = sum(vol(r) for r in paying)
    total_tx = sum(tx(r) for r in paying)
    prices: list[float] = [float(r["min_price_usd"]) for r in recs
                           if isinstance(r.get("min_price_usd"), (int, float))]

    def share(k):
        if not total_vol:
            return None
        return round(100.0 * sum(vol(r) for r in paying[:k]) / total_vol, 1)

    top = []
    for r in paying[:10]:
        t = r["assessment"]["traction"]
        top.append({
            "name": r.get("name"), "slug": r.get("slug"), "vol_30d_usd": round(vol(r), 4),
            "tx_30d": tx(r), "buyers_30d": t.get("unique_buyers_30d"),
            "last_settlement_at": t.get("last_settlement_at"),
            "top_buyer_share": round(t.get("top_buyer_share_30d") or 0, 3),
            "min_price_usd": r.get("min_price_usd"), "category": r.get("category"),
        })

    return {
        "captured_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "directory": "x402-list.com (CC BY 4.0, floor metric)",
        "listed_total": tot_listed,
        "traction_measured": len(measured),
        "traction_unmeasured": len(unmeasured),
        "services_paid_30d": len(paying),
        "pct_paid_30d": round(100.0 * len(paying) / tot_listed, 1) if tot_listed else None,
        "market_volume_usd_30d": round(total_vol, 2),
        "market_volume_usd_per_day": round(total_vol / 30.0, 2),
        "market_tx_30d": total_tx,
        "avg_settlement_usd": round(total_vol / total_tx, 4) if total_tx else None,
        "top1_share_pct": share(1), "top3_share_pct": share(3), "top5_share_pct": share(5),
        "min_price_median_usd": round(statistics.median(prices), 4) if prices else None,
        "min_price_p10_usd": round(sorted(prices)[len(prices) // 10], 4) if prices else None,
        "min_price_max_usd": max(prices) if prices else None,
        "top_earners": top,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/projects/alpha-scout-products/x402-tracker/data"))
    ap.add_argument("--pages", type=int, default=9)
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, "snapshots"), exist_ok=True)

    recs, meta = fetch_all(a.pages)
    s = summarize(recs)
    s["api_meta"] = meta
    day = s["captured_at_utc"][:10]
    with open(os.path.join(a.out, "snapshots", f"{day}.json"), "w") as f:
        json.dump({"summary": s, "services": recs}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.out, "series.jsonl"), "a") as f:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")

    lines = [f"# x402 真实收入快照 · {s['captured_at_utc']}", "",
             f"- 目录 listing 总数: **{s['listed_total']}**（有 traction 测量: {s['traction_measured']}，未测量: {s['traction_unmeasured']}）",
             f"- 30 天内真的收到过钱的服务: **{s['services_paid_30d']}**（占 {s['pct_paid_30d']}%）",
             f"- 全目录 30 天链上结算合计: **${s['market_volume_usd_30d']}**（≈ ${s['market_volume_usd_per_day']}/天）",
             f"- 30 天结算笔数: {s['market_tx_30d']}，单笔均值 ${s['avg_settlement_usd']}",
             f"- 集中度: 头部 1 家 {s['top1_share_pct']}% / 前 3 {s['top3_share_pct']}% / 前 5 {s['top5_share_pct']}%",
             f"- 报价中位数: ${s['min_price_median_usd']}（P10 ${s['min_price_p10_usd']}，最高 ${s['min_price_max_usd']}）", ""]
    for t in s["top_earners"]:
        lines.append(f"  - {t['name']} — ${t['vol_30d_usd']} / {t['tx_30d']} 笔 / {t['buyers_30d']} 买家 / 末次结算 {t['last_settlement_at']}")
    lines += ["", "> 口径: 观测到的 facilitator 上已结算 USDC，是下限不是估计；未测量的 listing 不等于零收入。数据: x402-list.com (CC BY 4.0)"]
    with open(os.path.join(a.out, "latest.md"), "w") as f:
        f.write("\n".join(lines))

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

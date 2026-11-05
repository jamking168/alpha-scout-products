#!/usr/bin/env python3
"""x402_revenue_track.py — 每日抓取 x402-list.com 的链上收入口径，产出市场真实收入快照。

数据源：https://x402-list.com/api/v1/services （公开、无需鉴权，CC-BY-4.0，署名 "Data: x402-list.com (CC BY 4.0)"）
口径：每个 listing 的 traction.volume_usd_30d 是「该目录观测到的 facilitator 上已结算 USDC」，
     是下限（floor）而非估计；status != measured 的 listing 记为「未测量」，不计入 0。

产出：
  <out>/snapshots/<UTC日期>.json  当日快照（全量）
  <out>/series.jsonl              时间序列（每日一行汇总）
  <out>/latest.md                 人类可读摘要

新鲜度护栏（2026-11-05 加）：该站结算层可能停更（实测 2026-10-28 起），此时「30 天」口径实际是
「截至末次结算日的 30 天存量」，读的人会当成「最近 30 天」→ 趋势结论无效。所以每天算
settlement_stale_days，超过 3 天在 latest.md 最顶部插警告并在 stdout 打 STALE 行。

用法：python3 x402_revenue_track.py [--out DIR] [--pages 9] [--stale-policy {warn,fail}]
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

# 结算层停更多少天开始警告。3 天是"周末空档"与"真的停了"的分界：
# 链上结算不会因为周末连续 4 天完全没有动作。
STALE_THRESHOLD_DAYS = 3


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


def _parse_iso(ts) -> datetime | None:
    """解析数据源里的 ISO 时间（`...Z` 结尾，秒级）。解析不了返回 None，绝不抛。"""
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip()
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None


def settlement_staleness(recs: list[dict], captured_at: str):
    """返回 (全目录末次结算时间 ISO 或 None, 停更天数 int 或 None)。

    天数按**日历日**差算（captured 的日期 − 末次结算的日期）：实测快照 2026-11-05T12:00:33Z 对
    末次结算 2026-10-28T12:29:21Z，时间差 7.98 天向下取整是 7，但"10-28 之后已经 8 天没有新结算"
    才是读的人需要的数字。没有 measured 记录时两个值都是 None。
    """
    cap = _parse_iso(captured_at)
    latest = None
    for r in recs:
        t = ((r.get("assessment") or {}).get("traction") or {})
        dt = _parse_iso(t.get("last_settlement_at"))
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    if latest is None or cap is None:
        return None, None
    return latest.strftime("%Y-%m-%dT%H:%M:%SZ"), (cap.date() - latest.date()).days


def stale_banner(days: int, max_last_at: str) -> str:
    return (f"> ⚠ 数据源结算层已停更 {days} 天（全目录末次结算 {max_last_at}）。"
            f"以下「30 天」口径实际截至该日，**趋势不可读**；只有集中度/结构结论仍成立。")


def read_prev_record(out: str) -> dict | None:
    """读 series.jsonl 的最后一条（本次运行**之前**的那天）。没有就返回 None。"""
    path = os.path.join(out, "series.jsonl")
    if not os.path.exists(path):
        return None
    last = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if not last:
        return None
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        return None


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


def _delta_suffix(s: dict, prev: dict | None) -> str:
    """目录抖动提示：total / measured 相对上一条的变化。首次运行（无上一条）返回空串。"""
    if not prev:
        return ""
    try:
        d_total = int(s["listed_total"]) - int(prev["listed_total"])
        d_meas = int(s["traction_measured"]) - int(prev["traction_measured"])
    except (KeyError, TypeError, ValueError):
        return ""
    return (f"（total 较上次 {d_total:+d}，measured 较上次 {d_meas:+d}；"
            f"measured 下降多半是目录换血，不等于需求下滑）")


def render_latest(s: dict, prev: dict | None, stale_days: int | None,
                  max_last_at: str | None) -> list[str]:
    """生成 latest.md 的行。原有 6 行指标的内容与顺序不变，只允许加警告段与括号说明。"""
    lines: list[str] = []
    if stale_days is not None and stale_days > STALE_THRESHOLD_DAYS and max_last_at:
        lines += [stale_banner(int(stale_days), max_last_at), ""]
    lines += [f"# x402 真实收入快照 · {s['captured_at_utc']}", "",
              (f"- 目录 listing 总数: **{s['listed_total']}**（有 traction 测量: {s['traction_measured']}，"
               f"未测量: {s['traction_unmeasured']}）" + _delta_suffix(s, prev)),
              f"- 30 天内真的收到过钱的服务: **{s['services_paid_30d']}**（占 {s['pct_paid_30d']}%）",
              f"- 全目录 30 天链上结算合计: **${s['market_volume_usd_30d']}**（≈ ${s['market_volume_usd_per_day']}/天）",
              f"- 30 天结算笔数: {s['market_tx_30d']}，单笔均值 ${s['avg_settlement_usd']}",
              f"- 集中度: 头部 1 家 {s['top1_share_pct']}% / 前 3 {s['top3_share_pct']}% / 前 5 {s['top5_share_pct']}%",
              f"- 报价中位数: ${s['min_price_median_usd']}（P10 ${s['min_price_p10_usd']}，最高 ${s['min_price_max_usd']}）", ""]
    for t in s["top_earners"]:
        lines.append(f"  - {t['name']} — ${t['vol_30d_usd']} / {t['tx_30d']} 笔 / {t['buyers_30d']} 买家 / 末次结算 {t['last_settlement_at']}")
    lines += ["", "> 口径: 观测到的 facilitator 上已结算 USDC，是下限不是估计；未测量的 listing 不等于零收入。数据: x402-list.com (CC BY 4.0)"]
    return lines


def run(recs: list[dict], meta: dict, out: str, stale_policy: str = "warn") -> int:
    """把一轮抓取结果落盘（快照 + series + latest.md）。抽成函数是为了能离线用构造数据测。"""
    os.makedirs(os.path.join(out, "snapshots"), exist_ok=True)
    s = summarize(recs)
    s["api_meta"] = meta

    # 上一条**必须在 append 之前**读，否则拿到的是本轮自己
    prev = read_prev_record(out)

    max_last_at, stale_days = settlement_staleness(recs, s["captured_at_utc"])
    s["settlement_max_last_at"] = max_last_at
    s["settlement_stale_days"] = stale_days

    day = s["captured_at_utc"][:10]
    with open(os.path.join(out, "snapshots", f"{day}.json"), "w") as f:
        json.dump({"summary": s, "services": recs}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(out, "series.jsonl"), "a") as f:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")

    stale = stale_days is not None and stale_days > STALE_THRESHOLD_DAYS
    if stale:
        # 让 cron 输出里一眼看见，不用点开 latest.md
        print(f"STALE settlement_feed: {stale_days} days (max last_settlement_at={max_last_at})")

    lines = render_latest(s, prev, stale_days, max_last_at)
    with open(os.path.join(out, "latest.md"), "w") as f:
        f.write("\n".join(lines))

    print("\n".join(lines))
    if stale and stale_policy == "fail":
        return 3
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/projects/alpha-scout-products/x402-tracker/data"))
    ap.add_argument("--pages", type=int, default=9)
    ap.add_argument("--stale-policy", choices=("warn", "fail"), default="warn",
                    help="结算层停更时的策略：warn=照常 exit 0（默认）；fail=exit 3")
    a = ap.parse_args()

    recs, meta = fetch_all(a.pages)
    return run(recs, meta, a.out, stale_policy=a.stale_policy)


if __name__ == "__main__":
    raise SystemExit(main())

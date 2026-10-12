#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CPI 发布差异表 —— 「实际 / 前值 / 环比 / 同比」自动出表。

设计口径（诚实优先）：
  * 只读公开数据（FRED 官方 API；FRED 的 CPI 序列本身来自 BLS）。
  * 不预测数字、不给方向性判断、不写"数据前布局"。
  * 共识(consensus)不是官方数据，本脚本不编造：只从外部 JSON 读入，
    没给就明确标注「未提供」。

用法：
  python3 cpi_release_watch.py                 # 出当前差异表
  python3 cpi_release_watch.py --json out.json # 同时落盘 JSON
  python3 cpi_release_watch.py --consensus consensus.json   # 带上共识值
  python3 cpi_release_watch.py --watch --max-minutes 15     # 轮询等新数据落地

consensus.json 格式（手工/外部来源填入，键可选）：
  {"headline_yoy": 3.7, "headline_mom": 0.3, "core_yoy": 3.1, "core_mom": 0.3, "source": "..."}
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
# 口径（与 BLS 发布一致）：环比用季调序列(SA)，同比用未季调序列(NSA)。
# 若同比误用 SA 序列会系统性偏掉（实测 2026-08 总体 SA 同比 3.71% vs BLS 官方 3.4%）。
SERIES = [
    {"name": "headline", "sa": "CPIAUCSL", "nsa": "CPIAUCNS", "label": "CPI 总体"},
    {"name": "core", "sa": "CPILFESL", "nsa": "CPILFENS", "label": "核心 CPI"},
]
ENV_CANDIDATES = [
    "/home/jam/hk-investment/system/.env",
    os.path.expanduser("~/.hermes/shared/accounts.env"),
]


def load_fred_key():
    key = os.environ.get("FRED_API_KEY", "").strip()
    if key:
        return key
    for path in ENV_CANDIDATES:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("FRED_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            return val
        except OSError:
            continue
    return ""


def fetch_observations(series_id, api_key, months=18, timeout=30):
    query = (
        FRED_URL
        + "?series_id=" + series_id
        + "&api_key=" + api_key
        + "&file_type=json&sort_order=desc&limit=" + str(months)
    )
    req = urllib.request.Request(query, headers={"User-Agent": "curl/8.5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    obs = []
    for row in payload.get("observations", []):
        if row.get("value") in (None, "", "."):
            continue
        obs.append((row["date"], float(row["value"])))
    obs.reverse()  # 升序
    return obs


def pct(new, old):
    if old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def shift_month(date_str, n):
    """'2026-08-01' + n 月 → 'YYYY-MM-01'（按日期对齐，不用位置偏移）。"""
    year, month = int(date_str[:4]), int(date_str[5:7])
    total = (year * 12 + (month - 1)) + n
    return "%04d-%02d-01" % (total // 12, total % 12 + 1)


def value_on(obs_list, date_str):
    for date, val in obs_list:
        if date == date_str:
            return val
    return None


def build_block(sa_obs, nsa_obs):
    """环比取季调(SA)序列、同比取未季调(NSA)序列，与 BLS 发布口径一致。

    按「日期」对齐取基期，不按列表位置 —— 序列里可能有缺失月
    （例:2025-10 因政府拨款中断缺失），位置法会把基期整体错位一个月。
    """
    if not sa_obs or not nsa_obs:
        return None
    latest_date, latest_sa = sa_obs[-1]
    latest_nsa = value_on(nsa_obs, latest_date)
    if latest_nsa is None:
        return None

    prev_month_date = shift_month(latest_date, -1)
    year_ago_date = shift_month(latest_date, -12)
    prev_sa = value_on(sa_obs, prev_month_date)
    prev_nsa = value_on(nsa_obs, prev_month_date)
    year_ago_nsa = value_on(nsa_obs, year_ago_date)

    prev2_date = shift_month(latest_date, -2)
    prev2_sa = value_on(sa_obs, prev2_date)
    prev_year_ago_nsa = value_on(nsa_obs, shift_month(prev_month_date, -12)) if prev_nsa is not None else None

    mom = pct(latest_sa, prev_sa) if prev_sa is not None else None
    yoy = pct(latest_nsa, year_ago_nsa) if year_ago_nsa is not None else None
    prev_mom = pct(prev_sa, prev2_sa) if (prev_sa is not None and prev2_sa is not None) else None
    prev_yoy = pct(prev_nsa, prev_year_ago_nsa) if (prev_nsa is not None and prev_year_ago_nsa is not None) else None

    return {
        "month": latest_date[:7],
        "prev_month": prev_month_date[:7],
        "sa_index": round(latest_sa, 4),
        "nsa_index": round(latest_nsa, 4),
        "mom": None if mom is None else round(mom, 2),
        "yoy": None if yoy is None else round(yoy, 2),
        "prev_sa_index": None if prev_sa is None else round(prev_sa, 4),
        "prev_mom": None if prev_mom is None else round(prev_mom, 2),
        "prev_yoy": None if prev_yoy is None else round(prev_yoy, 2),
        "yoy_base_missing": year_ago_nsa is None,
    }


def fmt_pct(v):
    return "未提供" if v is None else ("%+.2f%%" % v)


def render(rows, consensus, as_of):
    lines = []
    lines.append("CPI 发布差异表 · 数据源 FRED(BLS 原始发布) · 生成 %s" % as_of)
    lines.append("%-10s %-9s %10s %8s %8s %9s %9s" % (
        "项目", "月份", "未季调指数", "环比(SA)", "同比(NSA)", "共识环比", "共识同比"))
    for spec in SERIES:
        blk = rows.get(spec["name"])
        if not blk:
            lines.append("%-10s 数据不足" % spec["label"])
            continue
        cm = consensus.get(spec["name"] + "_mom") if consensus else None
        cy = consensus.get(spec["name"] + "_yoy") if consensus else None
        lines.append("%-10s %-9s %10.3f %8s %8s %9s %9s" % (
            spec["label"], blk["month"], blk["nsa_index"], fmt_pct(blk["mom"]), fmt_pct(blk["yoy"]),
            fmt_pct(cm), fmt_pct(cy)))
        lines.append("  前值(%s) 季调指数 %s  环比 %s  同比 %s" % (
            blk["prev_month"], "缺失" if blk["prev_sa_index"] is None else ("%.3f" % blk["prev_sa_index"]),
            fmt_pct(blk["prev_mom"]), fmt_pct(blk["prev_yoy"])))
        if blk.get("yoy_base_missing"):
            lines.append("  注意: 去年同期该月读数在源序列中缺失,同比为「未提供」(不按邻月替代)。")
        if cy is not None and blk["yoy"] is not None:
            lines.append("  同比 vs 共识偏离: %+.2f 个百分点" % (blk["yoy"] - cy))
        if cm is not None and blk["mom"] is not None:
            lines.append("  环比 vs 共识偏离: %+.2f 个百分点" % (blk["mom"] - cm))
    if consensus and consensus.get("source"):
        lines.append("共识来源: %s" % consensus["source"])
    else:
        lines.append("共识: 未提供(本脚本不编造共识值)")
    lines.append("口径: 环比取季调序列(CPIAUCSL/CPILFESL),同比取未季调(CPIAUCNS/CPILFENS),与 BLS 发布一致。")
    lines.append("仅陈述实际与前值,不含方向性判断与预测。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_out", default="")
    ap.add_argument("--consensus", dest="consensus", default="")
    ap.add_argument("--watch", action="store_true", help="轮询直到出现比基线更新的月份")
    ap.add_argument("--max-minutes", type=float, default=15.0)
    ap.add_argument("--interval-seconds", type=float, default=20.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    key = load_fred_key()
    if not key:
        print("ERROR: 找不到 FRED_API_KEY（环境变量或 /home/jam/hk-investment/system/.env）", file=sys.stderr)
        return 2

    consensus = None
    if args.consensus:
        try:
            with open(args.consensus, "r", encoding="utf-8") as fh:
                consensus = json.load(fh)
        except (OSError, ValueError) as exc:
            print("ERROR: 读共识文件失败: %s" % exc, file=sys.stderr)
            return 2

    deadline = time.time() + args.max_minutes * 60.0
    baseline = None
    rows = {}
    cache = {}
    while True:
        rows = {}
        for spec in SERIES:
            for key_name in ("sa", "nsa"):
                sid = spec[key_name]
                try:
                    obs = fetch_observations(sid, key)
                except Exception as exc:  # noqa: BLE001 - 网络异常直接报出来
                    print("ERROR: 拉取 %s 失败: %s" % (sid, exc), file=sys.stderr)
                    return 3
                cache[(spec["name"], key_name)] = obs
            rows[spec["name"]] = build_block(
                cache[(spec["name"], "sa")], cache[(spec["name"], "nsa")])
        if not args.watch:
            break
        head_month = rows.get("headline", {}).get("month") if rows.get("headline") else None
        if baseline is None:
            baseline = head_month
            if not args.quiet:
                print("watch: 基线月份 = %s,每 %.0fs 轮询,最多 %.0f 分钟" % (
                    baseline, args.interval_seconds, args.max_minutes), file=sys.stderr)
        elif head_month != baseline:
            if not args.quiet:
                print("watch: 检测到新月份 %s,出表" % head_month, file=sys.stderr)
            break
        if time.time() >= deadline:
            print("watch: 超时,仍未见新月份(基线 %s),按当前读数出表" % baseline, file=sys.stderr)
            break
        time.sleep(args.interval_seconds)

    as_of = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M CST")
    text = render(rows, consensus, as_of)
    print(text)

    if args.json_out:
        payload = {
            "generated_at": as_of,
            "source": "FRED (BLS 原始发布)",
            "series": {spec["name"]: {"sa": spec["sa"], "nsa": spec["nsa"]} for spec in SERIES},
            "readings": rows,
            "consensus": consensus,
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        if not args.quiet:
            print("已落盘: %s" % args.json_out, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

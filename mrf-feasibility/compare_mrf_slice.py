#!/usr/bin/env python3
"""把多家医院的 MRF 切片 JSONL 合成一张比价表 —— 「N 家医院 × 1 个码」的交付物。

输入: mrf_slice.py 产出的 *.jsonl(每行一条 standard_charges 条目)
输出: ① 明细 CSV(hospital, code, payer, plan, price) ② 汇总 markdown(min/max/倍数/付款方数)
用法: python3 compare_mrf_slice.py --out-dir <目录> file1.jsonl=医院名 file2.jsonl=医院名 ...
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict


def load(pairs):
    rows = []
    for path, hospital in pairs:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rec["hospital"] = hospital
            rows.append(rec)
    return rows


def best_variant(recs):
    """同一 (hospital, code) 取「带金额付款方最多」的那条 variant 作为代表。"""
    best = {}
    for r in recs:
        k = (r["hospital"], r["code"])
        cur = best.get(k)
        score = r.get("n_payers_with_dollar") or 0
        if cur is None or score > (cur.get("n_payers_with_dollar") or 0):
            best[k] = r
    return best


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+", help="path=医院名")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)

    pairs = []
    for p in args.pairs:
        path, hospital = p.split("=", 1)
        pairs.append((path, hospital))

    recs = load(pairs)
    best = best_variant(recs)
    os.makedirs(args.out_dir, exist_ok=True)

    detail_path = os.path.join(args.out_dir, "slice_detail.csv")
    with open(detail_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hospital", "code", "code_type", "description", "payer", "plan", "price_usd"])
        for r in recs:
            for p in r.get("payers") or []:
                w.writerow([r["hospital"], r["code"], r["code_type"], r["description"],
                            p.get("payer"), p.get("plan"), p.get("price")])

    # 汇总: 每个 code 一行 x 医院
    lines = ["# MRF 比价切片 — 同一编码跨医院对照", "",
             "数据来源: 各医院按 45 CFR 180.50 公布的 CMS 标准收费文件(version 3.0.0)。",
             "价格口径: `minimum`/`maximum` 为该医院自报的最低/最高谈判价;付款方列取 `standard_charge_dollar`。", ""]
    by_code = defaultdict(list)
    for (hospital, code), r in best.items():
        by_code[code].append(r)

    for code in sorted(by_code):
        rs = sorted(by_code[code], key=lambda x: x["hospital"])
        desc = rs[0]["description"]
        lines.append(f"## {code} — {desc}")
        lines.append("")
        lines.append("| 医院 | min 谈判价 (USD) | max 谈判价 (USD) | 有金额付款方 | 现金价 | 标价 |")
        lines.append("|---|---|---|---|---|---|")
        for r in rs:
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                r["hospital"], r.get("minimum_negotiated"), r.get("maximum_negotiated"),
                r.get("n_payers_with_dollar"), r.get("discounted_cash"), r.get("gross_charge")))
        mins = [r["minimum_negotiated"] for r in rs if isinstance(r.get("minimum_negotiated"), (int, float))]
        maxs = [r["maximum_negotiated"] for r in rs if isinstance(r.get("maximum_negotiated"), (int, float))]
        if len(mins) >= 2:
            lo, hi = min(mins), max(mins)
            lines.append("")
            lines.append(f"- 最低谈判价跨院区间: **${lo:,.2f} – ${hi:,.2f}**（高低差 **{hi/lo:.2f}x**，绝对值差 ${hi-lo:,.2f}）")
        if len(maxs) >= 2:
            lo2, hi2 = min(maxs), max(maxs)
            lines.append(f"- 最高谈判价跨院区间: **${lo2:,.2f} – ${hi2:,.2f}**（高低差 **{hi2/lo2:.2f}x**）")
        lines.append("")

    lines += ["## 口径与局限(必须随切片一起交付,不许省)", "",
              "1. 这是**医院自报的谈判价**,不是任何病人的实际账单;实际支付还取决于计划类型、免赔、共付。",
              "2. 每家医院列出的付款方集合不同,「跨院同付款方」不可直接比;上表比的是各家自报的 min/max 包络。",
              "3. 同一编码在同一家医院可能挂在**多条 standard_charges variant** 上(不同 billing_class/methodology),",
              "   本表取「带金额付款方最多」的那条作为代表 —— 取第一条会解空(UCSF/Stanford 实测)。",
              "4. 现金价(discounted_cash)与标价(gross_charge)在 inpatient MS-DRG 条目层**普遍缺失**,",
              "   显示为空即该院此码未按 CMS 要求填这两个字段,本身就是可报告的合规缺口。",
              "",
              "*本文为情报分析,不构成投资建议、不构成医疗或法律意见。*"]

    md = "\n".join(lines) + "\n"
    summ_path = os.path.join(args.out_dir, "slice_summary.md")
    open(summ_path, "w", encoding="utf-8").write(md)
    print(md)
    print(f"[written] {detail_path}")
    print(f"[written] {summ_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""TVL flow decomposition v0.1  (alpha-scout)

目的: 把 DeFi TVL 的日变化拆成「价格效应」与「真实进出流」。
方法: 同一 category 内协议的主要资产敞口相近, 用「类目日变化中位数」当该类的价格基准,
      协议自身变化与中位数之差 >= 阈值的, 视为「价格解释不了」=> 真实流/事件候选。
      (稳健、可复算、不依赖叙事; 缺点是类目内部仍可能异质, 所以输出只当线索, 不当结论)

用法: python3 flow_decomp.py [--min-tvl 50000000] [--dev 8] [--out DIR]
数据源: DeFiLlama 免费公开 API (/protocols, stablecoins.llama.fi/stablecoins)
"""
import argparse, json, os, statistics, time, urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def fetch_json(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def num(x):
    try:
        return float(x)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-tvl", type=float, default=50e6)
    ap.add_argument("--dev", type=float, default=8.0, help="deviation from category median, pp")
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    protos = fetch_json("https://api.llama.fi/protocols")
    stables = fetch_json("https://stablecoins.llama.fi/stablecoins?includePrices=true")
    pegs = stables.get("peggedAssets", [])

    # category medians of 1d change
    by_cat = {}
    for p in protos:
        tvl = num(p.get("tvl")) or 0
        c1 = num(p.get("change_1d"))
        cat = p.get("category") or "?"
        if tvl >= a.min_tvl and c1 is not None:
            by_cat.setdefault(cat, []).append(c1)
    med = {c: statistics.median(v) for c, v in by_cat.items() if len(v) >= 4}

    rows = []
    for p in protos:
        tvl = num(p.get("tvl")) or 0
        c1 = num(p.get("change_1d"))
        c7 = num(p.get("change_7d"))
        cat = p.get("category") or "?"
        if tvl < a.min_tvl or c1 is None or cat not in med:
            continue
        dev = c1 - med[cat]
        rows.append({
            "name": p.get("name"), "category": cat, "chain": p.get("chain"),
            "tvl_usd": tvl, "chg_1d": c1, "chg_7d": c7,
            "cat_median_1d": med[cat], "dev_pp": dev,
        })

    flagged = sorted([r for r in rows if abs(r["dev_pp"]) >= a.dev], key=lambda r: r["dev_pp"])

    # stablecoin price-neutral flow
    def circ(x):
        return num(x.get("circulating", {}).get("peggedUSD")) or 0
    tot = sum(circ(x) for x in pegs)
    d1 = sum(num(x.get("circulatingPrevDay", {}).get("peggedUSD")) or circ(x) for x in pegs)
    w1 = sum(num(x.get("circulatingPrevWeek", {}).get("peggedUSD")) or circ(x) for x in pegs)
    buckets = {}
    for x in pegs:
        sym = (x.get("symbol") or "?").upper()
        b = buckets.setdefault(sym, [0.0, 0.0, 0.0])
        b[0] += circ(x)
        b[1] += num(x.get("circulatingPrevDay", {}).get("peggedUSD")) or circ(x)
        b[2] += num(x.get("circulatingPrevWeek", {}).get("peggedUSD")) or circ(x)

    out = {
        "generated_at_utc": stamp,
        "method": "category-median 1d change as price benchmark; deviation = protocol 1d - category median (pp)",
        "params": {"min_tvl": a.min_tvl, "dev_pp": a.dev},
        "category_medians_1d": {k: round(v, 2) for k, v in sorted(med.items(), key=lambda kv: -len(by_cat[kv[0]]))},
        "stablecoin_total": {"now": tot, "d1_pct": (tot / d1 - 1) * 100 if d1 else None,
                             "d7_pct": (tot / w1 - 1) * 100 if w1 else None,
                             "net_1d_usd": tot - d1, "net_7d_usd": tot - w1},
        "stablecoin_top": sorted([{"symbol": s, "circ": v[0], "d1_pct": (v[0] / v[1] - 1) * 100 if v[1] else None,
                                   "d7_pct": (v[0] / v[2] - 1) * 100 if v[2] else None}
                                  for s, v in buckets.items() if v[0] > 100e6], key=lambda r: -r["circ"])[:15],
        "flagged": flagged,
    }
    with open(os.path.join(a.out, "flow_decomp.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    md = []
    md.append(f"# TVL 流 vs 价格拆解 · {stamp}\n")
    md.append(f"样本: TVL>={a.min_tvl/1e6:.0f}M 且有 1d 变化的协议 {len(rows)} 个; 偏离类目中位数 >={a.dev}pp 的 {len(flagged)} 个。\n")
    md.append("## 类目 1d 中位数(= 该类价格基准)")
    for k, v in sorted(med.items(), key=lambda kv: -(kv[1])):
        md.append(f"- {k}: {v:+.2f}%  (n={len(by_cat[k])})")
    md.append("\n## 稳定币总量(价格中性, 可当真实流)")
    s = out["stablecoin_total"]
    md.append(f"- 总量 ${s['now']/1e9:.1f}B | 1d {s['d1_pct']:+.2f}% (${s['net_1d_usd']/1e9:+.2f}B) | 7d {s['d7_pct']:+.2f}% (${s['net_7d_usd']/1e9:+.2f}B)")
    md.append("\n## 偏离类目基准最多的协议(真实流/事件候选)")
    md.append("| 协议 | 类目 | 链 | TVL | 1d | 类目中位 | 偏离 | 7d |")
    md.append("|---|---|---|---|---|---|---|---|")
    for r in flagged[:25]:
        md.append(f"| {r['name']} | {r['category']} | {r['chain']} | ${r['tvl_usd']/1e6:.0f}M | {r['chg_1d']:+.1f}% | {r['cat_median_1d']:+.1f}% | {r['dev_pp']:+.1f}pp | "
                  f"{(('%+.1f%%' % r['chg_7d']) if r['chg_7d'] is not None else 'n/a')} |")
    md.append("\n> 口径: 类目中位数只是粗基准(类目内部资产敞口并不一致), 结论只能当线索, 必须回协议官方/链上核验。")
    with open(os.path.join(a.out, "flow_decomp.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()

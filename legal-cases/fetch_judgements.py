#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取英国国家档案馆 Find Case Law 的加密相关判例，落 judgements.jsonl。

原则：
  * 只读官方源（caselaw.nationalarchives.gov.uk 的公开 Atom feed）。
  * 字段全部来自 feed 原文（标题 / 中性引注 / 法院 / 发布日期 / 链接），不做解读、不做法律意见。
  * 幂等：按中性引注去重，重复运行只会追加新案。

用法：
  python3 fetch_judgements.py                 # 默认查询词表
  python3 fetch_judgements.py --queries bitcoin "digital assets"
  python3 fetch_judgements.py --limit 50      # 每个词最多取多少条
"""
import argparse
import html
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://caselaw.nationalarchives.gov.uk"
FEED = BASE + "/atom.xml?query="
DEFAULT_QUERIES = [
    "bitcoin", "cryptocurrency", "cryptoasset", "digital assets",
    "stablecoin", "tether", "fraud crypto",
]
OUT_DIR = "/home/jam/projects/alpha-scout-products/legal-cases"
OUT = os.path.join(OUT_DIR, "judgements.jsonl")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) alpha-scout-judgements/1.0"}

# 只做「资产类型」的粗分类，命中不到就标 unknown，不猜。
ASSET_PATTERNS = [
    ("BTC", r"\bbitcoin|\bbtc\b"),
    ("ETH", r"\bether(eum)?\b|\beth\b"),
    ("USDT", r"tether|\busdt\b"),
    ("stablecoin", r"stablecoin"),
    ("general", r"crypto|digital asset"),
]


def fetch_feed(query, limit=30):
    # 注意：feed 不支持 order=date（实测会按最旧排序并把最新判例挤出首页），
    # 默认排序即「相关度 + 最新在前」，只用 per_page 控制条数。
    url = FEED + urllib.parse.quote(query) + "&per_page=%d" % limit
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", "ignore")


def parse_entries(xml):
    out = []
    for block in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        def grab(pat):
            m = re.search(pat, block, re.S)
            return m.group(1).strip() if m else ""
        title = html.unescape(re.sub(r"<[^>]+>", "", grab(r"<title>(.*?)</title>")))
        link = grab(r'<link href="([^"]+)" rel="alternate"/>')
        published = grab(r"<published>(.*?)</published>")[:10]
        court = html.unescape(grab(r"<author>\s*<name>(.*?)</name>"))
        m = re.search(r'type="ukncn">(.*?)</tna:identifier>', block, re.S)
        citation = html.unescape(m.group(1)) if m else ""
        if not citation or not link:
            continue
        out.append({
            "citation": citation,
            "title": title,
            "court": court,
            "date": published,
            "url": link,
            "source": "caselaw.nationalarchives.gov.uk",
        })
    return out


def classify(text):
    low = text.lower()
    for tag, pat in ASSET_PATTERNS:
        if re.search(pat, low):
            return tag
    return "unknown"


def load_existing(path):
    seen = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                seen[row.get("citation", "")] = row
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES)
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    seen = load_existing(OUT)
    before = len(seen)
    errors = 0
    new = 0

    for q in args.queries:
        try:
            xml = fetch_feed(q, args.limit)
        except Exception as exc:  # 网络异常直接记，不静默吞
            errors += 1
            print("ERROR query=%r %s" % (q, exc))
            continue
        for row in parse_entries(xml):
            key = row["citation"]
            if key in seen:
                seen[key].setdefault("queries", []).append(q) if q not in seen[key].get("queries", []) else None
                continue
            row["asset_type"] = classify(row["title"] + " " + q)
            row["queries"] = [q]
            row["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            seen[key] = row
            new += 1

    rows = sorted(seen.values(), key=lambda r: r.get("date", ""), reverse=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("queries=%d errors=%d new=%d total=%d -> %s" % (
        len(args.queries), errors, new, len(rows), OUT))
    for row in rows[:8]:
        print("  %s | %s | %s | %s" % (row.get("date"), row.get("citation"),
                                       row.get("court")[:40], row["title"][:70]))
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

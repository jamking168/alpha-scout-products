#!/usr/bin/env python3
"""MRF 流式解析器 —— 从美国医院价格透明文件里取「项目名 / 现金价 / 最低谈判价」。

支持: .json / .json.zip (CMS v3 standard_charge_information) 与 .csv (CMS 模板)。
设计要点（都踩过坑）:
  1. 不用 json.load 整文件 —— NYP 单个 MRF 解压后 229 MB, 全量加载会把 3.7G 内存的机器压死;
     改用分块增量解码 + json.JSONDecoder.raw_decode() 逐条取, 内存有界。
  2. CMS v3.0.0 里「条目层」没有 discounted_cash/gross_charge, 单价在
     standard_charges[].payers_information[].standard_charge_dollar, 条目层只有 minimum/maximum。
     按 v2 文档写会全部解空。
  3. 部分 CDN 忽略 HTTP Range, 不能靠 -r 只取头部; 下载务必带 --max-filesize 兜底。

用法:
  python3 parse_mrf_stream.py <文件路径> [--items N] [--max-scan 12000000]
"""
from __future__ import annotations

import argparse
import codecs
import csv
import io
import json
import sys
import zipfile

OPENERS = {}


def open_member(path):
    """返回 (二进制流, 成员名)。zip 取第一个 .json/.csv 成员。"""
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        names = [n for n in z.namelist() if n.lower().endswith((".json", ".csv"))]
        name = names[0] if names else z.namelist()[0]
        OPENERS["zipfile"] = z
        return z.open(name), name
    return open(path, "rb"), path


def parse_json_stream(fh, want_items, max_scan):
    """流式取 standard_charge_information 的前 N 条。"""
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decoder = json.JSONDecoder()
    buf, items, scanned, tail = "", [], 0, b""
    started = False
    top_head = ""
    while scanned < max_scan and len(items) < want_items:
        chunk = fh.read(131072)
        if not chunk:
            break
        scanned += len(chunk)
        buf += dec.decode(chunk)
        if not started:
            i = buf.find('"standard_charge_information"')
            if i >= 0:
                j = buf.find("[", i)
                if j >= 0:
                    top_head = buf[:i]
                    buf = buf[j + 1:]
                    started = True
                    continue
            if len(buf) > 8000:
                top_head = buf[:4000]
                buf = buf[-2000:]
            continue
        while len(items) < want_items:
            buf = buf.lstrip()
            if not buf:
                break
            if buf[0] == ",":
                buf = buf[1:]
                continue
            if buf[0] == "]":
                started = False
                break
            if buf[0] != "{":
                buf = buf[1:]
                continue
            try:
                obj, end = decoder.raw_decode(buf)
            except ValueError:
                break
            items.append(obj)
            buf = buf[end:]
    return top_head, items, scanned


def extract(item, variant=0):
    """把一条 standard_charge_information 元素规范成三列。

    注意 1: 不要拿「第一个付款方的价」冒充现金价。现金价必须是
    ① 条目层/付款方层真正的 discounted_cash 字段；或 ② payer_name 为 Cash/Self-pay 的那条。
    都没有就是没有（缺字段本身就是这家医院的合规缺口，要如实标 None）。
    注意 2: 一个 code 可能挂在**多条** standard_charges variant 上（不同 billing_class/methodology），
    价格只出现在其中一部分；取 [0] 会解空（UCSF/Stanford 实测）。默认 variant=0 只为打印结构，
    要取价请先用 pick_priced_variant() 选。
    """
    scs = item.get("standard_charges") or [{}]
    sc = scs[variant] if variant < len(scs) else scs[0]
    payers = sc.get("payers_information") or []
    cash = sc.get("discounted_cash")
    payer_cash = None
    for p in payers:
        if isinstance(p.get("discounted_cash"), (int, float)):
            cash = cash if cash is not None else p["discounted_cash"]
        nm = str(p.get("payer_name") or "").lower()
        if any(w in nm for w in ("cash", "self-pay", "self pay", "selfpay")):
            payer_cash = p.get("standard_charge_dollar")
    codes = item.get("code_information") or [{}]
    return {
        "description": item.get("description"),
        "code": codes[0].get("code") if codes else None,
        "code_type": codes[0].get("type") if codes else None,
        "setting": sc.get("setting"),
        "discounted_cash": cash if cash is not None else payer_cash,
        "minimum_negotiated": sc.get("minimum"),
        "maximum_negotiated": sc.get("maximum"),
        "gross_charge": sc.get("gross_charge"),
        "payers": len(payers),
        "first_payer_price": payers[0].get("standard_charge_dollar") if payers else None,
    }


def pick_priced_variant(item):
    """返回 (variant 下标, sc) —— 带金额付款方最多的那条; 都没有则回退第 0 条。"""
    scs = item.get("standard_charges") or []
    best, best_score, best_i = None, -1, 0
    for i, sc in enumerate(scs):
        score = sum(1 for p in (sc.get("payers_information") or [])
                    if p.get("standard_charge_dollar") is not None)
        if sc.get("minimum") is not None:
            score += 1
        if score > best_score:
            best, best_score, best_i = sc, score, i
    if best is None:
        return 0, {}
    return best_i, best


def parse_csv(fh, want_items, max_scan):
    text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
    reader = csv.DictReader(text)
    rows = []
    for row in reader:
        rows.append(row)
        if len(rows) >= want_items:
            break
    return "", rows, -1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--items", type=int, default=3)
    ap.add_argument("--max-scan", type=int, default=12_000_000)
    args = ap.parse_args(argv)

    fh, name = open_member(args.path)
    print(f"成员: {name}")
    if name.lower().endswith(".csv"):
        head, rows, _ = parse_csv(fh, args.items, args.max_scan)
        for r in rows:
            print({k: r.get(k) for k in list(r)[:8]})
        return 0

    top_head, items, scanned = parse_json_stream(fh, args.items, args.max_scan)
    version = None
    for key in ('"version"',):
        i = top_head.find(key)
        if i >= 0:
            version = top_head[i:i + 40]
    print(f"扫描 {scanned/1e6:.2f} MB，取到 {len(items)} 条；顶层片段: {version}")
    for i, it in enumerate(items, 1):
        e = extract(it)
        print(f"\n[{i}] {e['description']}")
        print(f"    编码 {e['code']} ({e['code_type']}) | setting={e['setting']} | 付款方 {e['payers']} 家")
        print(f"    项目名 description          : {e['description']}")
        print(f"    现金价 discounted_cash      : {e['discounted_cash']}")
        print(f"    最低谈判价 minimum          : {e['minimum_negotiated']}")
        print(f"    最高谈判价 maximum          : {e['maximum_negotiated']}")
        print(f"    标价 gross_charge           : {e['gross_charge']}")
        print(f"    (首个付款方价，非现金价，仅参考): {e['first_payer_price']}")
    if not items:
        print("未取到条目 —— 该文件可能不是 CMS v3 结构（检查 standard_charge_information 键名）")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

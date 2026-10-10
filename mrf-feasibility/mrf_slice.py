#!/usr/bin/env python3
"""MRF 最小切片器 —— 从某家医院的 CMS 价格透明文件里，抽出「指定编码」的条目，落成 JSONL。

用途: 做「N 家医院 × 1 个码」的比价切片, 拿去给福利顾问/自保雇主看, 验证付费意愿。
      不做全量镜像(TB 级, 必白干), 只按需抽码。

与 parse_mrf_stream.py 的区别: 那个取「前 N 条」做结构验证, 这个**扫全文件 + 按码过滤**,
且同时抓 ① 条目层 minimum/maximum ② 付款方级 standard_charge_dollar(逐付款方) ③ 现金价。

内存有界: 分块增量解码 + raw_decode 逐条, 永不 json.load 整文件(单文件解压可达 229MB+)。
下载务必带 --max-filesize 兜底(部分 CDN 忽略 HTTP Range)。

用法:
  python3 mrf_slice.py <文件> --code 001 [--code 003] [--code-type MS-DRG] [--out out.jsonl]
"""
from __future__ import annotations

import argparse
import codecs
import json
import sys
import zipfile

HEAD_KEYS = ("hospital_name", "location_name", "last_updated_on", "version", "type_2_npi", "license_number")


def open_member(path):
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        names = [n for n in z.namelist() if n.lower().endswith((".json", ".csv"))]
        name = names[0] if names else z.namelist()[0]
        return z.open(name), name, z
    return open(path, "rb"), path, None


def grab_head(text: str) -> dict:
    """从顶层片段里取医院元信息(不解析整文件)。"""
    out = {}
    for k in HEAD_KEYS:
        i = text.find(f'"{k}"')
        if i < 0:
            continue
        j = text.find(":", i)
        if j < 0:
            continue
        dec = json.JSONDecoder()
        try:
            val, _ = dec.raw_decode(text[j + 1:].lstrip())
        except ValueError:
            continue
        out[k] = val
    return out


def scan(path, codes, code_types, setting_want):
    fh, name, z = open_member(path)
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decoder = json.JSONDecoder()
    buf = ""
    head = {}
    scanned = 0
    matched = []
    started = False
    done = False
    while not done:
        chunk = fh.read(262144)
        if not chunk:
            break
        scanned += len(chunk)
        buf += dec.decode(chunk)
        if not started:
            i = buf.find('"standard_charge_information"')
            if i >= 0:
                head = grab_head(buf[:i])
                j = buf.find("[", i)
                if j >= 0:
                    buf = buf[j + 1:]
                    started = True
                    continue
            if len(buf) > 60000:
                head = head or grab_head(buf[:40000])
                buf = buf[-4000:]
            continue
        while True:
            buf = buf.lstrip()
            if not buf:
                break
            if buf[0] == ",":
                buf = buf[1:]
                continue
            if buf[0] == "]":
                done = True
                break
            if buf[0] != "{":
                buf = buf[1:]
                continue
            try:
                obj, end = decoder.raw_decode(buf)
            except ValueError:
                break
            buf = buf[end:]
            for ci in (obj.get("code_information") or []):
                code = str(ci.get("code") or "").strip()
                ctype = str(ci.get("type") or "").strip()
                if code not in codes or (code_types and ctype not in code_types):
                    continue
                # 关键: 同一 code 可能挂在**多条** standard_charges 上(不同 billing_class /
                # methodology 分支), 价格只出现在其中一部分 —— 取 [0] 会漏(UCSF 实测:
                # charges[0] 是 algorithm 报价无金额, charges[1] 才有 minimum/maximum)。
                for sc in (obj.get("standard_charges") or []):
                    if setting_want and sc.get("setting") != setting_want:
                        continue
                    payers = sc.get("payers_information") or []
                    prices = [
                        {
                            "payer": p.get("payer_name"),
                            "plan": p.get("plan_name"),
                            "price": p.get("standard_charge_dollar"),
                            "cash": p.get("discounted_cash"),
                        }
                        for p in payers
                        if p.get("standard_charge_dollar") is not None
                    ]
                    cash = sc.get("discounted_cash")
                    if cash is None:
                        for p in payers:
                            if isinstance(p.get("discounted_cash"), (int, float)):
                                cash = p["discounted_cash"]
                                break
                    if cash is None:
                        for p in payers:
                            nm = str(p.get("payer_name") or "").lower()
                            if any(w in nm for w in ("cash", "self-pay", "self pay", "selfpay")):
                                cash = p.get("standard_charge_dollar")
                                break
                    prices_sorted = sorted(prices, key=lambda x: (x["price"] is None, x["price"]))
                    matched.append({
                        "hospital_file": path.split("/")[-1],
                        "member": name,
                        "code": code,
                        "code_type": ctype,
                        "setting": sc.get("setting"),
                        "billing_class": sc.get("billing_class"),
                        "description": obj.get("description"),
                        "minimum_negotiated": sc.get("minimum"),
                        "maximum_negotiated": sc.get("maximum"),
                        "gross_charge": sc.get("gross_charge"),
                        "discounted_cash": cash,
                        "n_payers": len(payers),
                        "n_payers_with_dollar": len(prices),
                        "has_dollar_variant": bool(prices) or sc.get("minimum") is not None,
                        "payers": prices_sorted,
                    })
                break
    return head, matched, scanned, name


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--code", action="append", required=True, help="目标编码, 可重复")
    ap.add_argument("--code-type", action="append", default=[], help="限定编码体系, 如 MS-DRG / CPT")
    ap.add_argument("--setting", default=None, help="限定 setting, 如 inpatient")
    ap.add_argument("--out", default=None, help="JSONL 输出路径, 不传则打印摘要")
    args = ap.parse_args(argv)

    codes = {str(c).strip().upper() for c in args.code}
    code_types = {str(t).strip().upper() for t in args.code_type}
    head, matched, scanned, member = scan(args.path, codes, code_types, args.setting)

    print(f"文件: {args.path.split('/')[-1]} | 成员: {member}")
    print(f"医院元信息: {json.dumps(head, ensure_ascii=False)}")
    print(f"扫描 {scanned/1e6:.1f} MB(解压后) | 命中 {len(matched)} 条 | 目标码 {sorted(codes)}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for m in matched:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        print(f"已写 {args.out}")

    for m in matched[:8]:
        print(f"\n[{m['code']}/{m['code_type']}] {m['description']}")
        print(f"    setting={m['setting']} billing_class={m['billing_class']} "
              f"min={m['minimum_negotiated']} max={m['maximum_negotiated']} "
              f"cash={m['discounted_cash']} gross={m['gross_charge']} "
              f"付款方 {m['n_payers']} 家(带金额 {m['n_payers_with_dollar']})")
        for p in m["payers"][:3]:
            print(f"      ├ {p['payer']} / {p['plan']}: {p['price']}")
        for p in m["payers"][-2:]:
            print(f"      └ {p['payer']} / {p['plan']}: {p['price']}")
    return 0 if matched else 2


if __name__ == "__main__":
    sys.exit(main())

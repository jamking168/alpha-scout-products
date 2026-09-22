#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FOMC 决议即时 diff 告警器 (FOMC Decision Diff Alert)
====================================================

在 FOMC 决议发布后 5 分钟内,自动产出中文摘要:
  1) 决议声明措辞变化 (statement diff)   — 数据源: 美联储官网 press release
  2) 点阵图中位数变化 (SEP dot plot)      — 数据源: 美联储官网 fomcprojtabl 页
  3) 联邦基金目标利率区间变化 (FRED)      — 数据源: FRED API (DFEDTARU / DFEDTARL)
  4) 市场隐含概率跳变 (CME FedWatch)      — 数据源: CME FedWatch 公开页(可能被 WAF/地域封锁)

约束: 只用免费公开数据。FRED_API_KEY 从 .env 读取(默认 /home/jam/hk-investment/system/.env),
      密钥永不打印、不落盘到输出文件。

用法:
  python3 fomc_diff.py                          # 自动定位最近一次已发布的 FOMC 决议及其上一次
  python3 fomc_diff.py --meeting 2026-09-16 --prev 2026-07-29
  python3 fomc_diff.py --fred-key-file /path/.env --no-fedwatch

输出: 中文摘要(标准输出) + JSON 落盘 output/fomc_diff_<date>.json
"""

import argparse
import datetime as dt
import difflib
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
FED_PR_BASE = "https://www.federalreserve.gov/newsevents/pressreleases"
FED_SEP_BASE = "https://www.federalreserve.gov/monetarypolicy"
CME_FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
CME_FEDWATCH_JSON = (
    "https://www.cmegroup.com/CmeWS/mvc/xsltTransformer.do"
    "?xlstDocId=/quiksuite/fedwatch/fedwatch_styles.xsl"
)

# FRED 必须用非浏览器 UA,否则 stlouisfed 的 WAF 会拒绝
UA_FRED = "curl/8.5.0"
UA_WEB = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

# 2026 年 FOMC 会议日历: (声明发布日期, 是否伴随 SEP 点阵图)
# 声明通常在会议第二日 14:00 ET 发布
FOMC_2026 = [
    ("2026-01-28", False),
    ("2026-03-18", True),
    ("2026-04-29", False),
    ("2026-06-17", True),
    ("2026-07-29", False),
    ("2026-09-16", True),
    ("2026-10-28", False),
    ("2026-12-09", True),
]


# ----------------------------------------------------------------------------
# HTTP 抓取
# ----------------------------------------------------------------------------

def http_get(url, ua=UA_WEB, timeout=30):
    """返回 (status, bytes) 或 (None, b'')。"""
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except Exception as e:  # noqa: BLE001
        return None, str(e).encode()


# ----------------------------------------------------------------------------
# .env 读取(不打印密钥)
# ----------------------------------------------------------------------------

def load_fred_key(path):
    """从 .env 读 FRED_API_KEY,返回字符串;找不到返回 None。"""
    if not os.path.isfile(path):
        return None
    key = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == "FRED_API_KEY":
                key = v.strip().strip('"').strip("'")
    return key or os.environ.get("FRED_API_KEY")


# ----------------------------------------------------------------------------
# 1) FRED: 联邦基金目标利率区间
# ----------------------------------------------------------------------------

def fred_target_range(api_key, series_id, limit=60):
    """返回 [{"date","value"}, ...] 按日期降序。"""
    url = (f"{FRED_API_URL}?series_id={series_id}&file_type=json"
           f"&sort_order=desc&limit={limit}&api_key={api_key}")
    st, body = http_get(url, ua=UA_FRED)
    if st != 200 or not body:
        return []
    try:
        return json.loads(body)["observations"]
    except Exception:  # noqa: BLE001
        return []


def range_delta(upper_obs, lower_obs):
    """从两个序列的观测中抽取 (当前区间, 前次区间, 生效日期, 变动bp)。

    每个 obs: {"date":..., "value":"4.00"}。value 可能为 "."(缺失)。
    返回 dict,找不到时对应字段为 None。
    """
    def runs(obs):
        """把(降序)观测转为升序的连续同值段 [(value, start_date), ...]。"""
        asc = list(reversed(obs))
        out = []
        for o in asc:
            v = o.get("value", ".")
            if v == ".":
                continue
            if not out or out[-1][0] != v:
                out.append([v, o["date"]])
        return out

    ur = runs(upper_obs)
    lr = runs(lower_obs)
    empty = {"current": None, "previous": None, "change_date": None, "bp": None}
    if not ur or not lr:
        return empty

    cur_u, cur_l = ur[-1][0], lr[-1][0]
    prev_u = ur[-2][0] if len(ur) > 1 else cur_u
    prev_l = lr[-2][0] if len(lr) > 1 else cur_l
    # 当前区间生效日 = 两个边界跳到当前值的较晚日期
    change_date = max(ur[-1][1], lr[-1][1])

    def to_bp(a, b):
        try:
            return round((float(a) - float(b)) * 100, 1)
        except (TypeError, ValueError):
            return None

    bp = to_bp(cur_u, prev_u) or to_bp(cur_l, prev_l)

    return {
        "current": f"{cur_l}-{cur_u}",
        "previous": f"{prev_l}-{prev_u}",
        "change_date": change_date,
        "bp": bp,
    }


# ----------------------------------------------------------------------------
# 2) 美联储声明: 抓取 + 文本抽取 + diff
# ----------------------------------------------------------------------------

class _TextExtract(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_article = False
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "div" and dict(attrs).get("id") == "article":
            self.in_article = True
        if tag in ("p", "h1", "h2", "h3", "li", "br", "div") and self.in_article:
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip > 0:
            self.skip -= 1

    def handle_data(self, data):
        if self.skip == 0 and self.in_article:
            self.buf.append(data)


_BOILERPLATE_RE = re.compile(
    r"^([A-Z][a-z]+ \d{1,2}, \d{4}|"
    r"Federal Reserve issues FOMC statement|"
    r"For release at|"
    r"Share|"
    r"For media inquiries|"
    r"Implementation Note issued|"
    r"Last Update:|"
    r"Media Contacts?|"
    r"^\d{4})"
)


def extract_statement_text(html_bytes):
    html = html_bytes.decode("utf-8", errors="replace")
    p = _TextExtract()
    p.feed(html)
    lines = []
    for ln in "".join(p.buf).split("\n"):
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln and not _BOILERPLATE_RE.match(ln):
            lines.append(ln)
    return "\n".join(lines)


def fetch_statement(date_str):
    """按日期抓声明,探测 a/b/c 后缀,返回 (url, text) 或 (None, None)。"""
    d = date_str.replace("-", "")
    for suffix in ("a", "b", "c"):
        url = f"{FED_PR_BASE}/monetary{d}{suffix}.htm"
        st, body = http_get(url, ua=UA_WEB)
        if st == 200 and body:
            return url, extract_statement_text(body)
    return None, None


def sentence_diff(old_text, new_text):
    """行级 diff:返回 (新增行, 删除行)。声明正文每段一行,行级对比噪声最小。"""
    def lines(t):
        return [ln.strip() for ln in t.split("\n") if ln.strip()]

    sm = difflib.SequenceMatcher(None, lines(old_text), lines(new_text))
    added, removed = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            added.extend(lines(new_text)[j1:j2])
        elif tag == "delete":
            removed.extend(lines(old_text)[i1:i2])
        elif tag == "replace":
            removed.extend(lines(old_text)[i1:i2])
            added.extend(lines(new_text)[j1:j2])
    return added, removed


# ----------------------------------------------------------------------------
# 3) 点阵图 SEP: 抓取 + 解析联邦基金利率中位数
# ----------------------------------------------------------------------------

class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self.cur_table = None
        self.cur_row = None
        self.cur_cell = None
        self.capture = False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.cur_table = []
        elif tag == "tr":
            self.cur_row = []
        elif tag in ("td", "th") and self.cur_table is not None:
            self.capture = True
            self.cur_cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.capture:
            self.capture = False
            if self.cur_row is not None:
                self.cur_row.append("".join(self.cur_cell).strip())
        elif tag == "tr" and self.cur_row is not None:
            self.cur_table.append(self.cur_row)
            self.cur_row = None
        elif tag == "table" and self.cur_table is not None:
            self.tables.append(self.cur_table)
            self.cur_table = None

    def handle_data(self, data):
        if self.capture:
            self.cur_cell.append(data)


def _clean(s):
    return re.sub(r"\s+", " ", s).strip()


def fetch_sep_median(date_str):
    """抓取并解析 SEP 表,返回联邦基金利率中位数行(含 June 对照)。

    返回 {"years": [...], "median": {...}, "june": {...}, "url": ...}
    年列为 2026/2027/2028/2029/Longer run;median 行第 2~6 个数字单元格。
    """
    d = date_str.replace("-", "")
    url = f"{FED_SEP_BASE}/fomcprojtabl{d}.htm"
    st, body = http_get(url, ua=UA_WEB)
    if st != 200 or not body:
        return None
    p = _TableParser()
    p.feed(body.decode("utf-8", errors="replace"))

    for table in p.tables:
        rows = [[_clean(c) for c in r] for r in table]
        years = None
        funds_idx = None
        for idx, row in enumerate(rows):
            # 年份表头行: 含 "2026" 且含 "Longer run"
            if years is None and "2026" in row and any("longer" in c.lower() for c in row):
                years = row[:5]
            if funds_idx is None and "federal funds rate" in " ".join(row).lower():
                funds_idx = idx
        if funds_idx is not None and years is not None:
            frow = rows[funds_idx]
            june_row = rows[funds_idx + 1] if funds_idx + 1 < len(rows) else None
            return {
                "url": url,
                "years": years,
                "median": frow[1:6],
                "june": june_row[1:6] if june_row else [],
            }
    return None


# ----------------------------------------------------------------------------
# 4) CME FedWatch(尽力而为;该站常对本 VPS 地域封锁)
# ----------------------------------------------------------------------------

def fetch_fedwatch():
    """返回 dict 或 None。主站不可达时返回 {"error": ...}。"""
    st, body = http_get(CME_FEDWATCH_URL, ua=UA_WEB, timeout=20)
    if st == 200 and body:
        return {"status": "fetched", "url": CME_FEDWATCH_URL, "bytes": len(body)}
    return {"status": "unreachable", "url": CME_FEDWATCH_URL}


# ----------------------------------------------------------------------------
# 汇总渲染
# ----------------------------------------------------------------------------

def render_cn(meeting, prev, fred, stmt, sep, fw, out_path):
    L = []
    L.append("=" * 72)
    L.append(f"FOMC 决议即时 diff 告警 | 决议日 {meeting} (对照 {prev})")
    L.append("=" * 72)

    L.append("\n【1】联邦基金目标利率区间 (FRED, A级)")
    if fred["current"]:
        arrow = "↑" if (fred["bp"] or 0) > 0 else ("↓" if (fred["bp"] or 0) < 0 else "→")
        L.append(f"  前值: {fred['previous']}%")
        L.append(f"  现值: {fred['current']}%  ({arrow} {fred['bp']}bp)")
        L.append(f"  生效日期: {fred['change_date']}")
    else:
        L.append("  获取失败")

    L.append("\n【2】决议声明措辞变化 (美联储官网 statement diff)")
    if stmt["new_text"]:
        L.append(f"  本次声明: {stmt['new_url']}")
        L.append(f"  前次声明: {stmt['old_url']}")
        added, removed = stmt["added"], stmt["removed"]
        if removed:
            L.append("  ─ 删除/改写 (前次有,本次无):")
            for s in removed:
                L.append(f"      - {s}")
        if added:
            L.append("  ─ 新增/改写 (本次新增):")
            for s in added:
                L.append(f"      + {s}")
        if not added and not removed:
            L.append("  (无实质措辞变化)")
    else:
        L.append("  声明获取失败")

    L.append("\n【3】点阵图中位数 (SEP, A级)")
    if sep:
        L.append(f"  数据: {sep['url']}")
        yrs = sep["years"]
        med = sep["median"]
        jun = sep["june"]
        L.append("  年        | 本次中位数 | 上次(June) | 变动")
        L.append("  ----------|------------|------------|------")
        for i, y in enumerate(yrs):
            m = med[i] if i < len(med) else "-"
            j = jun[i] if i < len(jun) and jun[i] else "-"
            delta = ""
            try:
                if m not in ("", "-") and j not in ("", "-"):
                    delta = f"{round((float(m) - float(j)) * 100, 0):+.0f}bp"
            except ValueError:
                delta = "?"
            L.append(f"  {y:<10}| {m:<11}| {j:<11}| {delta}")
    else:
        L.append("  点阵图获取失败")

    L.append("\n【4】市场隐含概率跳变 (CME FedWatch)")
    if fw and fw.get("status") == "fetched":
        L.append(f"  已抓取: {fw['url']} ({fw['bytes']} 字节)")
        L.append("  (注: 概率解析需结合 FedWatch 前端 JSON;此环境仅验证可达性)")
    else:
        L.append("  CME FedWatch 主站本 VPS 不可达(地域/IP 封锁)。")
        L.append("  生产上线必须通过美国/新加坡出口 IP 或代理解决此依赖。")

    L.append("\n" + "=" * 72)
    L.append(f"JSON 已落盘: {out_path}")
    L.append("=" * 72)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="FOMC 决议即时 diff 告警器")
    ap.add_argument("--meeting", default=None, help="决议声明日期 YYYY-MM-DD(默认: 最近一次已发布)")
    ap.add_argument("--prev", default=None, help="前次决议声明日期 YYYY-MM-DD(默认: 自动)")
    ap.add_argument("--fred-key-file", default="/home/jam/hk-investment/system/.env")
    ap.add_argument("--no-fedwatch", action="store_true")
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output"))
    args = ap.parse_args()

    # 定位会议对
    if args.meeting:
        meeting = args.meeting
        if not args.prev:
            past = [d for d, _ in FOMC_2026 if d < meeting]
            args.prev = past[-1] if past else None
    else:
        # 最近一次已发布的声明 = 日历中小于等于今天的最新日期
        today = dt.date.today().isoformat()
        past = [d for d, _ in FOMC_2026 if d <= today]
        if not past:
            meeting = FOMC_2026[-1][0]
        else:
            meeting = past[-1]
        before = [d for d, _ in FOMC_2026 if d < meeting]
        args.prev = before[-1] if before else None
    prev = args.prev

    # FRED
    key = load_fred_key(args.fred_key_file)
    fred = {"current": None, "previous": None, "change_date": None, "bp": None}
    if key:
        up = fred_target_range(key, "DFEDTARU")
        lo = fred_target_range(key, "DFEDTARL")
        fred = range_delta(up, lo)
    else:
        print("警告: 未找到 FRED_API_KEY,跳过 FRED 利率区间抓取", file=sys.stderr)

    # 声明
    new_url, new_text = fetch_statement(meeting)
    old_url, old_text = (None, None)
    if prev:
        old_url, old_text = fetch_statement(prev)
    added, removed = sentence_diff(old_text or "", new_text or "")
    stmt = {"new_url": new_url, "old_url": old_url, "new_text": new_text,
            "added": added, "removed": removed}

    # SEP
    sep = fetch_sep_median(meeting)

    # FedWatch
    fw = None if args.no_fedwatch else fetch_fedwatch()

    # 输出
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, f"fomc_diff_{meeting}.json")
    payload = {
        "generated_at_utc": dt.datetime.utcnow().isoformat() + "Z",
        "meeting": meeting, "prev": prev,
        "fed_funds": fred,
        "statement": {"new_url": new_url, "old_url": old_url,
                      "added": added, "removed": removed},
        "sep": sep,
        "fedwatch": fw,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(render_cn(meeting, prev, fred, stmt, sep, fw, out_path))


if __name__ == "__main__":
    main()

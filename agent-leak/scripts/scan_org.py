#!/usr/bin/env python3
"""agent-leak: detect plaintext credentials in AI-coding-agent config files
(.claude/settings.local.json etc.) committed to public GitHub.

Usage:
  GITHUB_TOKEN=... python3 scan_org.py --org someCompany [--extra-qualifier "filename:settings.json"]
  GITHUB_TOKEN=... python3 scan_org.py --query "filename:settings.local.json path:.claude" --max 60

Design notes (measured 2026-11-08):
- GitHub code search API works with an authenticated token (anonymous = 401).
- Free-tier search rate limit is 10 req/min -> sleep 7s between search pages.
- Never print secret values: masked only, and only 4 leading chars.
- Template references (${VAR}, {{x}}, <x>) are NOT secrets; exclude them explicitly,
  otherwise a naive regex reports ~3x false positives (measured 2/3 in a 60-file sample).
"""
import argparse, base64, json, os, re, sys, time, urllib.parse, urllib.request

TOKEN = os.environ.get("GITHUB_TOKEN", "")
if not TOKEN:
    sys.exit("GITHUB_TOKEN missing")

UA = "agent-leak-scan/0.1 (security research; contact: owner)"
STOP = ("$", "{{", "<", "%(", "process.env", "os.environ", "{{")

RULES = [
    ("pgpass", re.compile(r"PGPASSWORD\s*=\s*['\"]?([^'\"\s)]{6,})")),
    ("sshpass", re.compile(r"sshpass\s+-p\s*['\"]?([^'\"\s)]{6,})")),
    ("mysql_p", re.compile(r"mysql\s+[^\n]*-p([^'\"\s)\-]{6,})")),
    ("basic_auth", re.compile(r"curl\s+[^\n]*-u\s*[^:\s]+:([^'\"\s@]{6,})")),
    ("aws_ak", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("openai_sk", re.compile(r"\b(sk-[A-Za-z0-9\-_]{20,})")),
    ("github_pat", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})")),
    ("jwt", re.compile(r"\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})")),
    ("url_creds", re.compile(r"https?://[^/\s:@]+:([^/\s@]{6,})@")),
    ("kv_secret", re.compile(r"(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*[\"']([^\"'\s]{12,})[\"']", re.I)),
]


def gh(url):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def masked(v):
    return (v[:4] + "*" * max(0, min(len(v) - 4, 12)) + "(%d)" % len(v)) if v else "?"


def find_secrets(text):
    hits = []
    for name, pat in RULES:
        for m in pat.finditer(text):
            val = m.group(1)
            if any(s in val for s in STOP):
                continue
            if name == "kv_secret" and len(set(val)) < 6:
                continue
            hits.append((name, masked(val)))
    return hits


def collect(query, max_files):
    items = []
    page = 1
    while len(items) < max_files and page <= 10:
        url = "https://api.github.com/search/code?q=%s&per_page=30&page=%d" % (
            urllib.parse.quote(query), page)
        try:
            d = gh(url)
        except Exception as e:
            print("search_fail p%d %s" % (page, e), flush=True)
            break
        got = d.get("items", [])
        items.extend(got)
        print("search page %d: +%d (total_count=%s)" % (page, len(got), d.get("total_count")), flush=True)
        if len(got) < 30:
            break
        page += 1
        time.sleep(7)
    return items[:max_files]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org")
    ap.add_argument("--query")
    ap.add_argument("--max", type=int, default=30)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    q = a.query or ("filename:settings.local.json path:.claude" + (" org:" + a.org if a.org else ""))
    if a.org and not a.query:
        q = "filename:settings.local.json org:" + a.org
    items = collect(q, a.max)
    rows = []
    for it in items:
        repo = it["repository"]["full_name"]
        path = it["path"]
        row = {"repo": repo, "path": path}
        try:
            c = gh("https://api.github.com/repos/%s/contents/%s" % (repo, urllib.parse.quote(path)))
            raw = base64.b64decode(c.get("content", "")).decode("utf-8", "replace")
        except Exception as e:
            row["status"] = "fetch_fail:%s" % type(e).__name__
            rows.append(row)
            time.sleep(0.4)
            continue
        row["status"] = "ok"
        row["size"] = len(raw)
        row["secrets"] = find_secrets(raw)
        rows.append(row)
        time.sleep(0.4)
    ok = [r for r in rows if r.get("status") == "ok"]
    bad = [r for r in ok if r.get("secrets")]
    summary = {"query": q, "sampled": len(rows), "parsed_ok": len(ok),
               "files_with_secret": len(bad),
               "rate_pct": round(100.0 * len(bad) / max(1, len(ok)), 2),
               "repos_affected": len(set(r["repo"] for r in bad)),
               "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for r in bad:
        print("  LEAK %s %s -> %s" % (r["repo"], r["path"], [s[0] for s in r["secrets"]]))
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data",
                                "scan-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=1)
    print("saved", os.path.abspath(out))


if __name__ == "__main__":
    main()

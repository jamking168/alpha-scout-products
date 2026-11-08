#!/usr/bin/env python3
"""agent-leak: owner-scoped scan (works where GitHub code-search org: qualifier returns 0).

Lists a GitHub org/user's public repos via REST, then probes each repo for known
AI-agent config files through the contents API (404 == absent). This is the
sales-motion path: one target = one deterministic, reproducible scan.

Usage:
  GITHUB_TOKEN=... python3 scan_owner.py --owner supabase --max-repos 60
Outputs a redacted JSON + a short human summary. Secret VALUES are never printed.
"""
import argparse, base64, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

TOKEN = os.environ.get("GITHUB_TOKEN", "")
if not TOKEN:
    sys.exit("GITHUB_TOKEN missing")
UA = "agent-leak-scan/0.1 (security research)"

TARGETS = [
    ".claude/settings.local.json",
    ".claude/settings.json",
    ".claude/settings.local.json.bak",
    ".mcp.json",
    ".cursor/mcp.json",
]
STOP = ("$", "{{", "<", "%(", "process.env", "os.environ")
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


def gh(url, ok404=False):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + TOKEN,
                                               "Accept": "application/vnd.github+json",
                                               "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()), r.status
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None, 404
        raise


def masked(v):
    return v[:4] + "*" * min(max(len(v) - 4, 1), 10) + "(%d)" % len(v)


def find_secrets(text):
    hits = []
    for name, pat in RULES:
        for m in pat.finditer(text):
            val = m.group(1)
            if any(s in val for s in STOP):
                continue
            if name == "kv_secret" and len(set(val)) < 6:
                continue
            hits.append({"type": name, "value_masked": masked(val)})
    return hits


def list_repos(owner, limit):
    out = []
    page = 1
    while len(out) < limit and page <= 5:
        d, _ = gh("https://api.github.com/orgs/%s/repos?per_page=100&page=%d&type=public" % (owner, page), ok404=True)
        if d is None:
            d, _ = gh("https://api.github.com/users/%s/repos?per_page=100&page=%d" % (owner, page), ok404=True)
        if not d:
            break
        out.extend([r for r in d if not r.get("fork") and not r.get("archived")])
        if len(d) < 100:
            break
        page += 1
        time.sleep(0.5)
    return out[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--max-repos", type=int, default=50)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    repos = list_repos(a.owner, a.max_repos)
    print("public repos scanned: %d (owner %s)" % (len(repos), a.owner), flush=True)
    rows, finds = [], []
    for r in repos:
        full = r["full_name"]
        for t in TARGETS:
            url = "https://api.github.com/repos/%s/contents/%s" % (full, urllib.parse.quote(t))
            try:
                c, st = gh(url, ok404=True)
            except Exception as e:
                rows.append({"repo": full, "path": t, "status": "err:%s" % type(e).__name__})
                time.sleep(0.3)
                continue
            if st == 404 or not c:
                continue
            try:
                raw = base64.b64decode(c.get("content", "")).decode("utf-8", "replace")
            except Exception:
                continue
            sec = find_secrets(raw)
            rows.append({"repo": full, "path": t, "status": "present", "size": len(raw),
                         "secrets": sec})
            if sec:
                finds.append({"repo": full, "path": t, "types": [s["type"] for s in sec]})
            time.sleep(0.25)
    present = [r for r in rows if r.get("status") == "present"]
    summary = {"owner": a.owner, "repos_scanned": len(repos), "config_files_present": len(present),
               "files_with_secret": len(finds),
               "measured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for f in finds:
        print("  LEAK", f["repo"], f["path"], f["types"])
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data",
                                "owner-%s-%s.json" % (a.owner, time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump({"summary": summary, "rows": rows}, fh, ensure_ascii=False, indent=1)
    print("saved", os.path.abspath(out))


if __name__ == "__main__":
    main()

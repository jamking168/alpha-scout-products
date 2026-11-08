#!/usr/bin/env python3
"""Sample public .claude/settings.local.json files via GitHub API, measure plaintext-credential rate.
Secrets are NEVER printed: only counts and masked metadata."""
import json, os, re, time, urllib.request, base64

TOKEN = os.environ.get("GITHUB_TOKEN", "")
UA = "alpha-scout-research"
OUT = "/home/jam/.hermes/profiles/alpha-scout/cache/scratch/agentleak-20261108"
os.makedirs(OUT, exist_ok=True)


def gh(url):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


# 1) collect paths
items = []
for page in (1, 2):
    d = gh("https://api.github.com/search/code?q=filename%%3Asettings.local.json+path%%3A.claude&per_page=30&page=%d" % page)
    items.extend(d.get("items", []))
    print("page", page, "total_count", d.get("total_count"), "got", len(d.get("items", [])))
    time.sleep(7)

PATTERNS = [
    (r"PGPASSWORD\s*=\s*(\S+)", "PGPASSWORD"),
    (r"sshpass\s+-p\s*(\S+)", "sshpass"),
    (r"mysql\s+.*-p(\S{6,})", "mysql -p"),
    (r"curl\s+.*-u\s*\S+:(\S+)", "curl basic auth"),
    (r"AKIA[0-9A-Z]{16}", "AWS AK"),
    (r"sk-[A-Za-z0-9\-_]{20,}", "sk- key"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "github token"),
    (r"https?://[^/\s:]+:[^/\s@]+@", "creds-in-url"),
    (r"(password|passwd|pwd|secret|token|api[_-]?key)\"?\s*[:=]\s*\"?([A-Za-z0-9\-_\.]{8,})", "kv-secret"),
]

rows = []
for it in items:
    repo = it["repository"]["full_name"]
    path = it["path"]
    try:
        c = gh("https://api.github.com/repos/%s/contents/%s" % (repo, path))
        raw = base64.b64decode(c.get("content", "")).decode("utf-8", "replace")
    except Exception as e:
        rows.append({"repo": repo, "status": "fetch_fail:%s" % type(e).__name__})
        time.sleep(0.5)
        continue
    try:
        js = json.loads(raw)
    except Exception:
        rows.append({"repo": repo, "status": "not_json", "size": len(raw)})
        time.sleep(0.5)
        continue
    allow = js.get("permissions", {}).get("allow", []) or []
    allow += js.get("permissions", {}).get("deny", []) or []
    hits = []
    for rule in allow:
        s = rule if isinstance(rule, str) else json.dumps(rule)
        for pat, name in PATTERNS:
            if re.search(pat, s, re.I):
                hits.append(name)
                break
    rows.append({"repo": repo, "status": "ok", "rules": len(allow), "secret_hits": sorted(set(hits))})
    time.sleep(0.4)

ok = [r for r in rows if r["status"] == "ok"]
bad = [r for r in ok if r["secret_hits"]]
print("sampled_files", len(rows), "parsed_ok", len(ok), "with_plaintext_secret", len(bad),
      "rate=%.1f%%" % (100.0 * len(bad) / max(1, len(ok))))
from collections import Counter
print("hit_types", Counter([h for r in bad for h in r["secret_hits"]]))
print("repos_with_leak", len(set(r["repo"] for r in bad)))
print("sample_repos(no secrets shown):", [r["repo"] for r in bad][:8])
with open(os.path.join(OUT, "sample.json"), "w") as f:
    json.dump({"items": len(items), "parsed_ok": len(ok), "leak_files": len(bad), "rows": rows}, f, indent=1)
print("saved", OUT)

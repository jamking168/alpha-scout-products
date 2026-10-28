import json, time, urllib.request, os, re
from concurrent.futures import ThreadPoolExecutor

base = "/home/jam/.hermes/profiles/alpha-scout/cache/scratch/scan-1028/"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}


def get(url, tries=2):
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(0.4)
    raise last


items = []
for off in (0, 100):
    d = get("https://clawhub.ai/api/v1/skills?limit=100&offset=%d" % off)
    items.extend(d.get("items", []))
print("listing items:", len(items), flush=True)

n = min(120, len(items))


def work(it):
    slug = it.get("slug")
    try:
        d = get("https://clawhub.ai/api/v1/skills/%s" % slug)
        desc = (d.get("skill") or {}).get("description") or ""
        if not desc:
            return None
        return {
            "name": slug,
            "description": desc,
            "_meta": {"owner": (d.get("owner") or {}).get("handle"),
                      "downloads": (it.get("stats") or {}).get("downloads", 0),
                      "moderation": d.get("moderation")},
        }
    except Exception:
        return None


t0 = time.time()
with ThreadPoolExecutor(max_workers=8) as ex:
    res = list(ex.map(work, items[:n]))
tools = [r for r in res if r]
print("fetched:", len(tools), "of", n, "in %.1fs" % (time.time() - t0), flush=True)

out = base + "skills_tools.json"
with open(out, "w") as f:
    json.dump({"tools": tools}, f, ensure_ascii=False)
print("wrote", out, os.path.getsize(out), flush=True)

pats = {
    "curl_pipe_sh": re.compile(r"curl[^\n]{0,80}\|\s*(ba)?sh", re.I),
    "base64_exec": re.compile(r"base64[^\n]{0,40}(-d|-D|\|)", re.I),
    "invisible_unicode": re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]"),
    "ignore_prev": re.compile(r"ignore (all )?(previous|prior|above)", re.I),
    "no_tell_user": re.compile(r"(do not|don't) (tell|inform|mention) (the )?user", re.I),
    "install_prereq": re.compile(r"(npm i|pip install|brew install|curl -)", re.I),
}
hits = {k: [] for k in pats}
for t in tools:
    for k, p in pats.items():
        if p.search(t["description"]):
            hits[k].append(t["name"])
print("\nraw pattern hits over", len(tools), "live skills:")
for k, v in hits.items():
    print(" ", k, len(v), v[:6])

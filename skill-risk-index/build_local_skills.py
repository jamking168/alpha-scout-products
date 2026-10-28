import json, os, glob

out_path = "/home/jam/.hermes/profiles/alpha-scout/cache/scratch/scan-1028/local_skills_tools.json"
roots = ["/home/jam/.hermes/skills"] + glob.glob("/home/jam/.hermes/profiles/*/skills")
tools = []
seen = 0
for r in roots:
    for dirpath, dirs, files in os.walk(r):
        if "SKILL.md" not in files:
            continue
        p = os.path.join(dirpath, "SKILL.md")
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                txt = f.read(6000)
        except Exception:
            continue
        seen += 1
        tools.append({"name": p.replace("/home/jam/", "~/"), "description": txt})

with open(out_path, "w") as f:
    json.dump({"tools": tools}, f, ensure_ascii=False)
print("skill files:", seen, "bytes:", os.path.getsize(out_path))

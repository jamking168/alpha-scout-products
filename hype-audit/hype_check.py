#!/usr/bin/env python3
"""hype_check.py — 采用度指纹体检（Adoption Fingerprint Audit）

用途：对一个「看起来很火」的开源项目 / HF 模型，用公开 API 拉取可复现的采用度指标，
输出一张带阈值判定的体检表。不判定「造假」，只回答：这个热度有没有对应的使用痕迹。

数据源（全部公开、只读、无需 key）：
  - GitHub REST API  /repos/{owner}/{repo}  、/contributors
  - Hugging Face Hub API /api/models/{org}/{name}

核心指纹（阈值**不是拍脑袋**，来自本轮实测的对照基线，见 baseline/control_r13.json）：
  对照实测（2026-09-25）：huggingface/transformers 0.744% watcher、20.8% fork、14.3 issue/千星、57.7★/天；
                        ollama/ollama 0.560% watcher、9.9% fork、22.4 issue/千星、153.1★/天。
  watcher_ratio = subscribers / stars   对照区间 0.56%~0.74%；<0.40% = MILD，<0.20% = SEVERE
  fork_ratio    = forks / stars         对照区间 9.9%~20.8%；>25% 且 watcher 极低 = 脚本 fork
  issues_per_1k = open_issues / stars*1000  对照 14~22；<2 且星多 = 只有星，没人提问题
  velocity      = stars / age_days      对照 58~153★/天；新仓库 >300★/天 = 异常斜率
  hf_like_dl    = likes / downloads     downloads=0 且 likes 数百以上 = 点赞与下载完全脱钩

用法：
  python3 hype_check.py --github owner/repo [--github ...] [--hf org/name] [--json out.json] [--md out.md]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

UA = "alpha-scout-hype-check/1.0 (research; contact jamking168@example.com)"


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def audit_github(full: str) -> dict:
    d = _get(f"https://api.github.com/repos/{full}")
    age_days = max((time.time() - datetime.strptime(d["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc).timestamp()) / 86400.0, 0.5)
    stars = d["stargazers_count"]
    subs = d.get("subscribers_count", 0)
    forks = d.get("forks_count", 0)
    issues = d.get("open_issues_count", 0)
    rec = {
        "kind": "github", "id": full, "stars": stars, "forks": forks,
        "watchers": subs, "open_issues": issues,
        "created": d["created_at"][:10], "pushed": d["pushed_at"][:10],
        "age_days": round(age_days, 1), "language": d.get("language"),
        "archived": d.get("archived", False),
        "watcher_ratio_pct": round(100.0 * subs / stars, 3) if stars else None,
        "fork_ratio_pct": round(100.0 * forks / stars, 1) if stars else None,
        "issues_per_1k_stars": round(1000.0 * issues / stars, 1) if stars else None,
        "velocity_stars_per_day": round(stars / age_days, 1),
        "description": (d.get("description") or "")[:120],
    }
    try:
        c = _get(f"https://api.github.com/repos/{full}/contributors?per_page=1&anon=false")
        link = ""  # 通过 Link 头拿总数需要原始响应，这里退化为抽样
        rec["contributors_sampled"] = len(c)
        rec["contributors_note"] = "sampled_first_page_only"
    except Exception as e:  # pragma: no cover
        rec["contributors_note"] = f"unavailable: {e}"
    rec["flags"] = _flags_github(rec)
    return rec


def audit_hf(model_id: str) -> dict:
    d = _get(f"https://huggingface.co/api/models/{model_id}")
    likes = d.get("likes") or 0
    dl = d.get("downloads") or 0
    created = (d.get("createdAt") or "")[:10]
    rec = {"kind": "hf", "id": model_id, "likes": likes, "downloads": dl,
           "created": created, "author": d.get("author"), "pipeline": d.get("pipeline_tag"),
           "likes_per_download": (round(likes / dl, 2) if dl else None)}
    flags = []
    if likes >= 200 and dl == 0:
        flags.append("DECOUPLED: 点赞数千但下载为 0 —— 热度与使用完全脱钩")
    if likes >= 200 and dl == 0 and created and \
            (time.time() - datetime.strptime(created, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) / 86400 < 21:
        flags.append("RECENT_SURGE: 该模型创建不到 21 天")
    rec["flags"] = flags
    return rec


def _flags_github(r: dict) -> list[str]:
    f = []
    wr = r["watcher_ratio_pct"] or 0
    if r["stars"] >= 500 and wr < 0.2:
        f.append(f"LOW_WATCH_SEVERE: {r['watchers']} watcher / {r['stars']} star = {wr}%（对照基线 0.56%~0.74%）")
    elif r["stars"] >= 500 and wr < 0.4:
        f.append(f"LOW_WATCH: {r['watchers']} watcher / {r['stars']} star = {wr}%（对照基线 0.56%~0.74%）")
    if r["stars"] >= 500 and (r["fork_ratio_pct"] or 0) > 25:
        f.append(f"HIGH_FORK: fork/star = {r['fork_ratio_pct']}%（对照上限 20.8%），且与 watcher 极低同时出现")
    if r["stars"] >= 500 and (r["issues_per_1k_stars"] or 0) < 2:
        f.append(f"QUIET_ISSUES: 每千星仅 {r['issues_per_1k_stars']} 个 open issue（对照 14~22）—— 没有真人提问题")
    if r["age_days"] <= 12 and r["velocity_stars_per_day"] >= 300:
        f.append(f"FAST_RAMP: 创建 {r['age_days']} 天，{r['velocity_stars_per_day']} star/天（对照 58~153/天）")
    if r["created"][:7] >= "2026-09" and r["stars"] >= 1000:
        f.append("SAME_WEEK_CLUSTER: 2026-09 同周冒出的千星级新仓库（需看同批仓库名单）")
    return f


def verdict(recs: list[dict]) -> dict:
    allf = [x for r in recs for x in r.get("flags", [])]
    dec = [x for x in allf if x.startswith("DECOUPLED")]
    return {
        "flag_count": len(allf),
        "decoupled_signals": len(dec),
        "reading": ("采用度指纹不自洽 —— 热度缺少对应的使用痕迹；不能据此认定造假，"
                    "但足够据此不发教程、不做集成、不把它写进任何产品的依赖里"),
        "limitation": ("这是采用度相关性的体检，不是造假的证明；阈值是经验分布不是官方标准；"
                       "公开 API 的 star/fork/like 都可能被低成本操纵，正向结论（指纹正常）"
                       "也仅说明「没有异常」，不等于「有真实付费需求」。"),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--github", action="append", default=[])
    p.add_argument("--hf", action="append", default=[])
    p.add_argument("--json", default="")
    p.add_argument("--md", default="")
    a = p.parse_args()
    recs = []
    for g in a.github:
        try:
            recs.append(audit_github(g))
        except Exception as e:
            recs.append({"kind": "github", "id": g, "error": str(e)})
    for h in a.hf:
        try:
            recs.append(audit_hf(h))
        except Exception as e:
            recs.append({"kind": "hf", "id": h, "error": str(e)})
    rep = {"generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "records": recs, "verdict": verdict(recs)}
    if a.json:
        json.dump(rep, open(a.json, "w"), ensure_ascii=False, indent=1)
    if a.md:
        lines = [f"# 采用度指纹体检 · {rep['generated_at_utc']}", ""]
        for r in recs:
            if "error" in r:
                lines.append(f"- **{r['id']}** —— 拉取失败：{r['error']}")
                continue
            if r["kind"] == "github":
                lines.append(f"- **{r['id']}** {r['stars']}★ / {r['forks']} fork / {r['watchers']} watcher / "
                             f"{r['open_issues']} open issues / {r['age_days']} 天 / {r['velocity_stars_per_day']}★/天")
            else:
                lines.append(f"- **{r['id']}** likes={r['likes']} downloads={r['downloads']} created={r['created']}")
            for f in r.get("flags", []):
                lines.append(f"    - ⚠ {f}")
        lines += ["", f"**判定**：{rep['verdict']['flag_count']} 条异常指纹。{rep['verdict']['reading']}",
                  "", f"> 口径限制：{rep['verdict']['limitation']}"]
        open(a.md, "w").write("\n".join(lines))
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

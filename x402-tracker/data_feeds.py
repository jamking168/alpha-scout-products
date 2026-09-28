#!/usr/bin/env python3
"""免费机器可读数据端点 —— 千里眼情报管线对外暴露的数据资产。

为什么免费: 这些是**我们独有的观察**（全网唯一在追踪 x402 赛道的真实链上收入），
开放出来换「被发现」——人和 agent 都能引用、能核对、能转载。
付费产品（每日情报简报 / 回测可信性审计）见 /intel。

安全: 只读**写死的本地路径**，不拼接任何用户输入，不做 shell 调用。

署名: x402 数据源自 x402-list.com (CC BY 4.0)，转载须保留该署名。
"""
import json
import os
import sqlite3

PROJECTS = "/home/jam/projects"
X402_DIR = os.path.join(PROJECTS, "alpha-scout-products", "x402-tracker", "data")
DEFI_DB = os.path.join(PROJECTS, "defi-monitor", "state", "defi_monitor.db")
MACRO_JSON = os.path.join(PROJECTS, "fed-regime-monitor", "state", "latest_brief.json")

SOURCE_X402 = "x402-list.com (CC BY 4.0)"
X402_CAVEAT = ("口径: 该目录观测到的 facilitator 上**已结算 USDC**，是下限(floor)而非估计；"
               "status != measured 的 listing 记为「未测量」，不当作 0。以 Base/USDC 为主。")


# ---------- 读取工具 ----------

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return rows


# ---------- x402 赛道真实收入 ----------

def _x402_latest():
    rows = _read_jsonl(os.path.join(X402_DIR, "series.jsonl"))
    return rows[-1] if rows else None


def x402_revenue():
    """x402 赛道 30 天真实链上收入：市场总量、集中度、头部卖家。"""
    latest = _x402_latest()
    if not latest:
        return None
    return {
        "generated_at": latest.get("captured_at_utc"),
        "source": SOURCE_X402,
        "methodology_url": "https://web3project.top/research/x402",
        "caveat": X402_CAVEAT,
        "market": {
            "listed_total": latest.get("listed_total"),
            "traction_measured": latest.get("traction_measured"),
            "traction_unmeasured": latest.get("traction_unmeasured"),
            "services_paid_30d": latest.get("services_paid_30d"),
            "pct_paid_30d": latest.get("pct_paid_30d"),
            "volume_usd_30d": latest.get("market_volume_usd_30d"),
            "volume_usd_per_day": latest.get("market_volume_usd_per_day"),
            "tx_30d": latest.get("market_tx_30d"),
            "avg_settlement_usd": latest.get("avg_settlement_usd"),
            "concentration": {
                "top1_share_pct": latest.get("top1_share_pct"),
                "top3_share_pct": latest.get("top3_share_pct"),
                "top5_share_pct": latest.get("top5_share_pct"),
            },
            "price_median_usd": latest.get("min_price_median_usd"),
            "price_p10_usd": latest.get("min_price_p10_usd"),
            "price_max_usd": latest.get("min_price_max_usd"),
        },
        "top_earners": latest.get("top_earners", []),
    }


def x402_history():
    """时间序列（每天一行汇总）—— 用来观察赛道是在涨还是在退。"""
    rows = _read_jsonl(os.path.join(X402_DIR, "series.jsonl"))
    keys = ("captured_at_utc", "listed_total", "services_paid_30d", "pct_paid_30d",
            "market_volume_usd_30d", "market_volume_usd_per_day", "market_tx_30d",
            "avg_settlement_usd", "top1_share_pct", "top3_share_pct")
    return {
        "source": SOURCE_X402,
        "caveat": X402_CAVEAT,
        "points": [{k: r.get(k) for k in keys} for r in rows],
    }


# ---------- DeFi 风险事件 ----------

def defi_events(limit=100):
    """DeFi TVL 单日骤降事件 + 历史 hack 事件（事件驱动去重后的流）。"""
    out = {"tvl_drops": [], "hacks": []}
    if not os.path.exists(DEFI_DB):
        return out
    try:
        con = sqlite3.connect(f"file:{DEFI_DB}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        for r in con.execute(
                "SELECT slug, name, last_alert_pct, last_alert_ts, last_tvl "
                "FROM tvl_events ORDER BY last_alert_ts DESC LIMIT ?", (limit,)):
            out["tvl_drops"].append({
                "slug": r["slug"], "name": r["name"],
                "change_1d_pct": round(r["last_alert_pct"], 2) if r["last_alert_pct"] is not None else None,
                "tvl_usd": round(r["last_tvl"], 2) if r["last_tvl"] is not None else None,
                "alert_ts": r["last_alert_ts"],
            })
        for r in con.execute(
                "SELECT defillama_id, date, name, amount FROM seen_hacks "
                "ORDER BY date DESC LIMIT ?", (limit,)):
            out["hacks"].append({
                "id": r["defillama_id"], "date": r["date"],
                "name": r["name"],
                "amount_usd": round(r["amount"], 2) if r["amount"] is not None else None,
            })
        con.close()
    except Exception:
        pass
    out["caveat"] = ("事件驱动去重: TVL 骤降是持续性状态，首次跌破告警、再恶化 ≥10 个百分点才再报，"
                     "恢复后结束事件。阈值: TVL ≥ $10M 且 24h 跌幅 ≤ -15%。")
    return out


# ---------- 宏观 regime ----------

def macro_regime():
    """美联储政策路径 + 关键宏观序列（FRED 官方数据）。"""
    d = _read_json(MACRO_JSON)
    if not d:
        return None
    return {
        "generated_at": d.get("ts"),
        "source": "FRED (Federal Reserve Economic Data)",
        "series": d.get("data", {}),
        "brief": d.get("brief", ""),
    }


def x402_series_raw():
    """原始时间序列（JSONL 文本）。给想自己复算/复核的人。"""
    try:
        with open(os.path.join(X402_DIR, "series.jsonl"), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


# 可公开的源码白名单（只读固定文件，不拼接任何输入）
X402_PUBLIC_FILES = {
    "x402_revenue_track.py": os.path.join(PROJECTS, "alpha-scout-products",
                                          "x402-tracker", "x402_revenue_track.py"),
    "x402_integrity.py": os.path.join(PROJECTS, "alpha-scout-products",
                                      "x402-tracker", "x402_integrity.py"),
}


def x402_source(name):
    """抓取脚本源码，供复现。name 必须在白名单内。"""
    p = X402_PUBLIC_FILES.get(name)
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


# ---------- 索引 ----------

def api_index():
    """端点目录 —— 让 agent 一眼看懂这里有什么、怎么用、什么要钱。"""
    return {
        "name": "千里眼 Alpha-Scout 公开数据 API",
        "description": ("来自实时情报管线的免费机器可读数据。全部 JSON，无需注册、无需鉴权、"
                        "无需 API key。可用于研究、监测、二次开发。"),
        "license": "数据可自由使用，须保留各端点标注的 source 署名。",
        "free_endpoints": [
            {"path": "/api/x402/revenue", "format": "application/json",
             "desc": "x402 赛道 30 天真实链上收入：市场总量、集中度、头部卖家（含买家数）"},
            {"path": "/api/x402/history", "format": "application/json",
             "desc": "上述指标的时间序列，用于观察赛道涨退"},
            {"path": "/api/defi/events", "format": "application/json",
             "desc": "DeFi 协议 TVL 单日骤降事件 + 历史 hack 事件流"},
            {"path": "/api/macro/regime", "format": "application/json",
             "desc": "美联储政策路径与关键宏观序列（FRED 官方）"},
        ],
        "paid_endpoints": [
            {"path": "/a2a/v1", "method": "POST",
             "desc": ("付费情报服务：每日加密/投资情报简报（single $19 / monthly $129）。"
                      "JSON-RPC，method=sample 可免费领一份历史样报。"
                      "结算走 x402 扩展（TRON USDT, scheme=exact）。")},
        ],
        "human_readable": {
            "research": "https://web3project.top/research/x402",
            "intel_offering": "https://web3project.top/intel",
            "openapi": "https://web3project.top/openapi.json",
            "llms": "https://web3project.top/llms.txt",
        },
    }

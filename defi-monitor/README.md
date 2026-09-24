# DeFi 监控（defi-monitor）

DeFi 漏洞/异常监控 + RWA 板块资金流快照 —— 纯信息生意，不押任何币。告警推送 Telegram @news94168_bot。

> 原 `rwa-monitor` 项目已合并进本项目的 `--rwa` 模式（2026-09-23），一个 repo 两个模式。

## 模式一：异常告警（默认，每小时）

自动巡检 DeFiLlama，盯两类异常：

1. **TVL 单日骤降**：大协议（≥$10M）`change_1d ≤ -15%`。**事件驱动去重**：同一协议只在首次跌破阈值、或跌幅再恶化 ≥10 个百分点时告警，恢复到阈值以上即结束事件（避免持续性状态被每轮重复告警）
2. **新 hack 事件**：最近 7 天内新增的漏洞/攻击事件（去重，首次只初始化）

后续扩展：预言机喂价偏离、巨鲸大额转出（需链上 RPC）。

## 模式二：RWA 板块快照（`--rwa`，每日）

筛出全部 `category=RWA` 协议，拉头部协议 + 观察名单的历史 TVL，算 7/30 天资金变化，输出：

- RWA 总量 TVL、占全 DeFi 比例
- 7/30 天板块净变化 + 流入前三榜
- JSON 快照（`state/snapshots/`）+ 中文摘要 + 写情报库

## 数据源

DeFiLlama 免费 API（`api.llama.fi`），无需注册/付费：
- `/protocols` — 全部协议（TVL、change_1d、category、chain）
- `/hacks` — 历史 hack 事件（date、amount、technique、classification）
- `/protocol/{slug}` — 单协议历史 TVL 时序
- `/charts` — 全 DeFi 总 TVL 时序

## 用法

```bash
python3 defi_monitor.py                 # 默认：异常告警（TVL 骤降 + hack）
python3 defi_monitor.py --rwa           # RWA 板块资金流快照
python3 defi_monitor.py --rwa --top 50  # 自定义历史拉取深度
```

## 结构

- `defi_monitor.py` — 主脚本（两模式共用：抓数据 → 规则引擎 → 告警/快照 → 写情报库）
- `state/defi_monitor.db` — SQLite，存已见过 hack（去重）
- `state/snapshots/` — RWA 快照 JSON（带时间戳）

## 部署

| 模式 | cron | 节奏 | 入口 |
|---|---|---|---|
| 异常告警 | `e89fc9ddfb38` | every 60m | `defi_monitor.py` |
| RWA 快照 | （RWA资金流监控·每日快照） | 每天 9am | `defi_monitor.py --rwa`（wrapper `rwa_monitor_daily.sh`） |

- 告警：脚本内部调 `self_deliver.py --profile alpha-scout` 直发 Telegram
- 产出自动写入情报库（`intel_memo`），成为情报简报内容源

## 变现路径（GO 原文）

先免费运营积累订阅用户（协议方/大额玩家主动联系要告警），再开付费 tier（$19~99/月）。

需求真实：DeFi 异常监控价值被 $6 亿损失反复证明。

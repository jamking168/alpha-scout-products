# DeFi 监控（defi-monitor）· 已迁出本仓库（2026-11-19）

**这里不再放实现代码。生产代码在 `/home/jam/projects/defi-monitor/`（独立 git 仓库）。**

## 为什么

本目录曾有一份 502 行的旧拷贝，与生产文件（628 行）分叉 468 行，且**缺少** HTTP 重试、协议改名/合并白名单、canonical 聚合等 10-27/11-04 的修复。
2026-11-18 一次改动按需求单改到了这份旧拷贝上：功能在线上一个都没生效，还因为拿它跑了一次真实巡检、把新 schema 写进了生产状态库，差点让下一轮 cron 崩掉（已回滚，详见 `~/.hermes/reports/code-requests/2026-11-18-defi-monitor-REJECTED.md`）。
分叉本身就是事故根源，因此删掉这份拷贝，只留这份说明。

## 生产位置

```
/home/jam/projects/defi-monitor/defi_monitor.py     # 唯一实现（git 仓库，本地）
/home/jam/projects/defi-monitor/tests/              # 单测（迁移白名单 9 项 + 降噪 20 项）
/home/jam/projects/defi-monitor/state/defi_monitor.db  # 生产状态库
```

cron 链路：`e89fc9ddfb38`（every 60m）→ `~/.hermes/profiles/alpha-scout/scripts/defi_monitor.py`（8 行 wrapper）→ 上面那个生产文件。

## 功能（现状，2026-11-19 起）

1. **TVL 单日骤降**：TVL ≥ $50M 且 `change_1d ≤ -15%`，**连续两次巡检命中**才告警（单次命中多是数据源重算的假信号，如 11-18 linea-bridge 报 -73.7%、历史曲线仅 -5%）。跌幅 ≤ -60% 的行追加 `⚠️ 跌幅异常，先核对历史曲线`。
2. **新 hack 事件**：最近 7 天新增，去重，首次只初始化。
3. **合并成一条消息**：一次巡检的所有 TVL + hack 告警合成一条 Telegram 消息。
4. **RWA 板块快照**（`--rwa`，每日 9am）：RWA 总量/占比、7/30 天净变化与流入前三。
5. 验收用 `--dry-run`（走独立干跑库，生产库零写入）、`--fixture`、`--state-db`。

## 数据源

DeFiLlama 免费 API（`api.llama.fi`），无需注册/付费：`/protocols`、`/hacks`、`/protocol/{slug}`、`/charts`。

## 变现路径（GO 原文，未变）

先免费运营积累订阅用户（协议方/大额玩家主动联系要告警），再开付费 tier（$19~99/月）。
需求真实：DeFi 异常监控价值被数亿美元损失反复证明。

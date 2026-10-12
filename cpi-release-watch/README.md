# CPI 发布差异表（cpi-release-watch）

把「月度 CPI 发布」变成 30 秒内可读的三列对照：**实际 / 前值 / 共识（可选）**，含环比与同比。
用于宏观事件日的自动播报（自用信息源 → 内容 → 订阅 SKU 的候选）。

## 它做什么 / 不做什么

- 做：从 FRED 官方 API（序列原始来源是 BLS）拉 CPI 指数，按日期对齐算环比、同比，出表 + 可选落盘 JSON；`--watch` 可在发布窗口轮询，新月份一落地就出表。
- 不做：**不预测数字、不给方向性判断、不写"数据前布局"**；**不编造共识值** —— 共识不是官方数据，只从外部 JSON 读入，没给就打印「未提供」。

## 口径（与 BLS 发布一致，别搞混）

| 指标 | 取序列 | 为什么 |
|---|---|---|
| 环比 (MoM) | 季调 `CPIAUCSL` / `CPILFESL` | BLS 发布的环比是季调口径 |
| 同比 (YoY) | 未季调 `CPIAUCNS` / `CPILFENS` | BLS 发布的 12 个月变动是未季调口径 |

**踩过的坑（务必保留日期对齐）**：FRED/BLS 的 CPI 序列里 **2025-10 因政府拨款中断缺失**（footnote code `X`）。
如果按「列表第 13 个元素」取去年同期，基期会整体错位一个月 —— 实测会把 2026-08 总体同比从官方 **3.40%** 算成 **3.69%**。
本脚本一律按 `YYYY-MM-01` 日期匹配基期，缺失即报「未提供」，不用邻月顶替。

## 用法

```bash
cd /home/jam/projects/alpha-scout-products/cpi-release-watch
python3 cpi_release_watch.py                          # 出当前差异表
python3 cpi_release_watch.py --json readings.json     # 同时落盘 JSON（含原始读数）
python3 cpi_release_watch.py --consensus consensus.json
python3 cpi_release_watch.py --watch --max-minutes 15 # 发布窗口内轮询，检测到新月份即出表
```

FRED key 来源（按顺序）：环境变量 `FRED_API_KEY` → `/home/jam/hk-investment/system/.env` → `~/.hermes/shared/accounts.env`。脚本不打印 key。

`consensus.json`（键都可选）：

```json
{"headline_yoy": 3.7, "headline_mom": 0.3, "core_yoy": 3.1, "core_mom": 0.3, "source": "来源写这里"}
```

## 已知验证记录

- **2026-08 交叉核对（2026-10-13 实测）**：本脚本 总体同比 **+3.40%**、环比 **+0.40%**；
  BLS 公开 API `CUUR0000SA0` 334.980 ÷ 323.976（2025-08）= **3.397%** → 一致。
  核心同比 **+2.45%**（`CUUR0000SA0L1E` 338.041 ÷ 329.970 = 2.448%）→ 一致。
- `--watch` 超时分支、`--consensus` 分支、`--json` 落盘均已实跑通过（EXIT=0）。

## 下一步（未做，需排期）

- 发布日自动跑：本仓未挂 cron（改定时任务按既有规约需人工决定）。发布窗口到了手动跑一次，或由资讯扫描作业代跑。
- 共识值来源：目前无自动化来源，需人工填 `consensus.json` 或另接数据源；**在接上之前，表中共识一律显示「未提供」**。

---
本工具只做事实对照，不构成投资建议。

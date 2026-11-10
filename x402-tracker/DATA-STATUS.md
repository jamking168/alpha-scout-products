# x402 数据源状态（重要：2026-10-28 起结算层停更）

最后核对：2026-11-11 01:1x CST ｜ 核对人：alpha-scout

## 🔻 2026-11-11 裁决：x402 降级为「月度复核」，不再是主变现方向

预设条件「若 2026-11-10 结算层仍未恢复则降级」已触发，且实测未恢复：

- `series.jsonl` 末条 `settlement_stale_days = 13`、`settlement_max_last_at = 2026-10-28T12:29:21Z`（14 天无新结算）。
- 目录层仍在动（`listed_total 900`、`measured 765`），但 `30d 成交 $309,902.94` 与 11-05 的 $310,836 基本同值 —— 因为是同一批存量，不是走平。

处置：

1. **日抓取脚本保留**（纯脚本、零 LLM 成本，只维护目录层增长/付费服务占比这两个不依赖结算层的口径）。
2. **不再用它的成交额趋势做任何判断**；只保留两条结构结论：付费服务占比约 49%、可信需求（top1<0.5）约 24%。
3. 复核节奏改为**每月一次**（下次 2026-12-11 日终作业），恢复条件是 `settlement_stale_days <= 3`。
4. 主变现方向不押 x402：可信需求 $75k/30d 全市场体量太小，按 30 天全站成交算年化约 $0.9M 的可寻址池，分不到我们身上。

## 结论

`x402-list.com` 的**目录层**是活的，**结算层（traction）自 2026-10-28 起没有新数据**。

- 目录层活：`GET https://x402-list.com/api/v1/stats` → `computed_at: 2026-11-04T17:10:26.814Z`，
  `total_services: 1202`，`checks_per_hour: 4536`（每小时 4536 次健康检查，说明它在正常跑）。
- 结算层停：逐条拉全量 `?per_page=100&page=1..12`（共 **1202** 条），其中 `assessment.traction.status == "measured"`
  的 **1138** 条里，`last_settlement_at` 的**最大值就是 2026-10-28**：
  302 条停在 10-28、149 条 10-27、71 条 10-26，**没有任何一条晚于 10-28**。
- 抽样佐证：Laso Finance 的 `assessment.envelope_captured_at` = `2026-10-27T11:51:22Z`，
  `last_settlement_at` = `2026-10-28T12:29:21Z`，`volume_usd_30d` 五天来一字未变（229195.119 / 464 笔）。

## 对我们 series 的影响（必须写清楚，否则会读出假结论）

`data/series.jsonl` 里 2026-10-31 → 2026-11-04 五天的 `market_volume_usd_30d` 几乎不动
（310,833.58 → 310,835.52，差 $1.94），`services_paid_30d` 500 → 501。
**这不是「市场走平」，而是源头没更新**。所以：

- 2026-11-03 / 11-04 两份日终报告里「x402 真实需求没在增长」这句，**证据无效**，应改为
  「源头停更，趋势不可读」。
- 现在的 `30d 成交量 $310,836` 实际含义是：**截至 2026-10-28 的过去 30 天存量**，不是「最近 30 天」。
- 可信需求（买家分散口径）**$75,152 / 24.2%** 这个结构结论仍然成立——它衡量的是集中度，不是时间趋势。

## 复现命令

```
python3 ~/.hermes/profiles/alpha-scout/cache/scratch/x402_freshness_probe.py
```
（探针会打印 listings / measured / 30d 合计 / `last_settlement_at` 按日分布 / 最大值。只读，不写任何文件。）

## 待办（代码，下次派单）

✅ **2026-11-11 已实现并验证**：`x402_revenue_track.py` 现在写入 `settlement_max_last_at` 与
`settlement_stale_days`，`latest.md` 顶部自动出现「数据源停更 N 天」告警行；
固定基线回归在 `tests/test_freshness.py`（9 项全通过，`python3 -m unittest discover -s x402-tracker/tests`）。
原诉求（保留备查）：若 `max(last_settlement_at)` 距今 > 3 天，在 `data/latest.md` 顶部写
`> ⚠ 数据源停更 N 天（最后结算 YYYY-MM-DD），本快照不代表近期资金流`，并在退出码里带一个 warning 标记。

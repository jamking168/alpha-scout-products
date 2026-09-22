# RWA 链上资金流监控 (DeFiLlama)

免费公开数据、可 cron 化的 RWA 板块资金流监控。数据源为 DeFiLlama 免费 API,无需注册/付费。

## 文件
- `rwa_monitor.py` — 主脚本(标准库 urllib,无第三方依赖)
- `snapshots/` — 每次运行产出的结构化 JSON 快照(文件名带 UTC 时间戳)

## 用法
```bash
cd /home/jam/projects/rwa-monitor
python3 rwa_monitor.py                  # 默认: 拉 top 40 个 RWA 协议历史,算 7d/30d 变化
python3 rwa_monitor.py --top 60         # 加深历史覆盖
python3 rwa_monitor.py --window 7 30 90 # 自定义变化窗口
```

## cron 化(示例:每天 UTC 06:00 跑一次)
```cron
0 6 * * * cd /home/jam/projects/rwa-monitor && /usr/bin/python3 rwa_monitor.py --top 50 >> cron.log 2>&1
```

## 输出
1. 终端:一段中文摘要(总量 TVL、净流入/流出方向、涨跌榜)。
2. 快照 JSON:含 `rwa.total_tvl_usd`(板块总量)、`rwa.flow`(7d/30d 净流动)、
   每个协议的 `tvl_now` 与 `changes`(绝对/百分比变化)、未跟踪协议名单。

## 数据口径与注意
- **板块总量**:对 DeFiLlama `category == "RWA"` 的所有协议当前 TVL 求和。
  有 42 个 RWA 协议在 DeFiLlama 无 TVL(如 BlackRock BUIDL、Tether Gold、
  Paxos Gold、Centrifuge 旧入口),故该值是对真实链上 RWA 的**低估**。
- **净流动(7d/30d)**:对 TVL 排名靠前且带历史时序的协议,用「最新 TVL − N 天前 TVL」求和近似。
  默认覆盖约 98% 板块 TVL,不追涨跌幅 = 资金净流入/流出(近似,未剔除代币价格波动)。
- **TVL 变化 ≠ 纯资金流**:RWA 多为法币/国债/稳定币背书,价格效应小,
  但个别协议(如国债代币)仍可能受 NA V 影响。单日断崖(如某基金一日 -3 亿)需第二源核验是赎回还是重归类。
- 单一协议异常不视为板块信号;结论以多协议一致方向为准。

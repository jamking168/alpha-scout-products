# TVL 流 vs 价格拆解（v0.1）

把 DeFi 的 24h TVL 变化拆成「价格效应」与「真实进出流」，避免把行情下跌误读成资金撤离。

## 方法

同一 category 内协议的主要资产敞口相近，用**类目 1d 变化中位数**当作该类的价格基准；
协议自身 1d 变化与中位数之差 ≥ 阈值（默认 8pp）的，视为「价格解释不了」→ 真实流/事件候选。
稳定币总量是价格中性的，单列出来当真实流读数。

类目内部资产敞口并不一致，所以输出只能当**线索**，必须回协议官方公告 / 链上数据核验。

## 跑法

```
python3 flow_decomp.py                 # 默认 TVL>=50M、偏离>=8pp、输出到本目录
python3 flow_decomp.py --min-tvl 100000000 --dev 5 --out /tmp/tvl
```

- 数据源：DeFiLlama 免费公开 API（`api.llama.fi/protocols`、`stablecoins.llama.fi/stablecoins`），无需 key。
- 产出：`flow_decomp.json`（机器可读）+ `flow_decomp.md`（人读）；`run_<日期>.txt` 是当次跑出的快照，用于回溯。

## 已知局限

- 中位数基准在类目样本 <4 个时不可用（该协议会被跳过）。
- 单次抓取的是即时快照，没有历史序列；要做「连续偏离」判断需要自己按日累积 `run_*.txt`。

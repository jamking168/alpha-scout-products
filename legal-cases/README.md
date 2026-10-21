# legal-cases — 英国法院加密判例库（只读官方源）

用途：把英国国家档案馆 Find Case Law 里与加密资产相关的判决拉成结构化 JSONL，
供「资产追回 / 交易所责任 / 稳定币法律定性」这类问题做**可引用的原始素材**，
不做法律意见、不做解读。

## 数据来源与口径

- 唯一来源：`https://caselaw.nationalarchives.gov.uk` 的公开 Atom feed（官方，无需 key）。
- 字段全部来自 feed 原文：`citation`（中性引注）、`title`、`court`、`date`、`url`、`source`、
  `asset_type`（BTC/ETH/USDT/stablecoin/general/unknown，按标题与摘要正则粗分类，命中不到标 `unknown`，不猜）。
- 查询词表：bitcoin, cryptocurrency, cryptoasset, digital assets, stablecoin, tether, fraud crypto。
- 幂等：按中性引注去重，重复跑只追加新案。

## 用法

```bash
/usr/bin/python3 fetch_judgements.py                 # 默认词表
/usr/bin/python3 fetch_judgements.py --limit 50      # 每个词最多取多少条
/usr/bin/python3 fetch_judgements.py --queries bitcoin "digital assets"
```

输出：`judgements.jsonl`（一行一案）。

## 已知限制

- feed 不支持 `order=date`（实测会按最旧排序并把最新判例挤出首页），所以只靠 `per_page` 控条数，
  结果是**按相关度取样、不保证全集**——要全集得逐案抓页面，成本高，暂不做。
- 只覆盖英格兰与威尔士（Find Case Law 的范围），不含苏格兰、北爱、美国、新加坡、香港判例。
- `asset_type` 只是检索辅助标签，不是法律定性。

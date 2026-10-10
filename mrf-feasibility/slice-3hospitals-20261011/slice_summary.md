# MRF 比价切片 — 同一编码跨医院对照

数据来源: 各医院按 45 CFR 180.50 公布的 CMS 标准收费文件(version 3.0.0)。
价格口径: `minimum`/`maximum` 为该医院自报的最低/最高谈判价;付款方列取 `standard_charge_dollar`。

## 001 — HEART TRANSPLANT OR IMPLANT OF HEART ASSIST SYSTEM WITH MCC

| 医院 | min 谈判价 (USD) | max 谈判价 (USD) | 有金额付款方 | 现金价 | 标价 |
|---|---|---|---|---|---|
| NewYork-Presbyterian | 251298.25 | 2250992.88 | 40 | None | None |
| Stanford | 412866.98 | 412866.98 | 1 | None | None |
| UCSF | 425819.41 | 851638.82 | 12 | None | None |

- 最低谈判价跨院区间: **$251,298.25 – $425,819.41**（高低差 **1.69x**，绝对值差 $174,521.16）
- 最高谈判价跨院区间: **$412,866.98 – $2,250,992.88**（高低差 **5.45x**）

## 003 — ECMO OR TRACHEOSTOMY WITH MV GREATER THAN 96 HOURS OR PRINCIPAL DIAGNOSIS EXCEPT FACE MOUTH AND NECK WITH MAJOR O.R. PROCEDURES

| 医院 | min 谈判价 (USD) | max 谈判价 (USD) | 有金额付款方 | 现金价 | 标价 |
|---|---|---|---|---|---|
| NewYork-Presbyterian | 191305.58 | 1761713.18 | 40 | None | None |
| Stanford | 312808.58 | 312808.58 | 1 | None | None |
| UCSF | 322935.5 | 645871.0 | 12 | None | None |

- 最低谈判价跨院区间: **$191,305.58 – $322,935.50**（高低差 **1.69x**，绝对值差 $131,629.92）
- 最高谈判价跨院区间: **$312,808.58 – $1,761,713.18**（高低差 **5.63x**）

## 口径与局限(必须随切片一起交付,不许省)

1. 这是**医院自报的谈判价**,不是任何病人的实际账单;实际支付还取决于计划类型、免赔、共付。
2. 每家医院列出的付款方集合不同,「跨院同付款方」不可直接比;上表比的是各家自报的 min/max 包络。
3. 同一编码在同一家医院可能挂在**多条 standard_charges variant** 上(不同 billing_class/methodology),
   本表取「带金额付款方最多」的那条作为代表 —— 取第一条会解空(UCSF/Stanford 实测)。
4. 现金价(discounted_cash)与标价(gross_charge)在 inpatient MS-DRG 条目层**普遍缺失**,
   显示为空即该院此码未按 CMS 要求填这两个字段,本身就是可报告的合规缺口。

*本文为情报分析,不构成投资建议、不构成医疗或法律意见。*

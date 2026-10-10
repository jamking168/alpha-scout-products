# 「3 家医院 × 1 个码」比价切片 — 2026-10-11

这是 MRF 命题的**第一件可交付物**：不是爬虫、不是平台，是一张能直接摆在福利顾问/自保雇主面前问「这东西值多少钱」的对照表。

## 结论先行

- 同一个 MS-DRG，三家顶级医院自报的最低谈判价差 **1.69x**（$251,298 → $425,819，差 $174,521）；
  最高谈判价差 **5.45x**（$412,867 → $2,250,993）。
- 同一家医院内部、同一个码的价差更大（NYP：最低 $251,298 对最高 $2,250,993，**8.96x**；UCSF：2.0x）。
- 现金价 / 标价字段在 inpatient MS-DRG 条目层**三家全部缺失** —— 这是 CMS 明令要求披露的字段，
  缺失本身就是可对医院/对 CMS 说事的东西。

## 文件

| 文件 | 内容 |
|---|---|
| `slice_summary.md` | 汇总表：每码 × 每院的 min/max/付款方数/现金价/标价 + 跨院倍数 |
| `slice_detail.csv` | 明细 106 行：hospital, code, payer, plan, price_usd（可直接进 Excel/BI） |

## 数据来源与时间戳(不许省)

| 医院 | MRF 直链来源 | 文件体积 | last_updated_on |
|---|---|---|---|
| NewYork-Presbyterian（7 院区合一份） | `https://www.nyp.org/cms-hpt.txt` → nyp.widen.net `.json.zip` | zip 4.0 MB / 解压 **229.1 MB** | 2026-03-31 |
| UCSF Medical Center（4 院区） | `https://www.ucsfhealth.org/cms-hpt.txt` → Box 分享链 | **91.0 MB** | 2026-04-01 |
| Stanford Health Care | `https://stanfordhealthcare.org/cms-hpt.txt` → 自有 DAM | **154.6 MB** | 2026-04-01 |

取数时间：2026-10-11 01:15–01:30 CST（本地下载后离线解析，未落库）。

## 复现命令

```bash
# 1) 发现直链（CMS 强制根文件）
curl -s https://www.nyp.org/cms-hpt.txt

# 2) 取文件 —— 必须带 --max-filesize 兜底（部分 CDN 忽略 Range，见下）
curl -sL --max-time 600 --max-filesize 250000000 -o stanford.json '<mrf-url>'

# 3) 按码抽切片（扫全文件 + 遍历所有 standard_charges variant）
python3 mrf_slice.py stanford.json --code 001 --code 003 --code-type MS-DRG --out stanford_slice.jsonl

# 4) 合成比价表
python3 compare_mrf_slice.py --out-dir slice-3hospitals-20261011 nyp_slice.jsonl=NewYork-Presbyterian ucsf_slice.jsonl=UCSF stanford_slice.jsonl=Stanford
```

## 今晚新踩的坑（都写进代码了）

1. **同一个码 = 多条 `standard_charges` variant，取第一条会解空。**
   UCSF 与 Stanford 的 MS-DRG 001 都挂在两条 variant 上：第一条 `methodology: other`（给的是算法文本，无金额），
   第二条才有 `minimum`/`maximum` + 逐付款方 `standard_charge_dollar`。第一版切片器取 `standard_charges[0]`，
   两家都解出 `None` —— 差点得出「UCSF/Stanford 不填价格」的错误结论。
   修法：遍历**全部** variant，同一码取「带金额付款方最多」的那条作代表（`mrf_slice.py` / `compare_mrf_slice.py`）。
2. **体积量级被实测坐实。** Northwestern Memorial（nm.org）单个 MRF 的 `Content-Length` = **5,019,534,101 B ≈ 5.0 GB**；
   NYU Langone Tisch 是 **482 MB 的 CSV**。全美 6,000+ 家按此量级 ≈ TB 级，**全量镜像必白干**，只能按需抽码。
3. **Range 支持与速度天差地别。**
   - 支持 Range（206）：Northwestern（nm.org，0.8 MB/s）、NYU（S3，8.6 MB/s）、Box（UCSF，快）
   - 忽略 Range：Cedars-Sinai 的 CDN（要 120KB 会给整包 716MB）
   → 一律 `--max-filesize` + `--max-time` 双兜底；能整包就别赌 Range。
4. **`cms-hpt.txt` 可达性实测（10 家）**：NYP / UCSF / Stanford / NYU / Northwestern / Rush = **200 脚本直达**；
   Mayo 403、Mount Sinai 403（Akamai）、Johns Hopkins Cloudflare 挑战、UCLA 人机验证。**约 6/10 纯脚本可拿链**，
   其余走真实浏览器（本机具备 CDP）。

## 下一步（商业侧，未验证仍是未验证）

这张表证明的是「**技术能出东西**」，不是「有人付钱」。要验的需求侧只有一条路：
拿 `slice_summary.md` + `slice_detail.csv` 去问 **self-insured employer（自保雇主）/ benefits consultant（福利顾问）**
——他们的痛点正是「同一个手术在不同医院差几十万美元，但我们没有现成对照表」。
付费信号出现前，**不做任何规模化爬取**。

---

*本文为情报分析，不构成投资建议，也不构成医疗或法律意见。数据来自医院依法公布的公开文件（45 CFR 180.50）。*

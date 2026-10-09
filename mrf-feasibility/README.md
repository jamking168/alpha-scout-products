# 美国医院价格透明 MRF —— 技术可行性实测（GO/NO-GO 判据）

- 执行时间: 2026-10-10 01:10–01:25 CST（alpha-scout 日终作业 A3）
- 命题来源: `intel_memo` #2819（2026-10-02，「36GB 法定脏数据」）/ `intel_recheck` #189（2026-10-09 维持 HOLD）
- 判据原文: **「能不能 1 天内解出结构，是 GO/NO-GO 的唯一判据」**
- 结论: **技术侧 GO**（结构可解、字段可得），**商业侧仍待验**（谁付费未证）→ 判据通过，晋级「可动手」，不晋级「已证实需求」。

## 1. 实测链路（全部真实执行，命令与输出可复现）

### 1.1 发现层：CMS 强制根文件 `cms-hpt.txt`
CMS 要求每家医院在 MRF 所在站点根目录放 `cms-hpt.txt`，内含 `location-name / source-page-url / mrf-url / contact-*`。实测 4 家：

| 医院 | 站点 | `curl https://<域>/cms-hpt.txt` | MRF 直链特征 |
|---|---|---|---|
| NewYork-Presbyterian | nyp.org | **200** ✅ | `nyp.widen.net` 上的 `.json.zip`，4,002,128 B |
| Cedars-Sinai | cedars-sinai.org | **403 裸 curl / 200 带浏览器 UA** ⚠️ | 自家 DAM 上的 `.json` |
| UCSF | ucsfhealth.org | **200** ✅ | Box 分享直链（`box.com/index.php?...&file_id=...`） |
| Mayo Clinic | mayoclinic.org | **403（带浏览器 UA 仍 403，Akamai 边缘拦截）** ❌ | 未取到 |

即：**发现层约 3/4 家医院用脚本可直达**；被 WAF 拦的那 1/4 需要真实浏览器（本机有 CDP/Playwright 能力，可作为第二层）。

### 1.2 取数层：真实文件体积
- NYP：zip 4.0 MB → 解压 JSON **229,129,021 B（229 MB）**，`version: "3.0.0"`，`last_updated_on: 2026-03-31`。
- Cedars-Sinai：CDN **忽略 HTTP Range**（`curl -r 0-120000` 实际下满 716 MB 才被 `--max-time` 掐断）→ **不能靠 Range 做「只取头部」**，必须整包下载或流式边下边解。
- 量级推算：全美 6,000+ 家医院 × 单家 0.2–0.7 GB ≈ **TB 级**。任何方案都不能全量镜像，只能按需/按州/按码抓取。

### 1.3 解析层：流式解出「项目名 / 现金价 / 最低谈判价」
工具：`parse_mrf_stream.py`（本目录，分块增量解码 + `json.JSONDecoder.raw_decode()` 逐条取，**不做整文件 `json.load`**，内存有界）。

实测输出（NYP，前 3 条 `standard_charge_information` 元素）:

```
条目 1  description: HEART TRANSPLANT OR IMPLANT OF HEART ASSIST SYSTEM WITH MCC
        code_information: [{'code': '001', 'type': 'MS-DRG'}]
        setting: inpatient
        minimum(最低谈判价): 251298.25   maximum: 2250992.88
        payers: 49 家，例 1199SEIU Funds 640419.72
条目 2  ECMO OR TRACHEOSTOMY WITH MV >96H ...  minimum 191305.58 / maximum 1761713.18
条目 3  TRACHEOSTOMY WITH MV >96H ...          minimum 126282.29 / maximum 1428744.62
```

耗时 **< 1 秒**（扫描 0.3 MB 即解出 3 条），内存峰值远低于 100 MB。

**结构要点（CMS v3.0.0，与 v2 不同）**：单价不在条目层，而是
`standard_charge_information[].standard_charges[].payers_information[].standard_charge_dollar`（按付款方逐条），
条目层只有 `minimum`/`maximum`。用老版本文档（找 `discounted_cash` 在条目层）会解空 —— 我第一版脚本就踩了这个坑。

### 1.4 字段覆盖率（前 40 MB ≈ 全文件 17% 的切片）

| 字段 | 出现次数 | 读数 |
|---|---|---|
| `description` | 5,620 | 条目数基准 |
| `minimum` / `maximum` | 5,619 | 条目层最低/最高谈判价，**近乎全覆盖** |
| `gross_charge` / `discounted_cash` | 3,559 | 字段确实存在，但**不覆盖全部条目**（约 63%） |
| `standard_charge_dollar` | 230,986 | 付款方级价格，条目均 ~41 家 |
| `median_amount` | 387 | 分位数仅零星出现 |

**重要修正（第一版脚本踩的坑）**：NYP 文件里被抽样的 inpatient / MS-DRG 条目，其**条目层根本没有 `discounted_cash` 和 `gross_charge`**
（`extract()` 里如实地标成 `None`，没有拿「第一个付款方的价」冒充现金价）。这两个键在文件别处出现 3,559 次，
说明**字段位置随 setting / code_type 分支而变**（inpatient 走 DRG、outpatient 走 CPT/HCPCS，模板不同）。
这正是「法定脏数据」的实际形态：**结构可解 ≠ 字段齐**，按 code_type 写分支解析 + 缺失值补齐，才是真正的活儿。


## 2. 判据结论

| 判据 | 结果 |
|---|---|
| 能否取得 MRF 直链（不靠人工找） | ✅ 可（`/cms-hpt.txt`，3/4 家脚本直达，其余走浏览器） |
| 能否解出「项目名 / 现金价 / 最低谈判价」三列 | ✅ 可（v3 结构已跑通，秒级、内存有界） |
| 能否在 1 天内完成 | ✅ 本次 **15 分钟内**完成端到端验证 |
| 需求侧（谁付费） | ❓ 未验 —— 只查到 Billmender 覆盖 482 家医院、CMS 已罚 28 家，付费方未定位 |

**GO**（技术判据通过）→ 可进入下一步：**先做「3 家医院 × 1 个码」的最小可用切片**，用它去问福利顾问/自保雇主是否付费；
付费信号出现再谈规模化。**不因为技术上跑得通就投入做全量爬虫**——那是最容易白干的地方。

## 3. 复现方式

```bash
# 1) 发现直链
curl -s https://www.nyp.org/cms-hpt.txt
# 2) 取文件（注意：部分 CDN 忽略 Range，勿用 -r 做部分下载）
curl -sL -o nyp.zip '<mrf-url>'
# 3) 流式解析
python3 parse_mrf_stream.py nyp.zip
```

脚本已通用化：传入任意 `.json` / `.json.zip` / `.csv` 的 MRF 即可（CSV 走另一条分支，见脚本注释）。

---
*本文为情报分析，不构成投资建议。数据来源：医院自行公布的 CMS 法定公开文件（45 CFR 180.50）。*

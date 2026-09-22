# FOMC 决议即时 diff 告警器

在 FOMC 决议发布后,自动产出中文摘要:
1. **决议声明措辞变化** (statement diff) — 美联储官网 press release,逐段对比前次声明
2. **点阵图中位数变化** (SEP dot plot) — 美联储官网 fomcprojtabl 页,解析基金利率中位数 vs 上次(June)
3. **联邦基金目标利率区间变化** — FRED API (DFEDTARU / DFEDTARL)
4. **市场隐含概率跳变** — CME FedWatch(公开页,本 VPS 可能被地域/IP 封锁)

全部免费公开数据,无需付费账号。

## 运行

```bash
# 前置: FRED_API_KEY 已放在 /home/jam/hk-investment/system/.env(变量名 FRED_API_KEY)
cd /home/jam/projects/fomc-diff

# 自动定位最近一次已发布的 FOMC 决议 + 其上一次
python3 fomc_diff.py

# 指定决议对
python3 fomc_diff.py --meeting 2026-09-16 --prev 2026-07-29

# 自定义 .env 路径 / 跳过 FedWatch
python3 fomc_diff.py --fred-key-file /path/.env --no-fedwatch
```

输出: 中文摘要(标准输出) + `output/fomc_diff_<日期>.json`。

## 关键实现细节 / 陷阱

- **FRED 必须用非浏览器 UA**: `curl/8.5.0` 这类 UA,stlouisfed 的 WAF 拒绝浏览器 UA。
- **密钥不打印不落盘**: 只从 .env 读,输出 JSON 中不含 key。
- **声明 URL 探测**: `monetary<YYYYMMDD>a/b/c.htm` 逐后缀探测;正文抽取后过滤日期/标题/联系方式等 boilerplate,再行级 diff。
- **点阵图解析**: SEP 页是「Variable | Median | Central Tendency | Range」多级表头;年份表头行(含 "Longer run")与 "Federal funds rate" 主行 + 下一行 "June projection" 对齐取中位数。

## 数据时效性(决定「5 分钟」能不能成立)

| 数据 | 决议后可用性 | 说明 |
|---|---|---|
| 声明 statement | ✅ 2:00 p.m. ET 即时 | 决议动作、投票、措辞都在声明里 |
| 点阵图 SEP | ✅ 与声明同发 | 仅 Mar/Jun/Sep/Dec 会议有 |
| CME FedWatch | ✅ 实时 | 但本 VPS 抓不到,需出口 IP/代理 |
| FRED DFEDTAR | ❌ 滞后(日频,次日生效) | 决议动作应直接读声明,FRED 只做校验/回填 |

> 结论:「5 分钟内」真正能拿到的是 statement + SEP + FedWatch;FRED 利率区间是**事后校验源**,不能当 5 分钟触发源。脚本里 FRED 的 `change_date` 是生效日(如 9/17),晚于决议日(9/16)属正常。

## 真实 demo 输出(2026-09-16 决议)

```
【1】联邦基金目标利率区间 (FRED, A级)
  前值: 3.5-3.75%
  现值: 3.75-4%  (↑ 25.0bp)    生效日期: 2026-09-17

【2】决议声明措辞变化
  动作: maintain 3-1/2 to 3-3/4 → raise by 1/4 to 3-3/4 to 4 percent
  投票: 9–3 → 12–0(三名此前反对维持、主张加息的委员 Hammack/Kashkari/Logan 转为赞成,反对段删除)
  经济: "despite ... conflict in the Middle East" → "While ... geopolitical developments,
        domestic spending has been resilient"(新增「内需有韧性」)
  通胀: "remains elevated relative to 2% goal, ... supply shocks ... energy"
        → "remains elevated. Today's policy action will support a timelier return to 2% goal"
        (删除「供给冲击/能源」解释,新增「更快回归目标」的鹰派前瞻)

【3】点阵图中位数 (SEP)
  2026: 3.8 → 4.1 (+30bp)    2027: 3.6 → 4.1 (+50bp)
  2028: 3.4 → 3.9 (+50bp)    Longer run: 3.1 → 3.2 (+10bp)

【4】市场隐含概率 (CME FedWatch)
  本 VPS 不可达;第三方报道会前 +25bp 概率约 86–93%(kucoin 86% / wealthiertoday 92% / cfostimes 93%),
  决议落地后 9 月会议 +25bp 归 100%,注意力转向 10/27–28 下次会议。(待接入 CME 主站验证)
```

## 落地结论(付费路径)

**能不能落地**: 技术上能,数据已验证跑通、可复算;但「免费替代品丰富」是最大风险。

- **真正的价值 ≠ 新闻**: 决议本身财联社/金十几分钟内免费发。差异化在**机器逐句措辞 diff(人做不到)+ 点阵图中位数量化 + 可编程告警**,给量化/主观交易者当**信号输入**,不是给散户当新闻。
- **最现实付费路径**: API/告警订阅制。决议前后各 5 分钟高频抓取 → 结构化 JSON + 中文摘要 → Telegram bot / webhook 推送,年订阅卖给量化开发者、主观交易者、财经自媒体工作室(99–299 刀/年)。B2B2C 可选: 把「措辞 diff」做白标组件卖给财经资讯 App / 券商投研,按调用量。
- **验证付费意愿的 MVP**: 先开免费 Telegram 频道,决议日半自动发一份中文摘要,看真实订阅/转发/提问量;有留存再上付费层(深度版)。
- **定价锚点**: CME FedWatch 数据免费但难读;中文解读付费意愿低;「自动 diff + 点阵图量化 + 可编程告警」的**工程化价值**才是付费点。

**MVP 还要补**:
1. CME FedWatch 数据通道(用美国/新加坡出口 IP 修通 Clash 代理,或第三方镜像/官方许可)拿到会前→会后隐含概率跳变。
2. 声明措辞**语义方向**自动判断(对 diff 结果打「偏鹰/偏鸽/中性」标签,可用 LLM)。
3. 点阵图**逐委员点**解析(中位数上移的驱动力),不止中位数。
4. 推送通道 + FOMC 日历定时触发(决议日 14:00 ET 后轮询声明 URL 直至出现)。
5. 历史回测与误报治理(过去 8 次会议验证「措辞变化 → 利率路径预期」相关性)。
6. 合规标注: 信息工具,非投资建议;中文区用户利率交易涉跨境合规,交付物写清边界。

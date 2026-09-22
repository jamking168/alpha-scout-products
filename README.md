# Alpha-Scout 情报变现产品集

> 千里眼（alpha-scout）主线：**从资讯发现商机 → 做工具 → 发钩子 → 主动推销**。
> 本仓库是情报库中 **GO 级商机落地后的可运行产物**，按「信息/自动化业务优先」原则筛选，
> 全部为真实跑通的代码，非 demo 占位。

---

## 一、项目介绍

情报扫描每天从 web3 / 美股 / 外汇 / AI 前沿信源采集、去重、分级（GO / HOLD / PASS）。
对升级为 **GO** 的商机，按「能否用程序员 + 海外 VPS + agent 集群直接落地赚钱」标准二次筛选，
落地为四个可运行产品：

| 子项目 | 定位 | 状态 |
|---|---|---|
| `agent-commerce/` | AI 代理支付（x402/A2A）：让 agent 集群变成「可被其他 agent 付费调用」的服务 | ✅ 已上线（独立地址架构） |
| `rwa-monitor/` | 印度/主权 RWA 链上资金流监控：DeFiLlama 一手数据，每日快照 | ✅ 已上线（cron 每日跑） |
| `fomc-diff/` | FOMC 决议即时 diff 信息产品：声明措辞 + 点阵图量化 | ✅ demo 跑通（1 硬依赖待解） |
| `polymarket-jev/` | Polymarket 决议滞后扫描器（Jev 模型）：找价格未追上已发布事实的市场 | ✅ fork 落地（edge 待验证） |

四者共享同一**收款底座**：TRON USDT（TRC-20），**每订单独立 HD 派生子地址**（m/44'/195'/0'/0/N，无尾数匹配，付款归属无歧义）。

---

## 二、仓库结构

```
alpha-scout-products/
├── README.md                    # 本文件（项目总结）
├── .gitignore
├── agent-commerce/              # ① AI 代理支付
│   ├── a2a_server.py            #   A2A Merchant 服务（x402 付费）
│   ├── hd_wallet.py             #   HD 钱包派生（bip_utils + tronpy，m/44'/195'/0'/0/N）
│   ├── sweep.py                 #   归集脚本（子地址 USDT → 主地址）
│   ├── reconcile.py             #   孤儿付款对账（链上事件驱动兜底）
│   ├── agent-card.json          #   A2A Agent Card（发现入口）
│   ├── 调研简报.md               #   三协议调研（x402/ACP/TAP）与落地判断
│   └── references/              #   一手规范证据（A2A-x402 扩展等）
├── rwa-monitor/                 # ② RWA 资金流监控
│   ├── rwa_monitor.py           #   监控脚本（DeFiLlama 免费 API）
│   ├── README.md                #   用法 + cron 示例 + 数据口径
│   └── snapshots/               #   结构化快照（179 协议 7d/30d 变化）
└── fomc-diff/                   # ③ FOMC 决议 diff
    ├── fomc_diff.py             #   FRED + statement + 点阵图 + FedWatch 四路抓取
    ├── README.md                #   用法 + 陷阱 + 付费结论
    ├── .env.example             #   密钥模板（真实密钥不提交）
    └── output/                  #   9/16 决议真实 demo 输出
```

---

## 三、盈利模型

### ① agent-commerce —— 供给侧卡位，按次收费
- **收入来源**：任何 agent（或人类）通过标准 A2A 协议发现并调用你的能力，**按次付 USDT**。
- **定价**：首个付费技能「每日情报简报」$5/次；后续可挂「回测审计」「尽调」「竞品监控」等分层技能。
- **收款**：TRON USDT，每订单独立 HD 派生子地址（无尾数匹配，付款归属无歧义；归集脚本批量回收）。
- **分发**：已登记 A2A 官方社区注册表 a2a-registry.org，可被其他 agent 搜到。
- **本质**：不是等买家来 landing 下单，而是进入「AI agent 经济」的供给侧。

### ② rwa-monitor —— 数据产品 / 信号源
- **收入来源**：链上资金流异动 → 情报简报付费订阅 / 交易信号输入（不构成投资建议）。
- **价值**：DeFiLlama 一手 TVL 数据 + 板块级净流动量化，替代「传闻」为「可验证事实」。
- **成本**：零（免费 API），纯自动化，每日 cron。

### ③ fomc-diff —— 信息产品，差异化在机器 diff
- **收入来源**：API/告警订阅制（Telegram bot / webhook 推送，$99–299/年），卖量化开发者 / 主观交易者 / 财经自媒体。
- **差异化**：机器逐句措辞 diff + 点阵图中位数量化 + 可编程告警，是「信号输入」不是「新闻转发」。
- **风险**：免费替代品（财联社等）丰富，必须靠「逐句 diff + 量化 + 可编程」的机器能力差异化。

### ④ polymarket-jev —— 决议滞后信号层
- **来源**：fork `swang666/polymarket-jev`（145 测试全过），用 TypeSafe 的 Jev 模型做「文本阅读理解」，找「已发布事实已满足决议规则、但价格没追上」的市场。
- **收入路径**：决议滞后信号订阅 / 喂给 PMBot（韦小宝）做下单执行（alpha-scout 做信号，韦小宝做执行）。
- **诚实边界**：作者用数据证明 forecast 模式（让 AI 猜价格）塌缩到 base rate，是死路；resolution-lag 模式「几乎拒绝所有市场」，edge 尚未被证明。它不是「马上赚钱的机器」，是「想清楚了 Jev 边界的信号层框架」。
- **下一步**：申请 Jev key（或验证 `pip install laya` 本地免费替代）→ `--scan` + `--backtest` 验证 Brier gap 是否明显为负 → 有 edge 再谈钱。

---

## 四、当前进度（2026-09-22 更新：经双模型审查，P0 已修复）

> ⚠️ 本产品集于 2026-09-22 经 GPT-6-astra + Claude Opus 5 双模型独立审查，发现多处致命缺陷。
> P0（错账 / 白嫖 / 宕机 / 错误信号）已全部修复；P1（稳定性 / 合规 / 商业化）待做。
> 当前定位：**通过代码层修复的产品原型，尚未经真实收款验收，暂不对公众开放收款。**

### ① agent-commerce —— ✅ 独立地址架构落地，待真实收款验收
- A2A 服务已上线（systemd `a2a-agent.service` 常驻，端口 8898，venv 依赖 + EnvironmentFile 注入 mnemonic）。
- 公网可发现：`https://web3project.top/.well-known/agent-card.json`；调用端点 `/a2a/v1`。
- **架构升级（2026-09-22）**：共享地址+尾数匹配 → **每订单独立 HD 派生子地址**（m/44'/195'/0'/0/N，index 0 = 归集主地址）。归属歧义从根删除，配归集脚本（sweep.py）+ 孤儿对账（reconcile.py，每 5 分钟 cron）。
- **已修复 P0（四轮双模型审查）**：支付字段位置、毫秒时间戳、整数金额、tx_hash 唯一约束+原子认领、高熵 ID+幂等、交付固化+失败不结单、JSON-RPC envelope、请求校验+限流、SQLite WAL、task_id 取货语义（P0-A）、原子开单（P0-1）、认领进写事务（P0-C）。
- **待验收**：真实 USDT 到账→自动交付（尚未用真钱跑过）。

### ② rwa-monitor —— ✅ 口径已修正，cron 每日跑
- 已把误导性的「净流入/流出」改为准确的「TVL 变化」，并加免责声明（TVL 变化 ≠ 资金流）。
- 快照（2026-09-22）：RWA TVL **$5.80B**（占 DeFi 3.11%），30 天 +$42M、7 天 -$219M（均为 TVL 变化，非资金流）。
- cron `d91d921643b6` 每天 9:00 自动跑。

### ③ fomc-diff —— ✅ P0 已修复，仍需解 CME + 语义打标
- **已修复 P0**：FRED「最近一次变化」不再伪装成「本次会议决议」（标注为"FRED 观测，非本次会议专属"）；SEP 点阵图标注「解析待核验」；load_fred_key 回退环境变量 bug。
- demo（9/16）：声明 diff 可用（maintain→raise、投票 9-3→12-0）。
- **待做**：CME FedWatch 地域封锁（且当前代码只测可达性，未实现概率解析）、SEP 表头定位需人工核对。

### ④ polymarket-jev —— ✅ fork 落地，edge 待验证
- fork 到 `github.com/jamking168/polymarket-jev`（145 测试全过、demo + live 数据跑通）。
- 定位：决议滞后信号层，归 alpha-scout；有 edge 后信号交给韦小宝 PMBot 执行。
- **待做**：申请 Jev key（或验证 laya 本地替代）→ `--scan` + `--backtest` 验证 Brier gap。

### 审查与修复记录（2026-09-22）

双模型独立审查，共识结论：**初版不能对外收款**。核心 P0 及修复状态：

| 严重度 | 问题 | 状态 |
|---|---|---|
| P0 | 支付字段位置错（to/value 在顶层非 token_info）→ 付款验证 100% 失效 | ✅ 已修 |
| P0 | 尾数 99 槽位永不释放 + 死循环 → 远程 DoS | ✅ 已修 |
| P0 | tx_hash 无唯一约束 → 一笔转账解锁多订单 | ✅ 已修 |
| P0 | 无所有权校验 → 白嫖 | ✅ 已修 |
| P0 | 交付占位内容 → 收钱发白纸 | ✅ 已修 |
| P0 | RWA「净流入」= TVL 变化 ≠ 资金流 → 错误信号 | ✅ 已修（改名） |
| P0 | FOMC「最近一次变动」误标为「本次会议决议」 | ✅ 已修（诚实标注） |

完整审查报告：`state/code_review/`（gpt-6-astra.md / claude-opus-5.md）。

---

## 五、后续计划

### ① agent-commerce
- [ ] **认领目录**：用 GitHub 账号登录 a2a-registry.org 认领（当前 unclaimed，不影响调用）。
- [ ] **更多付费技能**：把「回测审计」「尽调」「竞品监控」等现成能力挂成 A2A 技能，分层定价。
- [ ] **主动获客**：向 A2A 生态的开发者/项目方外联，让 agent 被真实调用起来。
- [ ] **TRON facilitator 完整化**：当前是自定义 scheme，可评估接入官方 facilitator 网络。

### ② rwa-monitor
- [ ] **异动告警**：Anemoy 是否企稳、流入阵营是否延续 → 升级为更强信号。
- [ ] **第二源核验**：Anemoy 单日 -$300M 是机构赎回还是 DeFiLlama 重归类，需官方公告/Dune 交叉。

### ③ fomc-diff
- [ ] **解 CME 依赖**（上线前必须）：美国/新加坡出口 IP 或代理。
- [ ] **语义打标**：声明措辞「鹰/鸽」自动打标。
- [ ] **推送通道**：Telegram bot / webhook + FOMC 日历定时触发。
- [ ] **历史回测**：误报治理 + 合规标注（非投资建议）。

### ④ polymarket-jev
- [ ] **申请 Jev key**（或验证 laya 本地替代），跑通 `--scan` + `--backtest`。
- [ ] **验证 edge**：Brier gap 明显为负跨几十个决议才算有真实 edge，否则诚实 PASS。
- [ ] **信号交接**：有 edge 后定义信号输出格式，交给韦小宝 PMBot 执行。

---

## 合规声明

本仓库全部产物为**信息 / 自动化工具**，不构成投资建议，不喊单、不荐股、不承诺收益。
真金白银操作（实盘 / 钱包 / 私钥 / 真实下单）须验证达标或用户逐单确认。

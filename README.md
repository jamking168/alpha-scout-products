# Alpha-Scout 情报变现产品集

> 千里眼（alpha-scout）主线：**从资讯发现商机 → 做工具 → 发钩子 → 主动推销**。
> 本仓库是情报库中 **GO 级商机落地后的可运行产物**，按「信息/自动化业务优先」原则筛选，
> 全部为真实跑通的代码，非 demo 占位。

---

## 一、项目介绍

情报扫描每天从 web3 / 美股 / 外汇 / AI 前沿信源采集、去重、分级（GO / HOLD / PASS）。
对升级为 **GO** 的商机，按「能否用程序员 + 海外 VPS + agent 集群直接落地赚钱」标准二次筛选，
落地为三个可运行产品：

| 子项目 | 定位 | 状态 |
|---|---|---|
| `agent-commerce/` | AI 代理支付（x402/A2A）：让 agent 集群变成「可被其他 agent 付费调用」的服务 | ✅ 已上线 |
| `rwa-monitor/` | 印度/主权 RWA 链上资金流监控：DeFiLlama 一手数据，每日快照 | ✅ 已上线（cron 每日跑） |
| `fomc-diff/` | FOMC 决议即时 diff 信息产品：声明措辞 + 点阵图量化 | ✅ demo 跑通（1 硬依赖待解） |

三者共享同一**收款底座**：TRON USDT（TRC-20），唯一金额尾数匹配，无需 memo。

---

## 二、仓库结构

```
alpha-scout-products/
├── README.md                    # 本文件（项目总结）
├── .gitignore
├── agent-commerce/              # ① AI 代理支付
│   ├── a2a_server.py            #   A2A Merchant 服务（x402 付费，纯标准库）
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
- **收款**：TRON USDT，唯一金额尾数匹配（尾数 0.01–0.99 即订单标识）。
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

---

## 四、当前进度

### ① agent-commerce —— ✅ 端到端闭环全通
- A2A 服务已上线（systemd `a2a-agent.service` 常驻，端口 8898）。
- 公网可发现：`https://web3project.top/.well-known/agent-card.json`；调用端点 `/a2a/v1`。
- 完整链路已验证：发现 → 调用 → 付款要求 → 到账验证 → 交付真实情报简报。
- 已登记 a2a-registry.org 目录（当前状态 unclaimed）。

### ② rwa-monitor —— ✅ 已上线，cron 每日跑
- 第一份快照（2026-09-22）：RWA 板块 TVL **$5.80B**（占 DeFi 3.11%），30 天 +$40M 流入、7 天 -$221M 流出（被 Anemoy 单日断崖拖累）。
- 真实信号：Ethena USDtb / Huma V2 / Plume Vaults 出现 60–90% 级链上流入。
- cron `d91d921643b6` 每天 9:00 自动跑。

### ③ fomc-diff —— ✅ demo 跑通（1 硬依赖待解）
- 9/16 决议真实 demo：利率 +25bp、声明逐句 diff、点阵图 2026 +30bp。
- **唯一硬依赖**：CME FedWatch 主站被地域/IP 封锁，需美国/新加坡出口 IP 或代理。

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

---

## 合规声明

本仓库全部产物为**信息 / 自动化工具**，不构成投资建议，不喊单、不荐股、不承诺收益。
真金白银操作（实盘 / 钱包 / 私钥 / 真实下单）须验证达标或用户逐单确认。

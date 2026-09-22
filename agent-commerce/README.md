# agent-commerce — AI 代理支付（x402 / A2A）

> 让 Hermes agent 集群变成「可被其他 agent 付费调用」的服务。
> 通过 A2A 协议发现 + x402 链上支付，按次收 TRON USDT。

---

## 是什么

A2A（Agent2Agent）是 Google 主导的「agent 间互操作」开放协议，核心两步：

1. **发现**：`GET /.well-known/agent-card.json` 返回 Agent Card（技能清单 + 端点）。
2. **调用**：`POST /a2a/v1`（JSON-RPC `message/send`），按需触发 x402 付费。

x402 是 Coinbase 的 HTTP 402 链上支付标准，本实现用**自定义 TRON scheme**：
买家按「唯一金额尾数」转 USDT（TRC-20）到收款地址，尾数即订单标识，无需 memo。

## 为什么选这条（三协议调研结论）

| 协议 | 定位 | 结论 |
|---|---|---|
| **x402 / A2A-x402** | agent 卖服务收款 | ✅ 首选，开源、免注册、免 KYC，个人可自助接入 |
| ACP（Stripe×OpenAI） | agent 帮用户下单的电商结账 | ❌ 需 Stripe 商户 KYC + 禁售清单踩「无牌照金融产品」线 |
| TAP（Visa） | 买家侧信任/鉴权层 | ❌ 需 Visa 注册发密钥，不解决「收钱」 |

详见 `调研简报.md` 与 `references/` 一手规范。

## 文件结构

```
agent-commerce/
├── a2a_server.py      # A2A Merchant 服务（纯标准库，零依赖）
├── agent-card.json    # A2A Agent Card v1.0（5 技能 + x402 扩展）
├── 调研简报.md         # 三协议调研 + 落地判断
└── references/        # 一手规范证据
```

## 运行

```bash
# 直接跑（监听 8898）
python3 a2a_server.py

# 或 systemd 常驻
sudo cp a2a-agent.service ~/.config/systemd/user/  # 见部署说明
systemctl --user enable --now a2a-agent
```

## 端到端流程

```
1. 发现   GET  /.well-known/agent-card.json
2. 调用   POST /a2a/v1  message/send  → 返回 input-required + 付款要求(USDT + 唯一尾数)
3. 付费   买家转 5.x USDT 到收款地址
4. 验证   服务查 TronGrid 按金额尾数匹配到账
5. 交付   返回 completed + 情报简报(读情报库最新 GO 信号)
```

## 收款配置

在 `a2a_server.py` 顶部配置：

- `RECEIVE_ADDRESS`：TRON USDT 收款地址
- `USDT_CONTRACT`：USDT-TRC20 合约（TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t）
- `PRICE_USD`：首个技能定价（默认 $5/次）

## 当前技能（5 个）

1. `daily-intel-brief` — 每日前沿情报简报（$5/次，已实现付费闭环）
2. `backtest-accounting-audit` — 回测组合收益与资金账本审计
3. `deal-screening-diligence` — 机会筛选与尽调
4. `strategy-profitability-audit` — 策略盈利性审计
5. `competitor-news-monitor` — 竞品重大新闻监控

## 待办

- [ ] 认领 a2a-registry.org 目录（当前 unclaimed，不影响调用）
- [ ] 挂更多付费技能并分层定价
- [ ] 主动向 A2A 生态外联获客

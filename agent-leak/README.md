# agent-leak:AI 编码代理配置泄密检测

**一句话**:AI 编码代理(Claude Code 等)会把你在命令里敲的口令、token 原样写进
`.claude/settings.local.json`,而这个文件默认**不在 .gitignore 里**;一旦提交,
口令就永远留在 git 历史和 fork 里。本目录是对应的检测工具与实测记录。

## 为什么做这个(需求侧证据)

| 证据 | 来源 | 级别 |
|---|---|---|
| 104,185 份公开 `.claude/settings.local.json`(103,617 个仓库),其中 1,122 份(≈1.1%)含至少一个真凭据,共 1,379 个 | Apiiro 研究(2026-11-05),The Hacker News 报道(2026-11-06) | A(原始研究)+ B(媒体) |
| 构成:379 个数据库口令、43 个公网 SSH 登录、42 个 GitHub/GitLab/npm token、23 个 AWS key、122 个 Supabase service-role key;63 家机构受影响 | 同上 | A |
| 现有免费扫描器有盲区:gitleaks 命中 564/1,122,TruffleHog 643/1,122,**两者都漏 215 份(19%)** —— 漏的是"整条命令里的口令"(`PGPASSWORD=…`/`sshpass -p`/`mysql -p`) | 同上 | A |
| 我方独立复现:GitHub code search(token 鉴权)现在返回 **89,600** 个 `path:.claude/settings.local.json` 文件;抽样 60 个、可解析 57 个,**1 个真泄密(1.8%)**,另有 2 个是 `${VAR}` 模板误报 | 本目录 `scripts/sample_public.py`,2026-11-08 05:03 UTC | A(可复现) |

## 工具

```bash
# 全局抽样:测总体泄露率
GITHUB_TOKEN=... python3 scripts/sample_public.py

# 单目标组织扫描(销售动作:一个目标一次确定性扫描)
GITHUB_TOKEN=... python3 scripts/scan_owner.py --owner <github-org> --max-repos 60
```

- 输出永远是**脱敏**的:只给类型 + 前 4 位 + 长度,不给完整值。
- 模板引用(`${VAR}`、`{{x}}`、`process.env`)显式排除——朴素正则会多报约 3 倍(实测 60 样本里 3 报 1 真)。
- `org:`/`user:` 限定符在 code search 里实测返回 0(`supabase`/`cloudflare`/`vercel` 均 0),
  所以单目标扫描走"列仓库 → 逐仓探文件"(contents API,404=不存在),已验证可用。

## 实测记录

- `data/sample-20261108.json`:60 份公开文件抽样,1 真泄密(一家公开电商工具仓的 `.claude/settings.local.json.bak`,
  含 1,822 字符 JWT),2 个误报。
- `data/owner-supabase-20261108-130636.json`:supabase 25 个公开仓库 → 3 份配置文件在库,**0 含凭据**。
  这条本身说明:大厂(有安全团队)通常干净,泄露集中在中小团队——买方画像因此明确。

## 边界(硬约束)

1. **不做"先拿到别人口令再私信勒索"那种动作**。对外话术只报"存在 N 份含凭据的配置文件",
   具体值在对方书面确认后、走负责任披露流程再给。
2. 不存储、不转售他人凭据;本地 JSON 只存掩码值。
3. 我们的价值是"看别人看不到的那 19%",不是"帮你入侵"。

## 下一步(按顺序,不跳)

1. 目标清单 30 家(有公开 GitHub org 的中小 AI 产品团队),跑 `scan_owner.py` 全量出数。
2. 每家里程碑:命中 → 发"存在 N 份配置文件含凭据,细节在你确认后提供";未命中 → 不发。
3. 报价:诊断报告 $99–199(1 页结论 + 修复步骤),整改 + 复测 $499;月度复扫 $19–49。
4. 验证阈值写死:20 封触达 → ≥2 个回复、≥1 个愿付 → 继续;0 回复 → PASS 这条线。

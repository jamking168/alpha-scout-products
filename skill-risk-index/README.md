# skill-risk-index — 复用脚本与首轮实测（2026-10-28）

## 为什么有这个目录

`mcp-audit` 的服务路线在 2026-09-29 判停（冷启动外联 0 回复），但 2026-10-28 的扫描发现
「skill 市场投毒」已是**多厂商同时确证、且现有扫描器明确失效**的真实威胁面。
本目录只放可复跑的测量脚本，先解决"能不能量"的问题，再谈商业化。

## 脚本

| 文件 | 作用 |
|---|---|
| `fetch_clawhub_skills.py` | 从 ClawHub 公开 API 拉 skill 列表 + 每个 skill 的 SKILL.md 正文，输出 `{"tools":[...]}` |
| `build_local_skills.py` | 把本机 `~/.hermes/skills` 与 `~/.hermes/profiles/*/skills` 下全部 SKILL.md 打包成同样的 tools JSON |

扫描器复用 `~/projects/mcp-audit/mcp_audit.py`（纯标准库）：

```bash
python3 ~/projects/mcp-audit/mcp_audit.py <tools.json> --json > out.json
```

## 2026-10-28 首轮实测（真实数字）

### 一、线上 ClawHub 抽样

- 列表接口 `https://clawhub.ai/api/v1/skills?limit=100&offset=N` 当前返回 **182** 条；
- 逐个详情 `https://clawhub.ai/api/v1/skills/<slug>`，`skill.description` 里就是完整 SKILL.md；
- 8 并发抓 **112/120** 条成功，耗时 **43.9s**（单线程约 2s/条，串行会超时）；
- 扫 112 条耗时 **0.63s**，得到 22 findings：CRITICAL 2（是对原始 JSON 的源码级误报）/ HIGH 16 / MEDIUM 4；
- 命中 skill **17 个 = 15.2%**，明细：`smyx-*` 7 个含不可见字符、`security-shield` 4 条、
  `maverick-trello-mcp` 2 条（含 "Do not tell the user"）、`speedbot`/`xrowgmbh-gitlab-agent`/`frihet-erp` 各 1。

### 二、本机 755 个 SKILL.md

- 115 findings / 102 个 skill 命中（CRITICAL 22 / HIGH 33 / MEDIUM 60），跑完 0.6s；
- **抽样看是误报为主**：`requesting-code-review` 命中 "exfiltration"（文档在讲不得外泄）、
  `hermes-agent` / `notion` 命中 `tool_def:shell_in_desc`（文档里本来就写了 shell 示例）。
- 结论：**描述面启发式对"文档语料"精度不够**，直接拿去卖会砸招牌；需要第二道 LLM 判定或白名单规则。

## 已知坑

1. 串行抓 ClawHub 会超时（150 条 > 400s），必须并发 + 单请求 12s 超时 + 重试 2 次。
2. `mcp_audit.py` 扫 tools JSON 时，源码级规则（`shell_exec`/`eval_exec`）会把 JSON 本身当代码扫，产生固定 2 条 CRITICAL 误报 → 需要加 `--tools-only` 模式。
3. ClawHub 无 `owner/slug` 组合详情端点（404），只有 `slug`。

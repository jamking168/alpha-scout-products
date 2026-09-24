# localjev 集成说明（本地跑 Jev 决策模型，无需 TypeSafe API key）

## 背景

原 polymarket-jev 依赖 TypeSafe 的 Jev 模型 API（`api.typesafe.ai`），需要申请
`TYPESAFE_API_KEY`。之前因申请不到 key 而搁置。

2026-09-22 GitHub Next（微软）官方开源 **localjev**：一个本地、Jev-compatible 的
`POST /v1/systemone` API（TypeScript/Bun），后端接 DiffusionGemma 模型。这条「本地
跑 System 1 决策模型」的路线被大厂盖章，polymarket-jev 可以本地跑，不再需要 key。

## 集成点（已改好）

`pmjev/config.py` 的 `load_config()` 已支持 `TYPESAFE_BASE_URL` 环境变量覆盖，
指向任意 Jev-compatible 后端（localjev 或其它）：

```bash
export TYPESAFE_BASE_URL=http://127.0.0.1:8080
export TYPESAFE_API_KEY=local   # localjev 接受任意值（除非配了 LOCALJEV_API_KEY）
```

代码无需改动：`pmjev/typesafe.py` 的 `TypeSafeClient` 本来就是标准
`POST {base_url}` + `Authorization: Bearer {key}` 调用，localjev 接口兼容。

## 运行 localjev（当前 VPS 跑不动）

localjev 需要 Bun 1.2+ 和 oMLX 推理服务器，跑 **DiffusionGemma-26B-A4B-it-4bit** 模型
（26B 参数量）。本机 3.7G 内存跑不动，需更大机器（或有 GPU 的服务器）。

```bash
git clone https://github.com/githubnext/localjev
bun install && cp .env.example .env   # 改 .env 指向 oMLX
bun run start                          # 监听 http://127.0.0.1:8080
curl http://127.0.0.1:8080/ready       # 检查就绪
```

## 轻量替代（VPS 可跑的方向，待验证）

openjev-sglang / Qwen3.5 上的 **RBF 决策模型**（「tiny, fit in L1 cache, microsecond
inference」）——体积极小，可能在本机跑。待验证后可作为 localjev 的轻量替代。

## 现状

- `python run.py --demo` 离线跑通（StubClient + fixtures，无 key 无网络）
- 集成点已就绪，等有条件的机器跑 localjev 即可切换真实决策模型

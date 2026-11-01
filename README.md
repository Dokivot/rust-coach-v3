# Rust Coach v3

面向 Rust 生存游戏的教练 Agent。当前实现：FastAPI + LangGraph + SQLite 聊天，以及 PC 原版拆建筑爆炸物数量、合成路径与资源缺口计算。支持 OpenAI-compatible Responses 和 Chat Completions 接口，默认使用 Responses。

## 本地启动

需要 [uv](https://docs.astral.sh/uv/getting-started/installation/)，`uv sync` 会按 `.python-version` 准备 Python 3.12 并安装锁定依赖。

```sh
uv sync --locked
```

项目已有本地 `.env` 时直接编辑；首次克隆时复制 `.env.example` 为 `.env`。填写 `COACH_API_KEY` 后运行：

```sh
uv run uvicorn rust_coach.app:create_app --factory --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000>。配置默认使用：

```dotenv
COACH_BASE_URL=https://cf.api.fan/v1
COACH_MODEL=gpt-5.6-sol
COACH_API_MODE=responses
COACH_API_KEY=在本地填写你的密钥
```

服务读取 `.env` 后需要重启。密钥不会发给浏览器，也不纳入 Git。没有密钥时聊天输入禁用，但可以打开 <http://127.0.0.1:8000/calculator> 使用本地计算器。页面显示“已配置”只表示存在密钥，模型连通需实际调用验证。

`COACH_API_MODE=responses` 调用 `/v1/responses`，使用 `input` 和 `max_output_tokens`，并设置 `store=false`，每轮从本地历史重建上下文。已有 `.env` 未设置该项时也默认使用 Responses。`COACH_BASE_URL` 仍填写包含 `/v1` 的服务基础地址，不要追加 `/responses` 或 `/chat/completions`。实现依据 [OpenAI 官方迁移文档](https://developers.openai.com/api/docs/guides/migrate-to-responses)。

如果服务仅支持旧接口，设置 `COACH_API_MODE=chat_completions`，即可调用 `/v1/chat/completions`。此模式默认使用 `max_completion_tokens`；如服务不支持，在 `.env` 设置 `COACH_OUTPUT_TOKEN_PARAMETER=max_tokens`。该配置只影响 Chat Completions，Responses 始终使用 `max_output_tokens`。两种模式均可通过 `COACH_MAX_OUTPUT_TOKENS` 设置输出上限，通过 `COACH_INSTRUCTION_ROLE=system` 或 `developer` 配置指令消息角色。

## 首版边界

- 支持普通多轮对话、新建/删除当前会话、刷新恢复当前会话、错误提示和重复请求去重；回复完整返回后显示，尚未采用流式输出。
- 图通过条件边分流普通聊天与计算；模型只解析计算参数，Schema 校验后调用本地引擎，固定模板输出公式、批次、缺口和来源。
- SQLite 将完整成功轮次存入 `data/coach.sqlite3`，每次请求最多使用最近 12 轮、约 24000 字符上下文；每个会话最多 100 轮。新建会话会保留旧记录，但首版没有历史会话列表。
- 计算支持 8 类墙门、5 种爆炸物、60 个制作配方及已有成品/中间材料抵扣；支持多轮补充和重启恢复。支持范围、算法和 API 示例见 [计算实施说明](docs/calculations.md)。
- 数值来自 2026-11-01 的 RustData 固定快照，保留来源和哈希；未经游戏内实测、未核验游戏 build，不宣称随补丁实时准确。只比较单一爆炸物方案的新增硫磺，不求混搭全局最优。
- 知识库、向量检索、实时搜索、长期玩家状态、Plan-and-Execute 与战斗伤害尚未接入。普通聊天的能力提示仍不能替代事实验证。
- 仅适用于本机单用户、单进程开发；一次只运行一个模型请求。没有身份认证、多用户隔离或生产部署能力，请保持绑定 `127.0.0.1`，不要使用多个 workers。

## 开发与验证

```sh
uv run pytest -q
node --check src/rust_coach/static/app.js
node --check src/rust_coach/static/calculator.js
```

测试使用替代模型及 HTTP MockTransport 验证 LangGraph、HTTP、上下文、持久化及异常处理，同时验证计算阈值、共享资源、批次、库存守恒与来源追溯。不调用付费模型，不代表游戏内规则认证或外部模型连接。Node 仅用于 JavaScript 语法检查。

模块说明和后续接入路径见 [首版实施说明](docs/scaffold.md)。API 的机器可读定义位于 `/openapi.json`，首版不加载依赖外部 CDN 的交互文档页。

## 目标架构

下面描述后续业务系统目标，并非当前已实现能力：决策采用 Plan-and-Execute，知识获取采用含向量检索和实时搜索的 Agentic RAG，主图与子图支持中途澄清和持久恢复。

- [完整架构设计](docs/architecture.md)：需求分析、模块边界、知识与计算、状态记忆、工作流、工程化、评测和成本。
- [代码生成就绪度评审](docs/codegen-readiness.md)：当前可生成范围、实施前缺口、图文歧义及建议补充顺序。
- [技术架构图 SVG](docs/architecture.svg)：标注数据流、关键职责及确定性/生成式组件。
- [架构图 PNG](docs/architecture.png)：便于直接预览和分享。
- [Mermaid 图源](docs/architecture.mmd)：可编辑的模块关系图。
- [Plan-and-Execute 流程](docs/plan-and-execute.mmd)：Planner、Executor、Replanner、验证与恢复。
- [Agentic RAG 流程](docs/agentic-rag.mmd)：来源选择、语义检索、实时搜索、正文验证、查询改写与中途澄清。
- [SVG 生成器](docs/render_architecture.py)：运行 `python3 docs/render_architecture.py` 重新生成架构图。

两类强计算已实现受限范围，详细边界与当前工作流见 [计算实施说明](docs/calculations.md)；其余目标架构仍为后续设计。

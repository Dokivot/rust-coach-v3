# Rust Coach v3

面向 Rust 生存游戏的教练 Agent。当前版本是可运行的本地聊天骨架：FastAPI + LangGraph + SQLite + 浏览器聊天界面，通过 OpenAI-compatible Chat Completions 接口调用模型。

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
COACH_API_KEY=在本地填写你的密钥
```

服务读取 `.env` 后需要重启。密钥不会发给浏览器，也不纳入 Git。没有密钥时页面仍可打开，但输入被禁用并显示配置提示。页面显示“已配置”只表示存在密钥，成功收到回复后才显示本次连接成功。该第三方服务的模型权限与兼容性需用有效密钥实测。

如服务不支持 `max_completion_tokens`，在 `.env` 设置 `COACH_OUTPUT_TOKEN_PARAMETER=max_tokens`；指令消息角色可通过 `COACH_INSTRUCTION_ROLE=system` 或 `developer` 配置。不要盲目切换参数，应根据服务文档或实际返回定位。

## 首版边界

- 支持普通多轮对话、新建/删除当前会话、刷新恢复当前会话、错误提示和重复请求去重；回复完整返回后显示，尚未采用流式输出。
- 图只有 `START → chat → END`；模型、图、运行时、数据契约、存储和后续能力接口分离。
- SQLite 将完整成功轮次存入 `data/coach.sqlite3`，每次请求最多使用最近 12 轮、约 24000 字符上下文；每个会话最多 100 轮。新建会话会保留旧记录，但首版没有历史会话列表。
- 知识库、向量检索、实时搜索、玩家状态解析、Plan-and-Execute、计算工具均未接入，扩展占位返回 `unavailable`，不提供假结果。
- 提示词要求模型不编造游戏数值与已验证的决策；这只是当前能力边界提示，不能作为游戏事实正确性的技术保证。游戏业务规则、真实验收案例和计算器另行调研。
- 仅适用于本机单用户、单进程开发；一次只运行一个模型请求。没有身份认证、多用户隔离或生产部署能力，请保持绑定 `127.0.0.1`，不要使用多个 workers。

## 开发与验证

```sh
uv run pytest -q
node --check src/rust_coach/static/app.js
```

测试使用替代模型及 HTTP MockTransport 验证真实 LangGraph、HTTP 契约、上下文、持久化、重试及异常处理，不调用付费模型，不代表已验证游戏知识或外部模型连接。Node 仅用于可选的 JavaScript 语法检查，运行聊天服务不需要 Node。

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

强计算只预留“拆建筑所需爆炸物”和“合成路径与资源缺口”两个接口，暂不实现或接入计算器网站；业务质量、性能和成本目标待后续实施验证。

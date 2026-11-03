# Rust Coach v3

面向 Rust 生存游戏的教练 Agent。当前实现：FastAPI + LangGraph + SQLite 聊天、PC 原版拆建筑与合成计算、玩家状态与条件建议，以及背包、基地、装备截图的自动识别与分析。支持 OpenAI-compatible Responses 和 Chat Completions 接口，默认使用 Responses。

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

服务读取 `.env` 后需要重启。密钥不会发给浏览器，也不纳入 Git。没有密钥时可使用 `/calculator`、`/player-state`，以及聊天中的 `/state` 和 `/calc JSON`；普通聊天与文本状态解析仍需密钥。

主聊天页只保留对话和图片输入。粘贴背包截图后直接问“合成一个 C4 还差多少资源”，无需重发图片或选择容器。玩家档案管理仍可在 <http://127.0.0.1:8000/player-state> 使用；档案绑定、显式容器选择及表达偏好保留为 API 能力，不再在主聊天页显示设置表单。

`COACH_API_MODE=responses` 调用 `/v1/responses`，使用 `input` 和 `max_output_tokens`，并设置 `store=false`，每轮从本地历史重建上下文。已有 `.env` 未设置该项时也默认使用 Responses。`COACH_BASE_URL` 仍填写包含 `/v1` 的服务基础地址，不要追加 `/responses` 或 `/chat/completions`。实现依据 [OpenAI 官方迁移文档](https://developers.openai.com/api/docs/guides/migrate-to-responses)。

Responses 模式下，普通聊天、计算、建议和状态解析均使用上游流式传输（`stream=true`），从文本分片收集回复，兼容结束事件中 `output=[]` 的服务。浏览器实时显示识图、检索、计算、组织语言等实际执行阶段，正文仍在回复收集完成后一次显示。阶段命名、事件协议与验证见 [实时执行阶段](docs/workflow-progress.md)。

如果服务仅支持旧接口，设置 `COACH_API_MODE=chat_completions`，即可调用 `/v1/chat/completions`。此模式默认使用 `max_completion_tokens`；如服务不支持，在 `.env` 设置 `COACH_OUTPUT_TOKEN_PARAMETER=max_tokens`。该配置只影响 Chat Completions，Responses 始终使用 `max_output_tokens`。两种模式均可通过 `COACH_MAX_OUTPUT_TOKENS` 设置输出上限，通过 `COACH_INSTRUCTION_ROLE=system` 或 `developer` 配置指令消息角色。

## 首版边界

- 支持普通多轮对话、新建会话、刷新恢复、粘贴/拖入/选择图片、错误重试和重复请求去重。删除会话保留 API；回复完整返回后显示，尚未逐字流式展示。
- 图通过条件边分流普通聊天与计算；模型只解析计算参数，Schema 校验后调用本地引擎，固定模板输出公式、批次、缺口和来源。
- SQLite 将完整成功轮次存入 `data/coach.sqlite3`，每次请求最多使用最近 12 轮、约 24000 字符上下文；每个会话最多 100 轮。新建会话会保留旧记录，但首版没有历史会话列表。
- 计算支持 8 类墙门、5 种爆炸物、60 个制作配方及已有成品/中间材料抵扣；支持多轮补充和重启恢复。支持范围、算法和 API 示例见 [计算实施说明](docs/calculations.md)。
- 数值来自 2026-11-01 的 RustData 固定快照，保留来源和哈希；未经游戏内实测、未核验游戏 build，不宣称随补丁实时准确。只比较单一爆炸物方案的新增硫磺，不求混搭全局最优。
- 玩家状态覆盖容器库存、科技、基地、阶段、时间、目标和偏好。支持表单与文本草稿确认、局部更新、观测有效期、事件失效、版本历史和冲突保护。未知不等于零，计算不会扣减真实背包。
- 已接入按需网页取证与状态建议：规划候选、选择检索主题、调用本地工具、比较取舍、引用正文并保留快照。输入 `/coach 问题` 或 `/search 问题` 使用；无需向量库。详见 [建议链路与实际架构图](docs/advice.md)。
- 新手开局问题先检索 Falcon 等来源的入门攻略，再生成有顺序的行动路线；无需先填写玩家档案，已有进展会作为本轮条件。阶段依据、复核与验证见 [攻略驱动的新手引导](docs/onboarding-guides.md)。
- 已接入统一人设与表达校验；版本、检查、原始报告仍在 API 和 SQLite 留存，主聊天页不显示调试记录。可直接要求“简短一点”或“详细列出计算过程和来源”。早期设计与验证见 [人设实施说明](docs/persona-implementation.md)。
- 计算与建议默认先给自然短答，完整计算、截图观测和来源可在回复下方展开；追问历史只带正文。事实保护与验证见 [自然回复修复说明](docs/natural-replies-fix.md)。
- 图片无需填时间或逐项确认，默认当前本人；最近同会话图片自动用于后续自然语言提问。局部截图也会抵扣可读的材料，明确提示未展示/未知材料未计入，实际缺口可能更少。旧图、他人图、失效图不抵扣；不会自动写入长期档案。详见 [截图实施说明](docs/multimodal-implementation.md) 和 [本次修复验证](docs/chat-inventory-fix.md)。
- 在线搜索使用 Tavily：在本地 `.env` 配置 `COACH_SEARCH_PROVIDER=tavily` 和 `COACH_SEARCH_API_KEY=你的Tavily密钥`，重启服务后生效；设为 `off` 可关闭联网。未配置密钥或搜索失败时明确降级为直接读取已登记资料。搜索摘要不作为事实依据；抓取时间不代表游戏版本已核验。
- 已接入跨会话个人记忆：明确的表达偏好、自报习惯、关键历史与知识点进度。输入 `/memory` 查看，`/memory remember 个人经历` 立即记录，`/memory forget 标识` 删除，`/memory off` 暂停。按知识点选择逐步解释、适度提示或直接讨论条件；“讨论过”和“掌握”分开。详见 [记忆与循序讲解](docs/memory.md)。
- 尚无向量检索、完整基地拓扑诊断、战斗伤害计算、原生 Checkpointer 中断恢复或全局最优策略求解。个人偏好跨服保留，事件按档案隔离，不继承旧库存。截图确认草稿可持久恢复；引用匹配与人工确认均不等于事实语义验证。
- 仅适用于本机单用户、单进程开发；聊天任务串行，探测与上传另有限流。没有身份认证、多用户隔离或生产部署能力，请保持绑定 `127.0.0.1`，不要使用多个 workers。

## 开发与验证

```sh
uv run pytest -q
node --check src/rust_coach/static/app.js
node --check src/rust_coach/static/calculator.js
node --check src/rust_coach/static/player-state.js
node --check src/rust_coach/static/vision.js
node --test tests/test_chat_ui.cjs
```

测试使用替代模型及 HTTP MockTransport 验证 LangGraph、HTTP、上下文、持久化及异常处理，同时验证计算阈值、共享资源、批次、库存守恒与来源追溯。不调用付费模型，不代表游戏内规则认证或外部模型连接。Node 用于 JavaScript 语法检查和聊天详情的 DOM 回归测试。

另有 [真实模型、外部网页与浏览器验证记录](docs/advice-verification.md)，包含示例背包候选预算、条件建议、测试结果与界面截图。

模块说明和后续接入路径见 [首版实施说明](docs/scaffold.md)。API 的机器可读定义位于 `/openapi.json`，首版不加载依赖外部 CDN 的交互文档页。

## 目标架构

- [六层架构：输入层设计](docs/architecture-input-layer.md)：定义用户提示词和可选图像附件的接入、校验、归一化、来源，以及由此解析玩家状态的交接契约。
- [六层架构：理解层设计](docs/architecture-understanding-layer.md)：定义意图识别、状态解析、请求路由、数据流向，以及确定性组件与 LLM/VLM 的职责边界。
- [六层架构：生成层设计](docs/architecture-generation-layer.md)：定义决策结果、人设约束、受保护断言、叙述组装、输出门禁和确定性回退。
- [六层架构：评测与观测层设计](docs/architecture-evaluation-observability-layer.md)：定义跨层 trace、输入/检索/决策/生成事件、延迟、成本、质量指标、badcase 和发布门禁。
- [六层架构：决策层设计](docs/architecture-decision-layer.md)：定义简单工具调用、Plan-and-Execute、Agent Loop、工具治理，以及与理解层、知识层和生成层的交接。

- [多模态截图设计](docs/multimodal-design.md) 保留目标和评测规划；首版实际实现、边界及验证见 [实施说明](docs/multimodal-implementation.md) 与 [实际数据流图](docs/multimodal-implemented.mmd)。
- [人设与表达一致性设计](docs/persona-design.md)：实施前的调研、协议设计与目标指标；附 [来源审核](docs/persona-source-audit.json) 与 [18 个评测种子案例](docs/persona-eval-seeds.json)。已落地范围、测试覆盖与未解决的语义边界见 [实施说明](docs/persona-implementation.md)。

下面保留早期完整目标设计。当前落地采用有界规划/取证/验证子图，暂不使用向量库，最新实现边界以 [当前建议架构](docs/advice.md) 为准；早期图中的向量检索、中断和持久恢复仍为后续能力。

- [完整架构设计](docs/architecture.md)：需求分析、模块边界、知识与计算、状态记忆、工作流、工程化、评测和成本。
- [六层架构：知识层设计](docs/architecture-knowledge-layer.md)：结构化数据库、向量数据库、知识图谱、工作/情景/语义记忆，以及知识层与理解层、决策层的交互和证据追溯。
- [决策知识层调研](docs/decision-knowledge-research.md)：结合当前实现评估行动知识、策略证据与 Agentic RAG 的适用范围；包含对既有架构预设的调整建议，尚未实施。
- [Rust 游戏来源与决策知识调研](docs/rust-domain-knowledge-research.md)：实际核查数据站、官方更新、攻略与社区，记录版本冲突、行动知识提取及落地顺序；附 [26 项来源审核记录](docs/rust-source-audit.json)。
- [代码生成就绪度评审](docs/codegen-readiness.md)：当前可生成范围、实施前缺口、图文歧义及建议补充顺序。
- [技术架构图 SVG](docs/architecture.svg)：标注数据流、关键职责及确定性/生成式组件。
- [架构图 PNG](docs/architecture.png)：便于直接预览和分享。
- [Mermaid 图源](docs/architecture.mmd)：可编辑的模块关系图。
- [Plan-and-Execute 流程](docs/plan-and-execute.mmd)：Planner、Executor、Replanner、验证与恢复。
- [Agentic RAG 流程](docs/agentic-rag.mmd)：来源选择、语义检索、实时搜索、正文验证、查询改写与中途澄清。
- [SVG 生成器](docs/render_architecture.py)：运行 `python3 docs/render_architecture.py` 重新生成架构图。

两类强计算已实现受限范围，详见 [计算实施说明](docs/calculations.md)；当前建议链路另附 [可编辑工作流图](docs/advice-workflow.mmd)。

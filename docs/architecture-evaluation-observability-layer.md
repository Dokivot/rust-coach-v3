# Rust Coach 六层架构：评测与观测层设计

修订日期：2026-11-03。评测与观测层横切输入层、理解层、知识层、决策层和生成层，负责记录一次任务如何被处理、花费了什么、产生了什么结果，以及结果是否满足质量要求。它同时服务在线故障定位、离线质量评测、badcase 回归和版本发布门禁，但观测到一个事件不等于证明业务结果正确。

## 1. 层定位与目标

```text
一次用户任务
  → Trace：从请求到回答的完整因果链
  → Spans/Events：各层节点、模型、工具、检索和渲染数据
  → Metrics：延迟、成本、可靠性、覆盖和质量指标
  → Evaluation：离线金标、在线抽样、人工/模型辅助评审
  → Badcase：快照、归因、修复、回归和发布门禁
```

评测与观测层需要回答五类问题：

1. **发生了什么**：输入、路由、检索、工具、决策、生成和恢复是否按预期执行？
2. **花费了什么**：排队、模型、视觉、向量、网页、工具、重试和澄清分别花了多少时间与成本？
3. **结果可靠吗**：关键声明是否有来源，计算是否可复现，建议是否满足硬约束，生成是否保持状态？
4. **问题在哪里**：badcase 属于输入、理解、知识、决策、生成、基础设施还是跨层契约？
5. **版本能否发布**：新模型、提示词、知识 release、规则、渲染器和路由变化是否通过回归和预算门禁？

观测用于描述真实运行；评测用于有参照地判断质量。两者共享 trace/artifact 引用，但不能用低错误率的日志完整性替代金标评测，也不能用模型自评或“用户没有投诉”作为唯一真值。

## 2. 采集范围与数据边界

| 来源层 | 必采集内容 | 关键关联 |
| --- | --- | --- |
| 输入层 | 请求元数据、附件引用/hash、解析版本、候选、unknown、覆盖、校验错误 | `input_revision`、`attachment_id`、`input_hash` |
| 理解层 | 意图候选/最终意图、实体消歧、状态声明、路由、缺口、澄清和重理解 revision | `understanding_revision`、`route`、`clarification_id` |
| 知识层 | 查询、通道、过滤、召回、证据、版本、冲突、覆盖、实时抓取和降级 | `query_id`、`evidence_set_id`、`kb_release` |
| 决策层 | 链路、ExecutionPlan 摘要、步骤、工具调用、输入/输出 hash、验证、重规划、终止原因 | `decision_revision`、`step_id`、`tool_call_id` |
| 生成层 | `AnswerSpec`、保护断言、叙述模型、门禁、修复、回退、最终区块和版本 | `answer_id`、`assertion_id`、`renderer_version` |
| 用户/评测 | 用户纠正、澄清回答、反馈、人工标签、评审理由、金标和回放结果 | `feedback_id`、`case_id`、`eval_run_id` |

原始提示词、图片字节、私人记忆和网页正文默认不写入普通 metrics。日志只保存脱敏摘要、对象引用、内容 hash、区块定位和必要的最小字段；受控评测快照另存并有访问审计。任何下游事件都不能凭日志 payload 恢复已删除的私人内容。

## 3. 统一观测契约

### 3.1 Trace 与 Span

一次用户请求对应一个 `trace_id`；同任务澄清/resume 沿用 `task_id`，每个新输入增加 `input_revision`。每个在线节点、模型调用、工具调用、检索查询和持久化操作产生一个 span：

```json
{
  "trace_id": "tr-1",
  "span_id": "sp-17",
  "parent_span_id": "sp-10",
  "task_id": "task-1",
  "request_id": "req-1",
  "conversation_id": "conv-1",
  "layer": "decision",
  "component": "calculator_port",
  "event": "tool_call.completed",
  "started_at": "2026-11-03T10:00:00.100Z",
  "ended_at": "2026-11-03T10:00:00.180Z",
  "status": "ok",
  "versions": {
    "graph": "coach-graph-v1",
    "prompt": "coach-prompts-v1",
    "rules": "rules-2026-11-03",
    "provider": "calculator-v1"
  },
  "references": {
    "input_hash": "sha256:...",
    "state_revision": 12,
    "step_id": "step-2",
    "tool_call_id": "call-3"
  },
  "duration_ms": 80,
  "redaction": {"payload": "hashed", "private_fields": "omitted"}
}
```

必需字段：`trace_id`、`span_id`、`parent_span_id`、task/request/conversation ID、layer、component、event、时间、状态、版本、引用、duration、错误码和脱敏级别。事件不能把模型输出中的指令当作观测系统配置。

### 3.2 事件类别

| 类别 | 例子 | 用途 |
| --- | --- | --- |
| `lifecycle` | task.started、task.waiting_input、task.completed、task.cancelled | 任务状态、恢复和终止率 |
| `input` | request.received、attachment.validated、parsed.created | 输入失败、解析版本和回放 |
| `understanding` | intent.candidate、state.reconciled、route.selected、clarification.created | 路由与语义错误归因 |
| `retrieval` | query.started、source.selected、document.fetched、evidence.graded、coverage.assessed | 检索质量、来源和版本污染 |
| `decision` | plan.created、step.started、tool_call.completed、step.verified、replan.created、solution.finalized | Agent Loop、工具和结果质量 |
| `generation` | spec.created、assertion.checked、narrative.generated、output.gated、fallback.used | 事实保持、人设和回退 |
| `feedback` | user.corrected、user.confirmed、user.rejected、human.reviewed | 反馈和金标候选 |
| `evaluation` | case.started、grader.completed、regression.gated、release.published | 离线/在线评测和发布 |

同一个业务事实只由一个上游组件写权威事件；下游可以引用，不重复写一份可变副本。事件采用 append-only 语义，修正通过新事件和 `supersedes_event_id` 表示。

## 4. 各层观测字段

### 4.1 输入层

记录：接收/排队时间、请求体大小、附件数量和哈希、媒体类型、归属/过期检查、文本/视觉 extractor 版本、抽取耗时、候选数量、unknown 数量、覆盖范围、Schema 错误、跨模态绑定和降级原因。

不记录：原始图片 Base64、未脱敏私人文本、模型内部推理。评测回放只通过受控 `attachment_id`/hash 读取仍获授权的对象。

关键指标：输入拒绝率、附件安全失败率、解析成功率、unknown 保留率、视觉/文本抽取 p50/p95、跨模态指代准确率、局部观测误升级率。

### 4.2 理解层

记录：候选意图及最终 route、实体候选和消歧结果、槽位填充、状态声明来源/coverage/status、输入和理解 revision、知识查询次数、澄清字段、重理解原因、route gate 结果和耗时。

关键指标：意图/多意图 F1、关键槽位准确率、实体消歧准确率、漏澄清率、无谓澄清率、route accuracy、跨 scope 状态泄漏、重理解后正确路由率。

### 4.3 知识层

记录：`KnowledgeQuery` 的 purpose/filters/budget、来源通道、候选数、过滤数、top-k、重排、正文获取、引用片段、claim 支持/反驳、版本适用性、冲突、覆盖缺口、查询轮数、缓存命中和实时降级。

关键指标：Recall@k、nDCG、必要声明覆盖率、引用定位率、claim support rate、unsupported claim rate、跨版本污染率、冲突未解放行率、web 新鲜度、缓存命中率、单查询延迟和成本。

### 4.4 决策层

记录：`route`/execution mode、Agent Loop iteration、计划版本和步骤状态摘要、工具名称/版本、参数 hash、输入状态 revision、调用次数、重试、幂等键、工具结果 hash、验证码、重规划原因、澄清/终止原因和最终 `DecisionResult/TaskSolution` 引用。内部执行计划的观测只保存结构化摘要，不把完整思维链写入日志。

关键指标：工具成功率、schema 错误率、重复调用率、重规划率、无进展率、恢复成功率、硬约束满足率、结果可复现率、行动可执行率、`partial/blocked/unavailable` 比例、未配置 provider 假数字率和预算越界率。

### 4.5 生成层

记录：`AnswerSpec` 类型和状态、protected assertion 数量/检查码、叙述模型/提示词版本、生成耗时、门禁检查码、修复次数、回退原因、最终区块引用和 `AnswerEnvelope` hash。

关键指标：输出门禁通过率、断言/结果不一致率、数字保持率、条件保留率、引用支持率、虚构操作/经历率、状态误称率、人设违规率、回退率、修复成功率、答案完整度和用户可读性评分。

## 5. 延迟与成本观测

### 5.1 延迟分解

每个 span 使用单调时钟测量 `duration_ms`，并记录 wall-clock 时间用于关联。Trace 汇总：

```text
total_latency
  = queue
  + input_parse
  + understanding
  + knowledge/retrieval
  + decision/tool_loop
  + generation
  + persistence
```

同时记录首个有用信息时间（first useful result）、首个 token/阶段事件、最终答案时间、澄清等待时间和 resume 后增量耗时。报告 p50/p90/p95/p99、错误/取消分位数和关键路径；不要只报告平均值。队列、模型、网页和用户等待应分开，避免把等待澄清误算为模型慢。

### 5.2 成本分解

每次外部调用记录：provider/model、input/output/cached token、图片 token/字节、embedding token、搜索/抓取次数、工具计费单位、重试次数、货币和成本估算版本。字段区分 `estimated` 与 `billed`，未知成本不得写零。

按 `trace_id` 聚合：模型成本、视觉成本、embedding 成本、网络/工具成本、重试浪费、缓存节省、每个结果类型成本和每个成功任务成本。预算账本在调用前预留、完成后结算；观测事件只能引用账本，不能自行重新计费。

## 6. 质量评测体系

### 6.1 评测对象和真值

| 评测对象 | 真值来源 | 主要指标 |
| --- | --- | --- |
| 输入/理解 | 人工标注的意图、实体、槽位、状态范围和澄清必要性 | accuracy/F1、漏澄清、route accuracy |
| 检索/证据 | 声明-证据标注、版本和冲突标注 | Recall@k、支持率、覆盖率、污染率 |
| 决策 | 专家行动标签、硬约束、状态回放、工具金标 | 可行率、约束满足、结果正确、条件完整 |
| 生成 | 保护断言比对、人工盲评、独立事实/风格评审 | factual consistency、行动保真、风格、完整度 |
| 端到端 | 固定输入和预期任务结果/用户确认 | task success、澄清效率、恢复、成本、延迟 |

模型评审只能作为一个维度，必须与代码不变量、工具金标和人工校准分开报告。评测报告保留样本数、置信区间、失败样本和数据集版本；不能通过全部拒答来刷准确率。

### 6.2 数据集与回放

评测案例包含：脱敏输入快照、附件 hash/区域、期望意图和状态范围、知识 release、玩家 state revision、工具/provider 版本、期望 route、金标结果、可接受条件、禁止断言和评审标签。

数据集按来源、场景、规则版本和时间切分，防止同义改写、转载网页和同一玩家历史泄漏。线上 badcase 进入 staging 后才能加入回归集；加入前必须去除秘密、私人内容和未授权附件。

回放固定所有影响结果的版本和随机种子，重放时比较：route、工具参数 hash、证据集合、`DecisionResult`、受保护断言、最终状态、延迟和成本。允许模型叙述措辞变化，但不允许关键业务字段变化。

### 6.3 发布门禁

发布候选至少通过：

- 契约/Schema、输入隔离和跨 scope 测试；
- 强计算结果与固定金标一致，未配置 provider 不产生数字；
- 检索声明覆盖、版本适用性和冲突门禁；
- Agent Loop 无重复副作用、预算不越界、澄清/resume 可恢复；
- 生成层保护断言、条件状态和引用保持；
- 延迟/成本 p95、错误率和回退率不超过预算；
- 高严重度 badcase 为零，其他指标达到版本化阈值并有样本量说明。

任一关键门禁失败时阻止 active release 或自动回滚到上一个兼容版本；门禁结果本身写入 `EvalReport`，不能只在 CI 日志中存在。

## 7. Badcase 生命周期

```text
触发（用户纠正/错误/抽样/指标异常）
  → 脱敏快照与版本冻结
  → 分类与严重度
  → 人工金标/根因分析
  → 修复候选（代码/提示/知识/路由/工具/数据）
  → 回归集新增与对照评测
  → 灰度观察
  → 发布或回滚
```

### 7.1 记录格式

```json
{
  "case_id": "bad-42",
  "trace_id": "tr-1",
  "severity": "high",
  "category": "generation.fact_drift",
  "symptom": "conditional 被表达为 verified",
  "expected": {"status": "conditional", "required_terms": ["尚未确认"]},
  "observed_refs": ["answer-1", "assertion-7", "span-19"],
  "root_cause": "narrative_gate_missing_status_check",
  "owner": "generation",
  "regression_case_id": "eval-2026-11-03-17",
  "status": "fixed_pending_release"
}
```

分类至少区分：输入解析、视觉/文本融合、理解路由、状态范围、知识召回、证据支持、版本时效、工具/schema、Agent Loop/恢复、决策约束、生成事实漂移、人设、隐私/注入、延迟/成本和基础设施。一个 badcase 可以有主因和跨层贡献因。

用户反馈先作为线索；不能因为一次“成功”自动更新规则或策略知识，也不能把一次死亡/存活直接当作建议因果真值。知识更新和记忆更新仍走各自审核流程。

## 8. 观测系统、采样与隐私

在线默认全量保留低敏事件和计数指标；高容量 payload 采用尾部采样：错误、超时、门禁失败、用户纠正、严重 badcase 和高成本 trace 强制保留，正常成功 trace 按版本化比例采样。采样决策写入 trace，避免评测时误认为缺失样本不存在。

访问控制按玩家 scope、评测角色和环境隔离；生产观测只显示脱敏摘要。原始文本、图片、网页正文、个人记忆和评测快照使用独立保留期，支持删除和撤销访问。日志不保存密钥、完整私有图像或模型私有思维链。

观测后端可采用 OpenTelemetry-compatible traces/metrics/logs，加关系库保存 `EvalCase`、`EvalRun`、`Badcase` 和 release 门禁；实现初期可用 SQLite/JSONL 作为开发期 sink，但事件字段和引用契约不应随存储迁移改变。

## 9. 当前实现映射与差距

| 能力 | 当前实现 | 状态 |
| --- | --- | --- |
| 请求/业务结果留存 | SQLite conversations/turns、calculation、advice、presentation、visual | 已保存部分业务结果，但没有统一跨层 trace |
| 生成检查 | `AnswerEnvelope`、`checks`、`fallback_reason`、persona tests | 已有生成门禁字段和离线测试 |
| 评测脚本 | `presentation/evaluate.py`、`architecture.evaluation`、`evals/inventory_baseline.py`、`tests/test_*` | 库存 V2 已有版本化 JSON 案例、隔离 HTTP 执行、逐轮证据判定和复核；通用契约尚未接入统一评测仓库 |
| 检索证据 | `EvidenceStore`、网页快照和 advice 引用 | 有局部来源追踪；缺少统一 retrieval span/覆盖指标 |
| 延迟/成本 | 局部模型调用计数和运行时间 | 尚未全链路采集 token、工具成本和关键路径 |
| badcase | 日志、文档、`evals/inventory_review.py` | 库存场景支持待审核反馈→人工确认→新版案例及版本对照；尚未形成线上自动采样和完整 Badcase 生命周期 |
| 发布门禁 | pytest 和人工检查 | 尚未统一质量、成本、延迟阈值的 release gate |

因此，现有测试通过只证明当前测试覆盖的行为；不能据此声称已实现跨层观测、完整质量指标或线上自动 badcase 回流。

库存场景的已实施范围、真实模型结果和限制见 [V2 实施报告](inventory-evaluation-v2.md)。该工具使用保存的应用结果与阶段事件，不改变本章中生产 trace、检索评测和发布门禁仍未实现的边界。

## 10. 建议落地顺序

1. 固定 `TraceContext`、`SpanEvent`、`ArtifactRef`、`MetricPoint`、`EvalCase`、`EvalRun` 和 `BadcaseRecord` schema。
2. 在 Runtime 建立 trace/span 生命周期，先覆盖输入、路由、工具、生成和持久化；所有事件绑定 task/request/revision。
3. 接入 token、模型、网页、embedding、工具和预算账本，计算端到端延迟/成本分解。
4. 为输入/理解/检索/决策/生成各建最小质量指标和脱敏回放样本。
5. 建立 badcase 分类、人工标注、回归集和 `EvalReport`，把高严重度门禁接入发布流程。
6. 再扩展 OpenTelemetry 后端、尾部采样、在线质量抽样和跨版本 dashboard；不要先采集海量无关联日志。

最低验收条件是：任意最终回答都能沿 `trace_id` 追溯到输入、检索、决策和生成；每个关键外部调用都有延迟、状态、版本和成本字段；质量指标有数据集/真值来源；badcase 可定位、修复、回归和阻断发布；敏感原始内容遵守脱敏、访问和保留策略。

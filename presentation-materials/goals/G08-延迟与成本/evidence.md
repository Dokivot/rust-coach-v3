# G08 证据索引与核验边界

整理日期：2026-11-05。业务版本 `932f7dea82cc27e16b0e3fe25578f98b6d199d1f`，本轮材料写入前工作区干净。此前业务改动已提交，不能沿用早期“未提交业务版本”的描述。文件hash、报告来源和实际复跑记录见[版本清单](assets/source-manifest.json)及[核验记录](assets/verification.txt)。

事实分为当前实现、当前隔离测试、已保存历史报告与未来待采。聊天只解释范围，不能单独证明实现。本文证据编号只在G08内有效。

<a id="e01"></a>
## E01 — 可见入口与实施范围

**结论**：回答下能按需查看处理耗时与调用；全局诊断包含未形成回答的运行。本轮增加计量与诊断，没有新增整次请求预算，也没有证明速度或账单改善。

来源：[实施说明](../../../docs/latency-cost-implementation.md)、[聊天整合](../../../src/rust_coach/runtime.py)、[API及页面入口](../../../src/rust_coach/app.py)、[聊天UI](../../../src/rust_coach/static/app.js)、[诊断UI](../../../src/rust_coach/static/performance.js)。

核对要点：会话接口按request_id读取运行；全局接口默认50、最多100条，返回total_runs与truncated，汇总仅覆盖返回窗口。页面按需读取且可刷新后台状态，错误和不完整记录显式提示。实现以文本展示数据。阶段进度不是token流，也不是首个有效答案指标。

这些为源码与DOM／API测试证据，没有真实浏览器验收或演示截图。代码中的 `shared_request_budget=false` 表示本功能没有引入共享控制，不表示整个应用从来没有局部限制。

<a id="e02"></a>
## E02 — 计量语义、成本与持久化

**结论**：记录实际包围的阶段及每次模型入口尝试，并保留未知值与不完整性；外层工具次数不能代替模型次数。

来源：[计量核心](../../../src/rust_coach/performance.py)、[生产适配器边界](../../../src/rust_coach/llm.py)、[网页请求](../../../src/rust_coach/knowledge/web.py)、[后台记忆](../../../src/rust_coach/memory/service.py)、[旧事件修正](../../../src/rust_coach/architecture/observability.py)。

| 口径 | 核对结果 |
|---|---|
| 运行与阶段 | 每次尝试独立运行；阶段记录父ID、偏移、起止及状态。单调时钟计时，UTC用于关联。父阶段包含子阶段，不能累计；运行耗时在最终计量落盘前结束 |
| 模型调用 | 每次complete入口算一次；预检失败可能未进入网络。dispatched只表示到HTTP发送边界，injected标识测试注入，均不证明实际送达或收费 |
| 网页请求 | 逐次请求与重定向跳记录主机、方法和耗时；失败可能只有通用错误码，不能承诺完整HTTP错误诊断 |
| usage归一 | 输入／输出与prompt／completion别名归一；missing、partial、complete、invalid分开。负数、布尔值、类型错误、冲突、总量不一致及子集超总量会使数值无效 |
| 合计与费用 | 每字段保留known_sum、reported_calls、total_calls；没有已知值为null。complete仅指输入与输出可用，不保证所有细分字段齐全。cached与reasoning不是新增token总量。billed_cost为null |
| 无外部调用 | no_external_calls只描述返回记录中的调用；不等于硬件或整体服务费用为零，也不把超时、取消视为免费 |
| 后台归因 | 独立Context、独立运行，用原请求和parent_run_id关联；不计入前台运行，可能与前台重叠 |
| 持久化与故障 | SQLite保存开始与终态；会话删除级联清理其计量。写入故障触发脱敏告警，进程内标记持续为不完整，能写入时保存运行的不完整标志。完全不可用则查询失败，重启不能找回丢失记录 |
| 隐私与范围 | 计量不存提示原文、图像、回复、凭据或原始端点；保存长度、数量和端点hash等。无会话探测不随会话删除；无生产权限与自动保留期承诺 |

<a id="e03"></a>
## E03 — 本轮重新运行的定向测试

**结果**：`tests/test_performance.py` 27项通过，1.50秒；属于工程验证，测试总耗时不是用户响应时延。27项包含usage参数化的11个变体，也包含一次11场景评测调用，不把它们额外相加。

来源：[测试文件](../../../tests/test_performance.py)；实际命令输出见[核验记录](assets/verification.txt)。

| 覆盖边界 | 代表测试名称 |
|---|---|
| 起止、父子阶段、重放、失败、重启、删除 | test_real_chat_latency_replay_failure_restart_and_delete |
| 截图转拆墙、血量追问与有效复用 | test_screenshot_to_raid_followup_records_reuse_without_new_vision |
| 本地路径和视觉修复2次调用 | test_local_paths_and_schema_repair_are_counted_separately |
| 取消、繁忙、无回答仍有账目 | test_cancelled_request_and_busy_are_saved_without_turn |
| 后台绑定原请求并分开计数 | test_background_memory_has_own_run_and_original_request |
| 无效供应商响应保留usage、网页失败 | test_provider_usage_on_invalid_response_and_web_failure |
| 并行子阶段归属 | test_parallel_spans_share_parent_but_not_each_other |
| 记录失败、真实SQLite异常与脱敏告警 | test_recording_failure_is_explicit_and_does_not_replace_business_result；test_actual_database_recording_error_is_reported |
| 缺失usage、多次调用不受新增共享预算控制 | test_no_new_shared_budget_and_missing_usage_remains_unknown |
| 撤回后不能重放旧计算结果 | test_knowledge_revocation_blocks_replay_and_records_failure |
| 运行中状态、截断窗口、旧事件不伪造耗时 | test_running_records_and_limited_listing_are_not_complete_history；test_legacy_events_do_not_claim_business_duration |
| 建议计划修复和生成共3次调用 | test_advice_nested_calls_are_not_one_outer_tool |
| 探测、状态预览与本地计算独立运行 | test_probe_and_player_state_preview_are_separate_runs |

测试使用模型夹具、MockTransport和临时数据库；取消测试确实阻塞后取消任务。供应商响应测试并未访问真实供应商；探测测试也用合成模型。不能从通过数推导线上稳定性。

<a id="e04"></a>
## E04 — 已保存的33条合成接口评测

来源：[原始保存报告](../../../evals/results/performance-fixture.json)、[评测器](../../../evals/performance.py)。采集时间 `2026-11-05T01:59:17.530333+00:00`；模式 `synthetic_model_real_application_http_sse`。本轮核对报告中的源码、评测器与规则hash均匹配，没有将报告改写成新采样。

| 场景 | 每次模型尝试 | 通过／重复次数 |
|---|---:|---:|
| 首次截图制作 | 1 | 3/3 |
| 同图热复用 | 0 | 3/3 |
| 切换拆石墙 | 0 | 3/3 |
| 血量250追问 | 0 | 3/3 |
| 冷知识查询 | 0 | 3/3 |
| 热知识查询 | 0 | 3/3 |
| 本地拆墙命令 | 0 | 3/3 |
| 成功请求重放 | 0 | 3/3 |
| 模拟模型失败 | 1 | 3/3 |
| 同请求失败后重试 | 1 | 3/3 |
| 视觉结构修复 | 2 | 3/3 |

可重绘数据见[CSV](assets/scenario-summary.csv)，代表样本见[JSON摘录](assets/representative-records.json)。这些文件均为同一报告的派生素材，不是额外独立运行。

**真实执行了什么**：TestClient中的实际应用附件上传、HTTP/SSE、规则计算、保存及计量API；每次重复使用独立临时数据库，一次重复中的11个场景按顺序执行，历史会变化。当前定向测试又执行过一次相同11场景检查，但不是新的3次重复报告。

**固定了什么**：图像为320×180纯绿色PNG；视觉探测状态预设通过，未实际探测。模型每次固定等待5毫秒并返回硫磺100、目标火药10的合成观测。模拟超时是夹具抛出model_timeout，不是等待真实超时阈值耗尽。没有网络供应商调用或收费。

**断言到哪里**：终态符合预期、模型次数、无供应商发送、计量完整、无共享预算；图像场景检查calculated及目标／库存参数，拆墙检查计算状态和operation，续问另查hp=250，知识场景检查supported_snapshot。没有独立核对每个数值输出或任意建议质量，不能把33/33称为总体正确率。

**性能边界**：冷／热是应用缓存状态，不是供应商缓存；不是控制历史等变量的优化前后实验。每场景仅3次，p95为null。报告包含本地HTTP和服务端毫秒数，但HTTP计时不含上传和探测，更不含真实浏览器上传到渲染；这些数值不用于提速标题。

原文件是精简投影，保存关键检查、outcome、观测与模型调用，不是全量最终回答或完整span父子时间树。完整计量结构由E02代码和E03测试核验，不从此投影凭空补造。

<a id="e05"></a>
## E05 — 页面验证与历史全量检查的区别

本轮命令 `node --test tests/test_chat_ui.cjs tests/test_performance_ui.cjs`：20项通过，0失败。来源：[聊天DOM测试](../../../tests/test_chat_ui.cjs)、[诊断DOM测试](../../../tests/test_performance_ui.cjs)、[本轮日志](assets/verification.txt)。覆盖详情绑定原请求、不完整提示、失败／未完成记录、查询错误及以惰性文本展示；20项也包含原有聊天交互测试，不全是本轮新增功能。

[实施说明](../../../docs/latency-cost-implementation.md)记载上一轮全量Python684项通过、DOM20项通过。本轮没有重跑684项，27项包含在其测试范围中，不能说711项，也不能把历史20项加到本轮20项成为40个独立场景。历史全量结论引用文档记录；本材料另保存当前定向命令原始输出，不冒充有历史全量原始日志。

<a id="e06"></a>
## E06 — 待补证据与可靠演示建议

| 待验证主张 | 所需材料或采样 | 当前处理 |
|---|---|---|
| 玩家等待可接受 | 固定环境、模型和版本，记录首次截图／有效复用／拆墙／失败的浏览器提交、上传结束、首条进度与最终答案时间 | 没有硬指标；建议先采基线，再共同定场景目标 |
| 真实调用费用可控 | 同一批请求的模型版本、供应商usage、价格版本与账单或对账记录 | 费用保持未知，不索取密钥或私密凭据 |
| 缓存或模型更换带来收益 | 同条件对照、固定输入和质量标准，包含失败与重复采样 | 复用是已有能力；本轮无收益百分比 |
| 当前真实视觉质量 | 已确认标签对应的真实截图及新样本，用当前业务版本验证 | 既有人工标注沿用，未知项仍未知；不增加专家审核要求 |

可补一段当前页面的“截图制作→拆墙→250血→展开调用详情”录屏，保留失败或待确认状态。用户现有标注无需重复确认；本次材料交付不依赖用户再提供资料。没有同条件真实样本前，不给出看似精确的响应SLA或预算。

## 复现与材料一致性

按[素材清单与复现说明](assets/README.md)运行离线检查，无需密钥或付费调用。图与brief中的Mermaid保持一致；CSV与代表记录从原报告派生；源文件和非材料文件范围检查记录在[verification.txt](assets/verification.txt)。若后续业务或判定器hash变化，先将本包标为需复核，再重采或明确保留历史身份。

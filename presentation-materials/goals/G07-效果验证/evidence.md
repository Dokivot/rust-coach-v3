# G07 证据与复现索引

> 2026-11-05版本适用性补记：G01/G05之后更新了任务路由、库存资格与状态失效链路。本文件的真实图像批次及原判定保持历史属性，需复核对当前业务版本的适用性；不能当作当前版本已重新验收。当前版本与检查见[总索引](../../README.md)及[G01证据](../G01-意图理解/evidence.md)。

整理日期：2026-11-05。以下把**已有真实模型批次**、**本次本地核验**、**设计记录**和**待验证结论**分开。正文中的证据编号在本文件内稳定使用。

## 日期与版本状态

真实运行捕获时的 Git HEAD 为 `aca007d`，当时未提交实现的完整文件哈希保存在[历史版本清单](assets/source-manifest.json)；实现随后进入 `99f1a7d`。当前审查修复位于该提交之上的工作树，未修改历史捕获或模型输出，最新软件验证见[深度审查](../../../docs/inventory-evaluation-audit-2026-11-05.md)。

真实新图批次于北京时间 2026-11-05 04:33:42—04:39:25 执行，主／视觉模型均为当时配置的 `gpt-5.6-sol`，Responses 接口；每案例一次，共9次运行。捕获报告与最终重判的判定器哈希不同，因为报告在评测工具完善期间捕获，最终结论由完成后的判定器重判并合并复核。当前审查又增加了报告完整性防护，因此源码哈希再次变化；真实结果回归逐项确认9个 assessment 未变，不伪称最初捕获时已使用当前工具。

本次没有重跑真实模型实验；执行本地软件回归并重新计算保存输出，证据见 [核验记录](assets/verification.txt)。图片标注已保存在版本化案例中，依据为用户确认；最终表达复核身份是 `coding_agent`，并非独立人工盲评。

## 关键证据表

| 证据编号 | 支持的结论 | 来源或素材路径 | 验证条件 | 实际结果 | 适用范围与限制 |
|---|---|---|---|---|---|
| G07-E01 | 真实应用链路能执行并留存可复核结果 | [运行明细](assets/evaluation-runs.csv)、[图表数据](assets/chart-data.json)、[原始捕获](../../../evals/results/inventory-acceptance-v2-live-2026-11-05.json)、[最终判定](../../../evals/results/inventory-acceptance-v2-reviewed-2026-11-05.json) | 既有新图批次，独立临时数据库，真实模型与 ASGI HTTP/SSE | 9案例、12轮、19次调用，9次执行完成；最终5失败、3待核验、1通过 | 非浏览器验收；仅一次运行／案例；这不是总体准确率 |
| G07-E02 | 能检出“错误库存前提下算术自洽”的回答 | [零件图](assets/inventory-components.png)、[代表案例](assets/representative-cases.json)、[人工标注](../../../evals/inventory-acceptance-v2.json) | `components_craft`，第0轮；普通工作台，不展开燃油 | 齿轮20→电子零件20、电子零件2→炸药2；先错在视觉，后影响缺口 | 证明本例定位能力，不证明已修复识图；局部库存与规则版本仍有限制 |
| G07-E03 | 能区分潜在视觉错误与被隔离的预算影响 | [历史图案例](assets/representative-cases.json)、[最终判定](../../../evals/results/inventory-acceptance-v2-reviewed-2026-11-05.json)、[图表数据](assets/chart-data.json) | 历史图、对手图、31分钟过期观测三例；只查对应库存隔离断言 | 3个隔离断言通过；历史、对手图仍保留视觉失败；未知HP另有1个正确澄清案例 | 3/3是隔离检查，不是3个案例整体通过；31分钟由隔离数据库受控置旧，非实际等待 |
| G07-E04 | 算术、图像事实和规则版本被分别核对 | [独立规则证据](../../../evals/results/inventory-rule-evidence-2026-11-05.json)、[最终判定](../../../evals/results/inventory-acceptance-v2-reviewed-2026-11-05.json)、[图表数据](assets/chart-data.json) | 独立Wiki配方手算表；十个C4、工作台火药、不展开燃油 | 8个制作结果按**实际采用库存**计算均匹配；规则build继续待核验 | 8/8不能证明库存正确或当前游戏规则正确；非通用配方求解金标 |
| G07-E05 | 评测边界处理有软件回归支撑 | [历史测试记录](assets/verification.txt)、[当前深度审查](../../../docs/inventory-evaluation-audit-2026-11-05.md) | 审查工作树；本地pytest与离线重判，未启动真实模型批次 | 审查检查点521项通过；当前评测专项34项通过；9案例重判仍为5失败、3待核验、1通过 | 软件测试覆盖范围内通过；不等于建议质量提升，不是游戏真实效果样本 |
| G07-E06 | 保存结果可复核；不兼容对照不会认证模型改进 | [本次核验](assets/verification.txt)、[历史复现报告](../../../evals/results/inventory-v2-reproducibility-2026-11-05.json)、[不兼容对照](../../../evals/results/inventory-grader-v1-v2-comparison-2026-11-05.json) | 本次用当前判定器重算同一捕获并合并既有复核；没有新模型调用 | 9个最终assessment与保存结果一致；历史对照标记判定器／复核协议不兼容 | 只证明重判一致；历史复现报告的一个副本位于未版本化data目录，本材料不依赖那个副本 |
| G07-E07 | 实际交付是小型评测闭环，非完整生产平台 | [优先级规划](../../../docs/improvement-priority-plan.md)、[评测架构设计](../../../docs/architecture-evaluation-observability-layer.md)、[实施报告](../../../docs/inventory-evaluation-v2.md) | 将规划、当前代码与落地结果交叉核对 | 本地隔离执行、版本化报告、显式复核、人工审核反馈已实现；统一生产追踪等仍是差距 | 文档设计不单独作为实现证据；替代方案对比和停止边界中本次归纳已在brief标明 |
| G07-E08 | 标注、复核和预算失效不会静默伪装成功 | [专项测试源码](../../../tests/test_inventory_evaluation.py)、[本次全量测试](assets/verification.txt)、[评测入口](../../../evals/README.md) | 合成缺输出／不确定标注／哈希冲突／0次预算／在途超时等关键场景 | 测试确认阻断或待核验、拒绝冲突复核、取消在途调用、剩余案例not_run；反馈须人工核实后生成新版本 | 测试机制有效，不等于已在生产中长期验证自动反馈闭环 |
| G07-E09 | 旧新判定差异来自检查范围与复核，不能算效果收益 | [旧捕获](../../../evals/results/inventory-baseline-live-2026-11-05.json)、[新版重判](../../../evals/results/inventory-baseline-v2-reviewed-2026-11-05.json)、[对照原因](../../../evals/results/inventory-grader-v1-v2-comparison-2026-11-05.json) | 同一旧批次16次运行；模型输出没有重生成；判定器和复核条件不同 | 原自动判定5失败／11待核验；新版加复核后8失败／6待核验／2通过 | 禁止画成“模型优化前后”收益图；新旧图片批次的样本也不同 |

## 定位证据

<a id="g07-e01"></a>
### G07-E01：批次范围与执行结果

原报告按 `runs` 保存案例，按 `steps` 保存轮次，按 `model_calls` 保存调用。素材中的三张CSV分别保留9、12、19行，不能混用分母。最终状态已合并逐轮复核；原捕获时的自动判定是5失败、4待核验，不能与最终5失败、3待核验、1通过混称同一口径。

源码核验：[隔离执行与捕获](../../../evals/inventory_baseline.py#L138) 的 `run_case`、[批次执行](../../../evals/inventory_baseline.py#L219) 的 `live`。函数名和文件哈希共同定位。

<a id="g07-e02"></a>
### G07-E02：主讲案例的完整证据链

`components_craft` 的人工库存为布40、齿轮20、电子零件2。正确可见库存下仍需：电子零件18、布10、金属碎片2000、低品质燃油600、硫磺22000、木炭30000。实际回答为金属碎片1980、燃油594、硫磺21780、木炭29700、布10，未列电子零件缺口。

这些期望值来自已保存的 `recipe_arithmetic.expected`，不是本次凭印象重新编写；回答全文、识别物品、实际计算输入及原报告定位均在 [样例摘录](assets/representative-cases.json)。数量单位是游戏对应资源单位，燃油这里不是截图液体容器的毫升数。模型识别错的库存也能通过 `arithmetic_given_admitted_inventory`，所以不能拿那一项通过覆盖视觉失败。

源码核验：[已知格子比对与首个分歧](../../../evals/inventory_grading.py#L51) 的 `visual_comparison`、`finalize`；[逐轮判定](../../../evals/inventory_grading.py#L106) 的 `score_case`。首错是按轮次与检查序列／依赖定位，附工作流事件引用；没有完整阶段耗时或因果干预分析。

<a id="g07-e03"></a>
### G07-E03：同图历史声明与缺条件澄清

`components_historical` 的回答全文保留在样例中：明确“库存未知”，只列总需求，注明截图未用于抵扣；它的整体状态仍是失败，因为视觉检查存在错误。`mixed_opponent` 和 `mixed_expired` 的对应隔离断言也通过。过期测试只将该案例的图像观测时间调到31分钟前，记录修改前后值，不更改系统时间或玩家真实数据库。

`damaged_unknown_hp` 的真实回答为“暂不能给出可靠计算：受损石墙需要提供当前生命值；不知道剩余血量时无法计算。”逐字文本也保存在样例的 `assistant_verbatim`；该例在表达复核后通过，证明的是必要澄清处理。

源码核验：[受控置旧](../../../evals/inventory_baseline.py#L121) 的 `expire_observation`；[状态隔离检查](../../../evals/inventory_grading.py#L106) 的 `score_case`；[潜在错误隔离测试](../../../tests/test_inventory_evaluation.py#L60)。

<a id="g07-e04"></a>
### G07-E04：独立规则与未知版本

三条配方来源为 Facepunch Wiki 的 C4、炸药、火药页面，原始抓取证据与哈希保存在 [规则证据](../../../evals/results/inventory-rule-evidence-2026-11-05.json)。评测手算不导入生产规则目录或 Calculator；应用计算器与判定器职责分开。

源码核验：[独立材料表](../../../evals/inventory_grading.py#L13) 的 `independently_calculated_materials`。当前仅接受固定的十个C4配方范围；包含中间材料抵扣。游戏build未知、石墙HP与伤害规则未认证，禁止把“符合这张表”表述为“当前游戏一定正确”。

<a id="g07-e05"></a>
### G07-E05：当前软件回归

[历史核验文本](assets/verification.txt) 保存材料打包时的517项结果与源码哈希；[当前深度审查](../../../docs/inventory-evaluation-audit-2026-11-05.md)记录修复检查点的521项全量结果和当前34项评测专项结果。两者都只是软件测试，不代表同等数量的真实游戏问题答对。

关键回归位置：[标注不确定性传播](../../../tests/test_inventory_evaluation.py#L34)、[缺计算阻断](../../../tests/test_inventory_evaluation.py#L45)、[先前轮次错误](../../../tests/test_inventory_evaluation.py#L52)、[合理澄清复核](../../../tests/test_inventory_evaluation.py#L102)。

<a id="g07-e06"></a>
### G07-E06：重判、复核与可比性

本次从版本化原捕获重新计算9个案例，并按回答哈希合并已有复核，逐项比较最终判定，全部一致。该检查不依赖 `data/` 中历史重判副本，不向服务商发送请求。历史复现报告仍可作为背景，但其文件一致性结论不冒充本次重新做了相同的文件比较。

源码核验：[复核绑定与合并](../../../evals/inventory_evidence.py#L100) 的 `apply_reviews`、[可比性条件](../../../evals/inventory_evidence.py#L143) 的 `compare_reports`。检查数据集、判定器与源码哈希、模型配置、规则、图片、案例选择及复核协议；不会自动宣布模型改善。表达复核身份与逐条理由见 [已保存复核](../../../evals/results/inventory-acceptance-v2-review-2026-11-05.json)。

<a id="g07-e07"></a>
### G07-E07：设计与落地边界

[优先级规划](../../../docs/improvement-priority-plan.md) 第5节包含24例、浏览器验收及约90%完成率的建议；[评测架构设计](../../../docs/architecture-evaluation-observability-layer.md) 第9节明确现状差距；[实施报告](../../../docs/inventory-evaluation-v2.md) 记录实际交付与限制。三者交叉阅读，不能把规划指标当作已测成果。

反馈有候选、人工审核和新版本输出，没有线上自动收集→修复→发布的完整系统。源码核验：[审核后晋升](../../../evals/inventory_evidence.py#L167) 的 `promote_feedback`、[显式CLI入口](../../../evals/inventory_review.py#L15)。

<a id="g07-e08"></a>
### G07-E08：关键失败场景

源码核验：[调用预算](../../../evals/inventory_baseline.py#L75) 的 `RunBudget`、[调用前预算停止测试](../../../tests/test_inventory_evaluation.py#L203)、[取消在途调用测试](../../../tests/test_inventory_evaluation.py#L223)、[复核冲突测试](../../../tests/test_inventory_evaluation.py#L118)、[反馈审核测试](../../../tests/test_inventory_evaluation.py#L151)。

0次调用预算下，测试模型不可被调用，当前案例记录停止，剩余案例为未运行；时间预算下在途调用被取消。预算控制的是调用次数与墙钟时间，费用未知时保持空值，不能声称已做美元级预算控制。

<a id="g07-e09"></a>
### G07-E09：历史比较的解释

旧基线16次运行与新版重判使用同一批模型输出，但评分条件变化；新增三张图的9次运行则是不同输入。前者只适合解释判定覆盖变化，后者只适合描述新样本中的表现。两者都不支持“业务改进使准确率提高”的图表。

## 绘图数据与统计口径

优先用 [chart-data.json](assets/chart-data.json) 生成计数图，使用 [evaluation-runs.csv](assets/evaluation-runs.csv) 核对每个案例。对外建议展示“执行完成9”与“失败5／待核验3／通过1”两个不同维度，避免用一个成功率掩盖问题。

[evaluation-turns.csv](assets/evaluation-turns.csv) 给出12轮聊天HTTP耗时，单位秒；p50=20.848、p95=91.322，按升序后 `ceil(n×p)-1` 取值，排除视觉探测及会话初始化。它是小样本请求耗时，不是完整用户等待时间，也不能证明性能优化。

[model-calls.csv](assets/model-calls.csv) 给出19次历史调用的角色、状态、耗时和usage；轮次为-1的是视觉探测。输入token合计132267、输出15245，总计147512。调用的延迟与对话轮次延迟存在包含关系，不相加；token不是费用，账单金额未知。

截图含义、来源和使用注意见 [素材清单](assets/README.md)。所有素材为本地相对链接，重新排版时不得把库存输入图标成“评测平台UI截图”。

## 可复现演示

见 [演示步骤与前置条件](assets/demo.md)。默认使用已经保存的案例、回答和离线重判；不要求API密钥，不依赖临时目录中的历史报告。现场如选择重新调用模型，须明确属于新实验并单独记录条件，本轮没有执行该操作。

## 补采清单

| 缺失证据 | 如何获取 | 影响的展示结论 | 是否阻塞本材料包 |
|---|---|---|---|
| 目标游戏build及同版本配方／伤害认证 | 用户给出PC原版目标版本；保存可追溯官方说明或受控游戏测试记录 | “当前游戏数值正确”、拆墙最优结论 | 不阻塞；必须继续写待核验 |
| 独立人工盲评 | 请熟悉Rust的另一位评审按固定标准复核原回答，保存身份类别与逐项理由 | 表达与建议质量的独立可信度 | 不阻塞；现为代码代理复核 |
| 业务修复后的同条件未见图对照 | 新增未参与调试的独立图源，冻结模型／规则／判定器后执行前后版本并重复采样 | “识图更准”“建议质量提升”“稳定泛化” | 不阻塞；这些成果当前不能写 |
| 浏览器上传到回答的录屏／截图 | 在指定版本运行一次真实UI流程，记录输入与时间，并保留失败场景 | “浏览器端演示验收完成” | 不阻塞；现仅HTTP/SSE链路已验 |
| 供应商账单与完整等待时间 | 对齐请求ID核对计费，采集包含探测／排队的完整时序 | 每次任务成本、完整体验延迟或SLA | 仅补充材料；不影响本轮正确性诊断 |

需要用户补充的优先材料是目标游戏版本、独立评审人及未来未见库存图。没有这些材料，也可以据实演示现有诊断闭环；不需要用新的成功案例掩盖已保存失败。

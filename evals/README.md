# 库存与拆墙诊断基线

## 延迟与调用计量回归

`uv run python -m evals.performance --repetitions 3` 使用合成模型和临时数据库，经过真实上传、HTTP/SSE及计量接口，核对11类场景的结果与调用数。标准输出为JSON，不覆盖报告。包含冷/热复用、拆墙追问、失败重试和结构修复；不是实测模型延迟或账单。未新增在线共享预算，完整口径见 [实施说明](../docs/latency-cost-implementation.md)。

## 固定知识检索与时效评测

`uv run python -m evals.knowledge --output data/knowledge-evaluation-new.json` 运行24个固定案例，比较既有粗主题谓词的本地适配、加资格过滤、精确查询加FTS5三个策略。输出文件拒绝覆盖，保留语料/代码/规则hash和逐例判断。16个开发案例、8个首轮运行前冻结的预留案例共享规则语料，不是来源独立的盲测。对照不调用网络或模型，不代表旧版真实在线行为或游戏知识正确率。完整结果与边界见 [知识实施报告](../docs/knowledge-implementation.md)。

## V2：可追溯的评测闭环

当前入口继续复用原应用的上传、视觉探测、SSE 和隔离数据库。V2 增加证据前提、逐轮检查、复核合并、版本对照和预算停止；没有修改 Coach 的业务模型、提示词或计算器。实施结果与演示步骤见 [V2 实施报告](../docs/inventory-evaluation-v2.md)。下文旧基线说明保留历史背景，V2 的状态与归因规则以本节为准。

### 运行、重判和复核

```sh
uv run python evals/inventory_baseline.py --live \
  --cases evals/inventory-acceptance-v2.json --repeat 1 \
  --max-model-calls 40 --max-seconds 900 \
  --output data/inventory-evaluation-v2/demo-live.json \
  --portable-output evals/results/demo-live.json

uv run python evals/inventory_review.py \
  --review-template evals/results/demo-live.json \
  --output data/inventory-evaluation-v2/demo-review.json

uv run python evals/inventory_baseline.py \
  --cases evals/inventory-acceptance-v2.json \
  --replay evals/results/demo-live.json \
  --review data/inventory-evaluation-v2/demo-review.json \
  --output evals/results/demo-reviewed.json
```

复核模板默认 `pending`。阅读每一轮原始回答及相关证据后填写 `status`、`reviewer`、`reviewer_kind`、`reason`、`findings`；`human` 与 `coding_agent` 必须如实区分。合理澄清可填写 `clarification_appropriate: true`，不必要追问填 `false`。引用必须落在该轮真实存在的字段内。文件哈希、轮次哈希、重复冲突或证据缺失会拒绝合并。复核只能补充表达和澄清判断，不能抹除自动检查已确认的上游错误；修订复核请写新文件并递增 `revision`。

`--replay` 只重判保存的输出，新模型调用为零；原报告的 `model_calls` 数组是历史捕获证据。报告保留旧判定及新判定，不改原始文件。所有命令拒绝覆盖已有输出。

### 状态与归因

- 执行状态：`completed`、`failed`、`incomplete`、`stopped_budget`、`not_run`；质量状态单独记录 `pass`、`fail`、`pending`、`partial`、`not_run`。
- 检查状态 `blocked` 表示缺少计算或观测前提；缺输出不能通过“未使用无效库存”。缺标注、缺版本证据会让相关检查保持 `pending`。
- 已知格子的确定性错认是 `fail`；保守省略已知物品是 `partial`，另报覆盖损失。未知格子不编造金标；额外数值观测无法核验时，其下游抵扣仍待核实。
- 每轮均检查回答与图像证据。旧图、他人图、过期图的库存隔离与视觉正确性分别判断，潜在错误被库存门禁拦住时记录 `blocked_by_scope_gate`。
- 首错按轮次及工作流依赖寻找最早有证据的分歧，保留具体字段和阶段事件引用。依赖失败列入 `upstream_failures`，不把后续缺口错误重复宣称为独立根因；这不是经干预实验证明的因果根因。
- 独立手算表仅覆盖十个 C4、普通工作台火药、不展开燃油，支持库存中的 C4／炸药／火药抵扣。它不调用生产计算器。当前游戏 build 与拆墙规则未独立验证，数值匹配仍不能宣称当前版本正确。

### 版本、分组与对照

```sh
uv run python evals/inventory_review.py \
  --compare evals/results/demo-reviewed.json evals/results/another-reviewed.json \
  --output evals/results/demo-comparison.json
```

对照要求相同数据集、判定器版本及源码哈希、模型配置、规则、图片、案例与重复次数，以及一致的复核协议。不同业务源码允许对照，并由原报告保留源码清单。条件不兼容时给出原因，仍可查看描述性差异，但不宣称改进。相同保存输出的重判标记为 `saved_output_regrading`，真实执行标记为 `live_repeated_execution`；模型进步不会由一次状态变化自动认证。

新实时报告使用 `evaluation-configuration-v2` 冻结模型、端点哈希、API 模式、指令角色、输出上限、视觉 detail、上下文窗口、超时及附件限制，并保存计划案例、重复次数和运行数。重判拒绝重复运行、案例定义错配和新格式清单缺失；旧捕获没有显式计划清单时只校验运行身份与唯一性，并在结果中保留 provenance limitation，不能倒推为完整实验清单。

三张新图及其标注已获本会话用户确认。宽图与旧截图同源，属于诊断组；另两张图按独立场景分组，所有历史／对手／多轮变体跟随原图，不能跨调试集与验收集。`acceptance_first_run` 表示本轮首次真实测试，测试后已成为已见样本，未来修改还需要新的保留样本。

### 反馈进入候选集

```sh
uv run python evals/inventory_review.py \
  --feedback-template evals/inventory-acceptance-v2.json --image mixed \
  --output data/inventory-evaluation-v2/feedback.json

uv run python evals/inventory_review.py \
  --promote-feedback data/inventory-evaluation-v2/feedback.json \
  --cases evals/inventory-acceptance-v2.json \
  --output evals/inventory-reviewed-next.json
```

第二条命令会拒绝未经审核的候选。只有人工核实后，填写 `status: approved`、`reviewer_kind: human`、审核人、来源证据和新的数据集版本，才可生成新版案例。输出放在 `evals/` 以保持图片相对路径有效。原数据集、运行结果和玩家状态均不会被更新。

预算在每次模型调用前检查，包含视觉探测与工作流内部调用；时间预算取消在途案例。预先建立完整运行清单，剩余案例明确为 `not_run`，不算正确拒答。调用量、可获得的 token、聊天 HTTP 延迟分位数有真实记录；没有供应商账单时费用为 `null`，不填零。硬预算覆盖调用次数与墙钟时间，不承诺美元费用上限。

```sh
uv run pytest -q tests/test_inventory_baseline.py tests/test_inventory_evaluation.py
```

测试覆盖判定前提、跨层不确定性、逐轮首错、保守覆盖、复核冲突、版本不兼容、反馈审核、图片哈希、真实 HTTP/SSE 驱动及两类预算中止。测试模型与真实模型评测分别报告。

本轮只增加评测材料和执行工具，保留当前业务行为。八个案例用于定位典型流程中的错误，不代表整体准确率，也不是修复后的保留验收集。

首批两轮真实结果及建议见[评测报告](../docs/inventory-baseline-results.md)，其中区分了错误确定性回答、保守识别造成的覆盖不足和仍待核验的规则。脚本正常退出只表示批次执行结束，不表示案例通过；请阅读自动检查和逐案例复核记录。

## 案例与独立依据

[案例定义](inventory-baseline.json)覆盖空库存文本计算、清晰截图、遮挡读数、历史图、对手图、过期观测、受损但血量未知、制作切换拆墙及血量追问。

- 原图保存在 [fixtures/inventory-original.png](fixtures/inventory-original.png)。用户在本次会话中确认第一行依次为木头 8147、石头 550、金属碎片 946、木炭 4、硫磺 140；第二行身份与数量未知。
- 遮挡图保存在 [fixtures/inventory-occluded.png](fixtures/inventory-occluded.png)。执行脚本根据案例定义从原图生成完全相同的变体；第五格图标和数量被固定矩形遮挡。这是“读不到时不要猜”的可重复测试，不代表自然模糊图的识别准确率。
- C4 材料金标由独立读取 Wiki 配方后手算得出，判定器不导入业务计算器或规则目录。抓取原文、时间和哈希见 [规则证据](results/inventory-rule-evidence-2026-11-05.json)。普通工作台火药每批产出十个，低品质燃油不展开。
- 十个 C4 总需求：电子零件 20、布 50、金属碎片 2000、低品质燃油 600、硫磺 22000、木炭 30000。抵扣人工确认的截图后，金属碎片缺口为 1054、硫磺 21860、木炭 29996，其余不变；木头和石头与该制作任务无关。
- 游戏 build 尚未独立核验。石墙 500 HP、C4 有效伤害 275 仍是待核验候选值，数值匹配不能算当前游戏版本正确。

## 真实运行

在项目根目录使用已有 `.env` 的主模型与视觉模型配置。命令会向配置的服务发送固定问题及上述图片，并产生模型调用费用；不会读取或修改玩家现有会话数据库。

```sh
uv run python evals/inventory_baseline.py --live --repeat 2 \
  --output data/inventory-baseline/live-new.json \
  --portable-output evals/results/inventory-baseline-live-new.json
```

使用 `--only text_craft clear_inventory` 可选择诊断案例。输出路径必须不存在，避免覆盖已完成实验。

每个案例、每次重复都有独立临时数据库，内含空记忆、空会话且不绑定玩家档案。多轮案例保留本案例的历史、图像草稿和库存来源，不跨案例复用。实际调用现有 FastAPI 上传、创建会话和 SSE 聊天接口，使用应用原有工作流和生产模型适配器；不启动浏览器，也不把本地 HTTP 调用称为浏览器验收。

每个含图案例先实际进行视觉接口探测。主模型、视觉模型和记忆模型分别记录，未替换为固定回复。输入、模型配置和超时保持一致，模型自身的随机性不保证逐字复现。过期案例仅将自己临时数据库中的图像观测时间设为当前时间前 31 分钟；写入前后值均记录，无需等待或更改系统时钟。

主报告在每次模型调用和每轮完成后原子保存。记录原始模型输入文本、回复、图片哈希、耗时、usage、HTTP 请求、SSE 阶段、最终回答、完整视觉和计算结果。图片数据本身不重复嵌入报告。便于共享的结果移除模型提示正文和冗长呈现区块，保留原始模型回复、计算和视觉证据。

## 固定结果复核

```sh
uv run python evals/inventory_baseline.py \
  --replay evals/results/inventory-baseline-live-2026-11-05.json \
  --output evals/results/inventory-baseline-replay-new.json
```

这是对已保存输出重新执行判定，不调用模型，也不重新执行应用。报告明确标记 `saved_output_rescore`。它只证明判定器对固定证据的结果可复现，不能证明真实模型稳定或业务代码重跑成功。案例文件哈希不一致时拒绝静默复核。

## 结果如何判定

每个检查记录预期、实际、通过／失败／待核实。原始回答需另行逐轮阅读，特别检查不必要拒答、重复追问、条件丢失、未经授权的库存扣减回执和不支持的确定性说法。自动检查不把这些语义问题默认为通过。

首错阶段使用意图、参数、截图识别、状态使用、规则适用性、计算、最终表达或外部服务分类。后续数值不一致可能只是同一个上游误识别的后果，不能重复算作多个独立根因。阶段顺序只是初筛，最终根据原始模型响应和实际流程复核。

“自动检查未失败”与“完整任务通过”分开：数值符合独立配方，但版本未知时仍保留待核验；发生已确认行为错误时即使还有其他未知项，也应记录失败。服务错误不算正确拒答。当前案例都参与诊断，后续修复必须另补未调试的新案例。

## 判定器回归

```sh
uv run pytest -q tests/test_inventory_baseline.py
```

测试覆盖独立材料表、误识别后的首错归因、过期库存使用、未知血量误套满血、外部调用失败、未完成多轮以及未核验金标不得自动通过。这些合成测试只验证评测工具，不是业务效果结果。

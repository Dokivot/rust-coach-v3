# 技术设计交付验收记录

> 历史记录：以下描述首次三图交付。最新交付已按用户要求精简为一张核心总览，图源、布局和验收结果以 [总览图验收](technical-architecture-overview-validation.md) 为准；旧 Mermaid 不再是总览 SVG 的生成源。

日期：2026-11-03 UTC（美国洛杉矶当地 2026-11-02）。

交付入口：[技术设计文档](technical-design.md)。本次修改限于 README、设计文档、Mermaid 源码、渲染配置和导出的 SVG；没有修改业务实现。

## 1. 审计基线与判定方法

最终源码基线：`8a32e00592593902555c05e4ee81017f3a8b58de`，提交说明为 `fix: restore screenshot deductions and reliable coach guidance`。调研期间工作树发生过业务更新，最终复核使用此基线，包含标准拆墙规则解析、截图库存抵扣与新手引导修复。

已实现以调用位置、条件分支、数据读写和测试为依据；已有设计文档中的目标能力保留为“已设计未实现”；缺少领域公式、数据或验收标准的新领域工具归为“待设计”。契约类型、函数名称和 unavailable 占位不构成运行能力证据。

主要核查路径：

| 事实 | 核查代码 | 图文处理 |
| --- | --- | --- |
| 实际聊天分支及计算回到聊天 | `workflows.build_chat_graph`、`calculations.service.CalculationService.run` | 当前图和流程图包含六种路由，以及 `chat=true` 返回普通对话 |
| 规则理解与局部模型抽取 | `architecture/input.py`、`architecture/understanding.py`、`calculations/requests.py`、`calculations/service.py` | 当前理解为 D；复杂计算参数抽取为 G；通用语义理解为橙色 |
| 视觉抽取和本轮库存 | `vision/service.py`、`vision/context.py`、`attachments/store.py` | VLM 候选与代码校验拆分；局部图不能当完整盘点；不会自动写长期档案 |
| 精确计算 | `calculations/engine.py`、`catalog.py`、`rules.json` | 模型提供参数；D Calculator 执行批次、DAG、抵扣、伤害阈值；不画 LLM 直接算数 |
| 两种建议子流程 | `advice/service.py`、`advice/onboarding.py`、`architecture/decision.py` | 普通建议先计划、新手先取证；GuideReview 仅属于新手链路；不是通用 Agent Loop |
| 当前网页取证 | `knowledge/web.py`、`knowledge/store.py` | 确定性检索/抓取、SQLite 正文快照；统一 EvidenceBundle、向量与关系检索为橙色 |
| 状态/记忆/上下文 | `runtime.py`、`player_state/`、`memory/`、`persistence.py` | scope、revision、TTL、偏好、成功轮次与后台记忆区分；主任务 checkpoint 为橙色 |
| 生成保护与观测局限 | `presentation/rendering.py`、`presentation/validation.py`、`architecture/observability.py` | AnswerSpec 当前为尾部审计投影；事件创建耗时不是业务 span；完整成本为橙色 |

目标依据主要来自既有六层 `architecture-*-layer.md`、`architecture.md`、`decision-knowledge-research.md`、`rust-domain-knowledge-research.md`；实现依据补充 `calculations.md`、`player-state.md`、`memory.md`、`advice.md`、`onboarding-guides.md`、`multimodal-implementation.md`、`persona-implementation.md` 和 `inventory-raid-fix.md`。具体可点击路径见主文档第 10 节。

## 2. 需求对照

| 要求 | 交付位置与检查结果 |
| --- | --- |
| 完整中文技术设计 | 主文档第 1—9 节覆盖概述、六层设计、流程、知识/计算、玩家状态/上下文、评测观测、差距和路线 |
| 三张必需图 | [目标图](technical-architecture-target.svg)、[当前图](technical-architecture-current.svg)、[执行流程](agent-execution-flow.svg)，每张均有独立 `.mmd` 源文件 |
| 六层职责 | 三份源码均包含输入、理解、知识、决策、生成、评测与观测六个带编号分组；共享服务不替代六层 |
| 类型和实现状态独立 | 所有业务节点分别赋予 D/G 类型和一个实现状态；S 存储采用圆柱。填充、边框、箭头三个维度独立 |
| 当前图只画真实实现 | 33 个业务/边界节点均为绿色实框；图例中的规划/待设计只是视觉说明 |
| 目标完整且状态透明 | 43 个业务/边界节点包含现有链路、语义理解、RAG/知识图谱、通用执行、持久澄清、生成门禁、完整观测、部署扩展及待设计领域边界 |
| 执行分支完整 | 31 个业务/边界节点呈现短路径、规则/模型参数、两类建议、引用校验、有界修复、目标补检索/重规划/持久澄清、错误出口 |
| 数据与控制方向 | 实线承载请求、状态、参数、检索结果、证据、计算结果、任务结果、回答和存储数据；虚线承载调用、分支、修复与恢复 |
| LLM 与工具分工 | G 规划/抽取/表达与 D 校验/执行/计算分开；所有精确数值来自 Calculator；外部模型适配为共享依赖 |
| 图文一致 | 主文档直接嵌入由同名 Mermaid 源生成的 SVG；同名服务、状态边界及数据载荷与正文一致 |
| 后续计划适度 | 先契约/观测基线，再知识发布/检索、持久决策和质量闭环；多用户生产化按需求推进，不新增多 Agent 或独立图数据库 |

节点数不含图例和六层分组。一个服务可按职责拆成多个视图，不意味着多个部署单元。玩家入口与接收出口、Runtime 接入与交付、SQLite 读取与归档均已在正文说明为同一对象的阶段视图。

## 3. 实际验证结果

| 检查 | 结果与范围 |
| --- | --- |
| Mermaid 语法/实际渲染 | 使用 Mermaid CLI **11.12.0** 和本机 Chrome，三张图分别真实生成 SVG，渲染进程均正常退出；不是仅调用不支持 flowchart 的 parser |
| 样式核查 | 已修正多 class 写法；类型与状态分别赋 class。脚本检查每个业务节点恰有一种类型、一种状态；当前图全部为 done |
| 六层与输出格式 | 自动检查三图的六个分组；SVG 可作为 XML 解析，包含节点、箭头、文字及图例 |
| 视觉核查 | 已查看 PNG 渲染，核对中文、节点、绿色/橙色/灰色边框、D/G 填色及图例；最终交付 SVG 可放大阅读 |
| 本地链接 | README、主文档及本记录的相对文件引用逐项检查，均指向现存文件 |
| Python 软件回归 | `uv run pytest -q`：**487 passed in 10.89s** |
| 前端 DOM 回归 | `node --test tests/test_chat_ui.cjs`：**16 passed，0 failed** |
| 补丁空白检查 | `git diff --check` 通过 |

完整图涉及跨层工具调用、返回值与重试，保留必要回边；建议单独打开 SVG 放大查看标签，而不是把整张图缩成单页截图。Mermaid 自动布局不保证所有连线完全无交叉。

这些回归证明当前测试覆盖范围内的软件行为，没有调用付费模型或以游戏实测认证规则；不据此宣称视觉准确率、证据语义正确率、玩家任务成功率或生产 SLA。规则快照的游戏 build 仍未核验。当前未接入的目标模块也没有因文档交付而变为已实现。

## 4. 复现与维护

渲染命令及配置见主文档第 10.2 节。Mermaid CLI 安装在临时工具目录，没有加入项目运行依赖或修改锁文件；使用自有浏览器时以 `-p` 指定 Puppeteer 配置。

后续源码变化时，先核对对应函数及分支，再更新实现状态和正文，最后用同一配置重新生成 SVG、复查图例和相对链接。新增能力只有接入真实执行链并具备验证证据，才从橙色转为绿色。

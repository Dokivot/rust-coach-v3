# Rust Coach v3

面向 Rust 生存游戏的教练 Agent 架构设计 v2：决策采用 Plan-and-Execute，知识获取采用包含向量语义检索和互联网实时搜索的 Agentic RAG；主图及子图均支持中途澄清和持久恢复。

- [完整架构设计](docs/architecture.md)：需求分析、模块边界、知识与计算、状态记忆、工作流、工程化、评测和成本。
- [代码生成就绪度评审](docs/codegen-readiness.md)：当前可生成范围、实施前缺口、图文歧义及建议补充顺序。
- [技术架构图 SVG](docs/architecture.svg)：标注数据流、关键职责及确定性/生成式组件。
- [架构图 PNG](docs/architecture.png)：便于直接预览和分享。
- [Mermaid 图源](docs/architecture.mmd)：可编辑的模块关系图。
- [Plan-and-Execute 流程](docs/plan-and-execute.mmd)：Planner、Executor、Replanner、验证与恢复。
- [Agentic RAG 流程](docs/agentic-rag.mmd)：来源选择、语义检索、实时搜索、正文验证、查询改写与中途澄清。
- [SVG 生成器](docs/render_architecture.py)：运行 `python3 docs/render_architecture.py` 重新生成架构图。

强计算只预留“拆建筑所需爆炸物”和“合成路径与资源缺口”两个接口，暂不实现或接入计算器网站。当前交付是设计文档与图，不包含已运行的 Agent、向量库或搜索服务；性能和评测目标待实施验证。

# G02 素材清单与使用口径

整理日期：2026-11-05。以下内容供重绘、摘录和核验，不是PPT设计规格或页面锁定文件。

| 文件 | 用途 | 使用边界 |
|---|---|---|
| [data-flow.mmd](data-flow.mmd) | G02业务数据流，可供重绘 | 与brief中的图保持一致；实线数据、虚线控制，不是线上监控截图 |
| [representative-io.json](representative-io.json) | 合成任务经真实应用HTTP/SSE的输入输出 | 禁止模型调用；无图像、无浏览器、无游戏内实测，不证明总体质量 |
| [capture-demo.py](capture-demo.py) | 在临时数据库复现采集 | 只输出安全摘录；运行入口见demo，不修改日常数据库 |
| [fixed-evaluation.json](fixed-evaluation.json) | 实施期历史24案例完整报告副本 | 原报告原样保留；代码/判定器后来有修订，不当作当前重跑 |
| [evaluation-cases.json](evaluation-cases.json) | 历史报告对应的固定输入、预期和故障 | 16开发、8预留，共用规则语料；不是独立盲测 |
| [evaluation-summary.csv](evaluation-summary.csv) | 三策略历史汇总，适合带限制的附录图表 | 案例数、字符、毫秒、调用次数，不是准确率或费用 |
| [evaluation-cases.csv](evaluation-cases.csv) | 72条策略×案例明细，可复算汇总 | 24问题乘3方法，不是72独立问题 |
| [current-evaluation.json](current-evaluation.json) · [current-summary.csv](current-summary.csv) | 当前审查标准的24例结果及绘图数据 | 与历史文件分开；当前B0/B1通过均为边界例，不能写成正确回答数 |
| [audit-evaluation.json](audit-evaluation.json) · [audit-cases.json](audit-cases.json) | 18条审查回归及对应输入 | 已用于诊断修复，无独立保留集或泛化声明 |
| [source-manifest.json](source-manifest.json) | 日期、HEAD、未提交状态与文件hash | 以具体hash识别业务版本；初始HEAD不能代表并行未提交修订 |
| [verification.txt](verification.txt) | 本次定向测试、源文件与素材检查结果 | 与历史618项全量记录分开，不累计重复运行 |
| [demo.md](demo.md) | 离线与可选截图演示步骤 | 未录制的浏览器流程只属于演示建议 |

G06复用本目录的完整报告、采集脚本、测试和版本清单，只保存与失效有关的摘录，不重复制造样本。后续需要真实UI图时另行采集；现有G07图片只能作为输入素材。

# G01 素材清单

更新：2026-11-05。默认展示已保存证据，不需要重新调用模型。与G05共用测试、版本和六轮文本冒烟；不是两套独立样本。

| 素材 | 含义与使用方法 | 限制 |
|---|---|---|
| [fixed-evaluation.json](fixed-evaluation.json) | 当前audited报告全部案例与断言的选择性导出 | 固定模型、合成图、40案例54轮；不是真实视觉效果 |
| [audited-live-examples.json](audited-live-examples.json) | 当前源码匹配的六轮文本冒烟业务字段、回答节选 | 临时合成档案；无视觉模型；不证明泛化 |
| [representative-cases.json](representative-cases.json) | 历史固定及历史真实文本摘录，保留原批次属性 | 历史源码；不冒充当前版本新运行 |
| [evaluation-counts.csv](evaluation-counts.csv) | 基线、历史实施版、审查版逐类计数，单位case，分母total_cases，轮数turns | 审查版判定器已变；仅前两批可同契约比较；不叠加样本 |
| [source-manifest.json](source-manifest.json) | HEAD＋dirty、源码/测试/报告/规则指纹及匹配状态 | 工作树源码哈希才能区别未提交实现 |
| [verification.txt](verification.txt) | 材料整理最终193项定向测试及报告核对记录 | 未重跑全量、DOM或浏览器；不证明建议质量 |
| [data-flow.mmd](data-flow.mmd) | 与G01 brief一致的Mermaid源图 | 实线数据，虚线控制；可按版面重绘 |
| [demo.md](demo.md) | 离线演示与真实UI补采步骤 | UI步骤是待执行计划，不是已完成录屏 |

可引用的既有输入图：[扩展库存图](../../G07-效果验证/assets/inventory-wide.png)、[零件图](../../G07-效果验证/assets/inventory-components.png)、[混合库存图](../../G07-效果验证/assets/inventory-mixed.png)。图像来源与标注范围见[G07素材清单](../../G07-效果验证/assets/README.md)。用户已确认的标注沿用，不确定项继续未知；这些图片不是本轮浏览器截图，也不证明当前识图正确。

将JSON回答重排成聊天卡片时，标“已保存输出的排版摘录”；历史样例加历史标签。没有保存用户数据库、密钥或供应商全量跟踪信息；原报告通过相对路径及哈希追溯。

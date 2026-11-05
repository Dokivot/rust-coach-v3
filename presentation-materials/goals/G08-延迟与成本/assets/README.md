# G08 素材与复现说明

本目录只有内容素材，不包含PPT、页面设计规格或正式逐页讲稿。所有示意、历史采样和本轮测试必须按来源区分。

| 文件 | 用途与范围 |
|---|---|
| [data-flow.mmd](data-flow.mmd) | 与brief完全相同的业务数据流，可供后续重绘；虚线表示条件或异步触发 |
| [scenario-summary.csv](scenario-summary.csv) | 原33条报告的调用对照，不带毫秒提速结论 |
| [representative-records.json](representative-records.json) | 同一历史报告首个重复中7种代表记录，保留采集时间与来源；无新采样声明 |
| [source-manifest.json](source-manifest.json) | 当前业务提交、源文件／规则／报告hash及材料范围 |
| [verification.txt](verification.txt) | 本轮定向与DOM命令输出、材料一致性检查；区分历史全量684项 |
| [原评测报告](../../../../evals/results/performance-fixture.json) | 11场景×3，精简投影；不含完整回答或所有span |

## 离线复现

从仓库根目录执行，使用项目依赖环境：

```sh
uv run pytest -q tests/test_performance.py
node --test tests/test_chat_ui.cjs tests/test_performance_ui.cjs
uv run python -m evals.performance --repetitions 3
```

本轮实际Python命令使用已安装环境 `.venv/bin/python -m pytest -q tests/test_performance.py`。独立评测命令仅输出JSON，不覆盖历史报告；本轮没有另跑并保存第二批3次重复。评测用临时数据库，固定模型及合成图片，无需供应商凭据；若使用Windows原生虚拟环境，按当地环境使用uv或对应的Scripts/python。

## 演示顺序建议（未执行的现场流程）

先展示离线调用对照并说明夹具身份，再在当前应用展示本地拆墙计算和调用详情。若已配置可用视觉模型，用可公开库存图演示制作、切换石墙、补250血，观察本次记录；真实结果可能澄清或失败，不预先承诺固定次数。最后打开 `/performance` 解释未完成、失败、后台关联和费用未知。

若现场演示重放，应复用同一请求标识和相同内容，不能把“再次键入相同文字”当作成功请求重放。知识发布已变更或被撤回时，旧结果可能拒绝重放，符合现有保护要求。故障与结构修复优先用隔离测试说明；不要污染日常数据库或假装失败样本是线上采集。

目前无当前浏览器截图／录屏，不制作假UI素材。图表建议只用调用次数和状态，所有页面应标注合成夹具、每场景3次与费用未知。

# G07 可复现演示素材

## 展示目的与前置条件

默认展示“如何判断与定位”，不展示未经验证的模型改进。使用已保存的真实批次，可以稳定讲解，避免把现场网络和模型波动混入机制说明。

只读展示只需打开本材料与项目中的JSON文件。离线重判还需要完整项目、匹配版本的评测文件与图片、Python依赖；首次安装可按项目说明执行 `uv sync --locked`。已安装后下列 `--no-sync` 命令不会主动同步依赖。运行目录必须是项目根目录，密钥不是离线重判的前置条件。

历史真实捕获对应 `aca007d` 加当时版本清单中的文件，实现随后进入 `99f1a7d`。当前审查修复位于该提交之上的工作树；演示前运行下方离线命令和专项测试，不要只用旧提交号推断结果。

## 建议演示顺序（约3—5分钟，可缩短）

1. 打开 [零件图](inventory-components.png)，说明人工标注的布40、齿轮20、电子零件2；强调局部截图。
2. 在 [代表样例](representative-cases.json) 搜索 `components_craft`。并排显示标注、模型观测、计算库存和 `assistant_verbatim`：模型错认后少算材料，但实际采用库存下的算术检查通过。首错指向视觉。
3. 搜索 `components_historical`。同图加“昨天”后，回答只列总需求，抵扣为空；整体视觉失败保留，库存隔离检查通过。这是两个不同问题的行为对照，不能称为修复前后。
4. 搜索 `damaged_unknown_hp`。血量未知时澄清，没有套用满血；解释为什么合理澄清也可以通过。
5. 打开 [9例明细](evaluation-runs.csv) 和 [图表数据](chart-data.json)，说明5失败、3待核验、1通过以及每个分母。再执行下面的重判／对照，说明“可复核”与“模型进步”不同。

## 离线重判与对照命令

在项目根目录运行，输出必须是新路径。以下固定文件名首次可用；重复演示时更换后缀，工具会拒绝覆盖。

```sh
PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python evals/inventory_baseline.py \
  --cases evals/inventory-acceptance-v2.json \
  --replay evals/results/inventory-acceptance-v2-live-2026-11-05.json \
  --review evals/results/inventory-acceptance-v2-review-2026-11-05.json \
  --output data/presentation-demo/g07-reviewed-demo.json

PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python evals/inventory_review.py \
  --compare evals/results/inventory-acceptance-v2-reviewed-2026-11-05.json \
            data/presentation-demo/g07-reviewed-demo.json \
  --output data/presentation-demo/g07-comparison-demo.json
```

匹配当前文件版本时，预期结果：第一份报告标记 `saved_output_rescore`，新模型调用为0，最终计数仍为5失败／3待核验／1通过；第二份报告 `comparable: true`，9对案例状态相同，`model_improvement_proven: false`。

首条命令的终端可能显示 `all_match: false`：它比较新自动判定与**原捕获时**的判定，不能用该字段判断最终复核报告是否复现。应查看最终报告和第二条兼容性／成对结果。这一细节放在讲稿或附录，不必放页面正文。

若出现数据集或复核哈希不匹配，先确认版本，不要修改原证据凑成功。若没有环境依赖，使用已保存材料讲解，并明确没有现场重新执行。

## 可选软件机制演示

```sh
PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q -p no:cacheprovider \
  tests/test_inventory_baseline.py tests/test_inventory_evaluation.py
```

这组测试包含预算停止、缺输出、复核冲突等合成场景。它们是软件行为证据，不能替代真实模型效果；不需要为了面试重新执行付费模型实验。

如后续选择真实浏览器或新模型实验，需另外记录运行版本、输入、时间与全部结果，说明费用和波动，不能继续沿用本材料中的19次调用与耗时作为新运行数字。本轮尚未补采这类证据。

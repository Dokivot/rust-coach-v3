# 截图制作与拆墙：共享演示方案

## 默认：直接展示已保存证据

不需要API密钥，不重新调用模型。先读[当前固定结果](fixed-evaluation.json)的 `image_inventory`、`image_profile_conflict`、`image_correction`、`health_followup`，说明这些是夹具输出；连续截图制作→拆墙→血量→纠正由[当前集成测试](../../../../tests/test_vision.py#L93)证明。再读[审查版真实文本样例](audited-live-examples.json)前三轮，说明实际模型使用的是合成档案库存，未看图。

讲解顺序：截图提供候选材料 → 明确制作目标 → 切换拆墙清掉旧目标 → 补250血保留墙目标 → 纠正单格生成新证据。不可把多份记录剪辑成一次真实浏览器成功记录。

## 可选离线复现

从仓库根目录执行，已安装锁定依赖；使用临时数据库和脚本化模型，不读写用户档案：

```sh
uv run python -m evals.intent_state --output data/intent-state-evaluation/demo-local.json
uv run pytest -q tests/test_intent_state_audit.py tests/test_intent_state.py tests/test_intent_state_evaluation.py tests/test_player_state.py tests/test_vision.py
```

预期当前固定40案例、54轮全部通过；当前所选测试193项通过。运行次数不增加独立样本数。新输出单独保存，勿覆盖本材料中的已审计报告。

## 待补：真实浏览器录屏

这是待执行补采方案，本轮未执行。真实文本/视觉模型会产生调用；先确认本地配置可用、视觉探测通过。应用启动与配置见[项目README](../../../../README.md)，档案操作见[状态文档](../../../../docs/player-state.md)。采用全新演示数据库文件，例如：

```sh
COACH_DATABASE_PATH=data/presentation-demo-20261105-new.sqlite3 COACH_SEARCH_PROVIDER=off uv run uvicorn rust_coach.app:create_app --factory --host 127.0.0.1 --port 8000
```

确认所选文件是专用演示库，不能接用户日常库。记录源码、规则快照、模型、截图哈希与运行时间；密钥不进入录屏。规则快照固定为 `rustdata-pc-vanilla-2026-11-01`，build未认证。

1. 打开聊天页，上传[已标注库存图](../../G07-效果验证/assets/inventory-wide.png)，问“只用截图，制作两个C4还缺多少”。不预承诺准确识别，展开来源和未知项。
2. 说“改成拆一面石墙最省资源”。观察旧制作目标清除；保留有效截图库存；结果说明比较新增硫磺、所列单一爆炸物方案，满血假设需显式说明。
3. 说“它还剩250血”。观察目标仍为石墙、HP为250；不声称实际游戏库存已消耗。
4. 若图中硫磺唯一且确实识别错误，按真实标注纠正；该原图已确认硫磺140，可说“硫磺数量改为140”。教学演示150只属于合成夹具，不改写原图金标。展示新草稿和保留的原始识别。
5. 新建独立案例验证来源冲突：在状态页建立并关联服务器/清档档案，按API文档显式选择档案容器，同时上传图。未明确来源时应澄清；再说“只用截图”或“只用档案背包”。主聊天页没有旧版容器设置表单，不要求点击不存在的控件。
6. 转到[G05补充演示](../../G05-玩家状态建模/assets/demo.md)，按时间选工作台或死亡/新会话案例。

## 失败记录

受损但不知道剩余血量时保留澄清；物品未知就保留未知；模型识错记为失败并演示纠正。状态期间变化就重试最新版本。保存实际输入、输出、终止事件与解释，不删掉失败轮，不用历史截图冒充当前验收。

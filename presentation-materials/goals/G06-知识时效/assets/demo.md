# G06 撤回、反馈与回退演示

先完成[G02共享演示](../../G02-知识与推理/assets/demo.md)中的配方、250HP计算和解释。以下命令只对该演示数据库执行；它们会持久写入撤回或发布记录，不在日常数据库演示。

## 反馈与撤回

展开解释的来源详情，选择实际引用的石墙依据，提交“其他资料数值不同，待核实”。通过下面的命令查看反馈：它应是待核实，而不是已经修订的游戏事实。

```sh
uv run python -m rust_coach.knowledge.manage --database data/knowledge-material-demo.sqlite3 feedback
uv run python -m rust_coach.knowledge.manage --database data/knowledge-material-demo.sqlite3 status
uv run python -m rust_coach.knowledge.manage --database data/knowledge-material-demo.sqlite3 revoke demo-knowledge-2026-11-01-v1 raid:building:base-wall:stone --reason '演示：目标记录待核实'
```

重新问解释，应明确缺少目标依据；重新执行同一 `/calc` 请求，应停止发布数值。HTTP使用原请求ID重放返回409，浏览器重新发送通常是新请求，因此不等同于重放测试。旧回答仍留在历史中，不能把“历史可见”说成“仍可采用”。

## 发布与回退

```sh
uv run python -m rust_coach.knowledge.manage --database data/knowledge-material-demo.sqlite3 export data/knowledge-material-candidate.json
```

在导出文件中使用新包ID，演示时可移除待核实的目标记录。不要改写冻结数值。随后安装，观察当前包未立即切换；使用安装返回的hash和明确说明启用。回退时指定原包ID及原包hash。

```sh
uv run python -m rust_coach.knowledge.manage --database data/knowledge-material-demo.sqlite3 install data/knowledge-material-candidate.json
uv run python -m rust_coach.knowledge.manage --database data/knowledge-material-demo.sqlite3 activate NEW_RELEASE_ID --sha256 HASH_FROM_INSTALL --review-note '已核对演示包范围；未认证游戏build'
```

当前内容指纹检查会保留相同内容与来源的撤回影响；仅改包ID或解释ID不是恢复证据的方式。回退原包也不会清掉撤回。它不等于自动辨认被改写内容是否仍然错误。完整的隔离复现已在[G02采集脚本](../../G02-知识与推理/assets/capture-demo.py)中，包括移除目标的候选与回退。

## 观察要点

- 正常查询显示采用范围，最新问题显示未认证，其他范围不采用数值。
- 反馈不改变当前发布引用；撤回增加登记修订号。
- 回答期间切包或撤回的失败由自动测试覆盖；浏览器手动演示很难稳定命中时序，不能假装录屏已覆盖。
- 开启另一份全新演示数据库可重新准备场景；无需清空或修改用户日常记录。

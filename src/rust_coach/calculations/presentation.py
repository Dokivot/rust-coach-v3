from rust_coach.calculations.catalog import RuleCatalog


def render_calculation(calculation, catalog: RuleCatalog):
    request, result = calculation["input"], calculation["result"]

    def name(item):
        return catalog.items[item]["name"]

    def quantities(values):
        return "、".join(f"{name(item)} × {amount}" for item, amount in values.items()) or "无"

    def craft_lines(plan):
        label = "尚需获取" if plan["inventory_known"] else "总需求（未提供库存，不能判断实际缺口）"
        if plan["inventory_known"] and not plan.get("inventory_complete",True):
            label = "抵扣截图可见材料后，尚需" if calculation.get("visual_usage") else "抵扣已知材料后，尚需"
        lines = [f"{label}：{quantities(plan['acquisition'])}",
                 f"已抵扣库存：{quantities(plan['inventory_used'])}",
                 f"保留未使用：{quantities(plan['reserved'])}",
                 f"本次制作余量：{quantities(plan['batch_leftovers'])}", "合成顺序："]
        for index, step in enumerate(plan["steps"], 1):
            lines.append(f"{index}. {quantities(step['ingredients'])} → {name(step['item'])} × {step['produced']}"
                         f"；ceil({step['missing']}/{step['batch_size']}) = {step['batches']} 批；工作台 {step['workbench']} 级。")
        if not plan["steps"]:
            lines.append("无需制作。")
        requirements = plan["requirements"]
        status = {"unknown": "尚未提供", "satisfied": "等级满足", "blocked": "等级不足", "not_needed": "无需工作台"}
        lines.append(f"所需工作台：{requirements['workbench']} 级；你的工作台：{status[requirements['workbench_status']]}。")
        if requirements["blueprints_to_check"]:
            lines.append("还需核实是否可制作（含默认配方，不等于都要研究）：" + "、".join(name(item) for item in requirements["blueprints_to_check"]) + "。")
        if requirements["mixing_table_to_check"]:
            lines.append("火药选择了混合台配方；需另行确认已有混合台。")
        return lines

    lines = []
    if calculation["operation"] == "raid":
        lines.append(f"计算条件：{result['target_name']} × {result['target_count']}，每个 {result['hp_each']} HP，"
                     + ("硬面" if result["side"] == "hard" else "软面") + "。")
        if request["hp"] is None:
            lines.append("未指定当前生命值，按满血计算。")
        lines.append("单一爆炸物方案（按新增硫磺从少到多排列）：")
        for option in result["options"]:
            lines.append(f"• {name(option['item'])}：每个目标 {option['per_target']} 个，共 {option['count']} 个；"
                         f"新增硫磺 {option['new_sulfur']}。公式：{option['formula']}")
        best = result["options"][0]
        lines.append(f"硫磺排序首项「{name(best['item'])}」的资源路径（不代表已满足科技或其他资源）：")
        lines.extend(craft_lines(best["craft"]))
        if any(option["launcher_required"] for option in result["options"]):
            lines.append("火箭方案需要火箭发射器；发射器未计入上述成本。")
        lines.extend(result["assumptions"])
    else:
        lines.append("制作目标：" + quantities(result["goals"]))
        lines.extend(craft_lines(result))
    if request["inventory"] is not None:
        lines.append("只抵扣已读清的可见材料；未展示或读不清的物品未计入，实际缺口可能更少。" if not request.get("inventory_complete",True) else
            "库存按本次填写清单抵扣；未列出的物品按 0 处理，不会修改你的实际背包。")
    lines.append("火药配方：" + ("普通工作台（每批 10）" if request["gunpowder_recipe"] == "workbench" else "混合台（每批 10）") + "。")
    lines.append("获取边界：组件、木材、矿产等直接列为待获取；低级燃料" +
                 ("展开为动物脂肪和布。" if request["expand_fuel"] or "lowgradefuel" in request["goals"] else "直接列为待获取，不推算回收、采集或冶炼路线。"))
    provenance = calculation["provenance"]
    lines.append(f"依据：{provenance['provider']} / {provenance['snapshot_id']}；抓取 {provenance['retrieved_at']}。")
    lines.append("第三方资料快照，未核验对应游戏 build，也未经游戏内实测；不保证适用于后续补丁或修改服。")
    source_urls = {provenance["source_url"]}
    plans = [option["craft"] for option in result["options"]] if calculation["operation"] == "raid" else [result]
    if calculation["operation"] == "raid":
        source_urls.add(result["source"])
    for plan in plans:
        source_urls.update(step["source"] for step in plan["steps"])
    lines.append("来源：\n" + "\n".join(sorted(source_urls)))
    lines.append(f"计算凭据：{calculation['calculation_id'][:16]}；算法 {calculation['algorithm']}。")
    return "\n".join(lines)

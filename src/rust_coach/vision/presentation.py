from datetime import datetime, timezone
from rust_coach.vision.context import visual_input


def visual_summary(visual,language="zh"):
    accepted = visual_input(visual)
    fresh = bool(accepted and visual["fresh"] and datetime.fromisoformat(accepted["expires_at"]) > datetime.now(timezone.utc))
    english = language == "en"
    if accepted and not visual.get("accepted"):
        contexts = list(visual.get("resolved_context",{}).values())
        subjects = {item["subject"] for item in visual["observations"]} | {item["subject"] for item in contexts}
        if any(item["temporal_context"] in {"historical","example"} for item in contexts):
            scope = "Historical/reference image; not current inventory." if english else "按上下文作为旧图或参考图分析，不计入当前库存。"
        elif subjects - {"self"}:
            scope = "Subjects are kept separate; other players' items are not yours." if english else "按上下文区分图片主体，他人物品不计入你的库存。"
        elif fresh:
            scope = "Assumed to show your current situation." if english else "默认按你当前的情况分析。"
        else:
            scope = "Observation is expired or its timing is unclear." if english else "观测已过期或时间上下文不明，仅作条件分析。"
        lines = [("Screenshot recognition · " if english else "截图识别结果 · ") + scope]
        if accepted["inventory_complete"] and fresh and accepted["subject"] == "self":
            lines.append("Calculations use the recognized inventory counts." if english else "计算直接采用图中识别的库存读数。")
        elif any(item["kind"] == "inventory" for item in visual["observations"]):
            lines.append("Only visible items are known; missing slots are not treated as zero." if english else "仅掌握可见物品，未展示或读不清的库存不按零处理。")
        lines.append("Tell me directly if any recognition is wrong." if english else "如有识别错误，直接告诉我即可，无需逐项确认。")
    elif accepted:
        heading = "User-confirmed screenshot observations" if english else "玩家已确认截图观测"
        state = ("Current under the declared observation time" if fresh else "Historical/expired; not current inventory") if english else (
            "按声明观测时间仍在有效期内" if fresh else "历史或已过期观测，不能当作当前库存")
        lines = [heading + " · " + state,
            ("Observed at: " if english else "观测时间：") + accepted["observed_at"],
            ("Profile saved, revision " if english else "已保存到档案，版本 ") + str(accepted["state_revision"]) if accepted["saved"] else (
                "Used for this task only; profile unchanged." if english else "仅用于本轮，未写入玩家档案。")]
    else:
        lines = ["Previous screenshot recognition." if english else "历史截图识别结果。"]
    lines.append("A partial view does not establish the whole base or loadout. No combat damage calculator is available." if english else
        "局部画面不证明完整基地或装备状态；当前没有战斗伤害计算器。")
    for item in visual["observations"][:12]:
        quantity = str(item["quantity"]) if item["quantity"] is not None else ("unknown" if english else "未知")
        details = item["label"] + (" · " + item["description"] if item["description"] else "")
        if item["kind"] == "inventory":
            details += (" · visible count: " if english else " · 图中读数：") + quantity
        lines.append(f"[{item['id']}] {details}")
    if len(visual["observations"]) > 12:
        lines.append("See recognition details for all detected slots." if english else "其余格子可展开识别详情查看。")
    if visual["unknowns"]:
        lines.append(("Unknown: " if english else "尚不明确：") + "；".join(visual["unknowns"]))
    return "\n".join(lines)

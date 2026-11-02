from datetime import datetime, timezone
from rust_coach.vision.context import item_identity_status, visual_input


def visual_warnings(visual, language="zh"):
    accepted = visual_input(visual)
    english = language == "en"
    lines = []
    if not accepted or not visual.get("fresh") or datetime.fromisoformat(accepted["expires_at"]) <= datetime.now(timezone.utc):
        lines.append("The screenshot is not valid current inventory; recheck your situation." if english else "这张截图不能作为当前库存，请重新确认现状。")
    elif accepted["subject"] != "self":
        lines.append("Other players' items are not counted as yours." if english else "他人的物品没有计入你的库存。")
    if any(item["kind"] in {"base", "equipment"} for item in visual["observations"]):
        lines.append("Only visible structures and equipment can be assessed." if english else "这里只判断图中可见的结构和装备。")
    return "\n".join(lines)


def visual_summary(visual,language="zh", *, include_observations=True, include_unknowns=True):
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
        elif fresh and accepted["subject"] == "self" and any(item["kind"] == "inventory" for item in visual["observations"]):
            lines.append("Identified items with readable counts are used; unknown items are excluded." if english else "按已辨认物品和清晰数量计算；无法辨认的物品不计入。")
        included = set(accepted.get("included_observations",[]))
        if fresh and accepted["subject"] == "self" and any(item["id"] in included and item["uncertainty"] for item in visual["observations"]):
            lines.append("Some icons have image-quality warnings; calculations use the recognized identities and counts." if english else
                "部分图标有画质提醒，本次按下列识别结果计算。")
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
    if any(item["kind"] in {"base","equipment"} for item in visual["observations"]):
        lines.append("Only visible structures and equipment are known." if english else "仅能判断可见的结构和装备。")
    if not include_observations:
        if include_unknowns and visual["unknowns"]:
            lines.append(("Unknown: " if english else "尚不明确：") + "；".join(visual["unknowns"]))
        return "\n".join(lines)
    for item in visual["observations"][:12]:
        quantity = str(item["quantity"]) if item["quantity"] is not None else ("unknown" if english else "未知")
        details = item["label"]
        if item["kind"] == "inventory":
            details += " × " + quantity
            if not visual.get("accepted") and item_identity_status(item) != "identified":
                details += " (identity unclear; excluded)" if english else "（物品身份未确定，未计入）"
            elif accepted and not visual.get("accepted") and "included_observations" in accepted and item["id"] not in accepted["included_observations"]:
                details += " (count or slot unresolved; excluded)" if english else "（数量或槽位范围未确定，未计入）"
        elif item["description"]:
            details += " · " + item["description"]
        lines.append("• " + details)
    if len(visual["observations"]) > 12:
        lines.append("Other detected items are retained for follow-up questions." if english else "其余已识别物品也会保留用于后续提问。")
    if include_unknowns and visual["unknowns"]:
        lines.append(("Unknown: " if english else "尚不明确：") + "；".join(visual["unknowns"]))
    return "\n".join(lines)

from datetime import datetime, timezone


def visual_summary(visual,language="zh"):
    accepted = visual.get("accepted")
    fresh = bool(accepted and visual["fresh"] and datetime.fromisoformat(accepted["expires_at"]) > datetime.now(timezone.utc))
    english = language == "en"
    if accepted:
        heading = "User-confirmed screenshot observations" if english else "玩家已确认截图观测"
        state = ("Current under the declared observation time" if fresh else "Historical/expired; not current inventory") if english else (
            "按声明观测时间仍在有效期内" if fresh else "历史或已过期观测，不能当作当前库存")
        lines = [heading + " · " + state,
            ("Observed at: " if english else "观测时间：") + accepted["observed_at"],
            ("Profile saved, revision " if english else "已保存到档案，版本 ") + str(accepted["state_revision"]) if accepted["saved"] else (
                "Used for this task only; profile unchanged." if english else "仅用于本轮，未写入玩家档案。")]
    else:
        lines = ["Screenshot candidates · not confirmed; not a complete current inventory." if english else "截图识别候选 · 尚未确认，不代表完整当前库存。"]
    lines.append("A partial view does not establish the whole base or loadout. No combat damage calculator is available." if english else
        "局部画面不证明完整基地或装备状态；当前没有战斗伤害计算器。")
    for item in visual["observations"][:12]:
        quantity = str(item["quantity"]) if item["quantity"] is not None else ("unknown" if english else "未知")
        details = item["label"] + (" · " + item["description"] if item["description"] else "")
        if item["kind"] == "inventory":
            details += (" · visible count: " if english else " · 图中读数：") + quantity
        lines.append(f"[{item['id']}] {details}")
    if len(visual["observations"]) > 12:
        lines.append("See the observation card for all detected slots." if english else "其余格子请在识别确认卡片中逐项查看。")
    if visual["unknowns"]:
        lines.append(("Unconfirmed: " if english else "待核对：") + "；".join(visual["unknowns"]))
    if not accepted:
        lines.append("Confirm the subject, time, quantities and scope in the card before using exact inventory. Saving is optional." if english else
            "精确库存计算前，请在卡片中确认主体、时间、数量与覆盖范围；保存档案是独立选择。")
    return "\n".join(lines)

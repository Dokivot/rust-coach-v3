from datetime import datetime, timezone

from rust_coach.calculations.engine import Calculator
from rust_coach.calculations.presentation import render_calculation
from rust_coach.contracts import ModelReply
from rust_coach.player_state.domain import project
from rust_coach.player_state.schemas import FIELD_DEFINITIONS
from rust_coach.presentation.contracts import AnswerBlock, AnswerEnvelope, ProtectedAssertion, ResponseContext
from rust_coach.presentation.validation import check_envelope, narrative_issues, signature
from rust_coach.vision.presentation import visual_summary
from rust_coach.vision.context import visual_input


STATUS_TEXT = {
    "observation": ("可先观察核对", "Check the conditions first"),
    "conditional": ("条件未全部确认", "Some prerequisites remain unconfirmed"),
    "inventory_unknown": ("库存信息不完整，预算仅计已知材料", "Inventory is incomplete; budgets use only known items"),
    "blocked": ("材料或工作台条件未满足", "Materials or workbench requirements are not met"),
    "unverified": ("缺少足够依据，不能认定可执行", "Insufficient evidence to establish feasibility"),
    "feasible_in_snapshot": ("仅在本次规则与玩家状态快照下可行", "Feasible only under the supplied inventory and rule snapshot"),
}
RULE_LIMIT = ("依据为 PC 原版资料快照，未核验游戏 build，未经游戏内实测。",
              "Based on a PC vanilla data snapshot; game build and in-game behavior are not verified.")
EVIDENCE_LIMIT = ("网页原文匹配不等于事实或当前版本已验证；相关条件仍需核对。",
                  "Matching a source quote does not verify the claim or its applicability to the current game version.")
WORKBENCH_TEXT = {
    "unknown": ("尚未提供当前等级", "Current level not supplied"),
    "satisfied": ("已提供等级满足要求", "Supplied level meets the requirement"),
    "blocked": ("已提供等级不足", "Supplied level is insufficient"),
    "not_needed": ("无需工作台", "No workbench required"),
}
BLUEPRINT_TEXT = {
    "not_checked": ("尚未核对可制作配方", "Craftable recipes have not been checked"),
    "check_required": ("仍需核对可制作配方", "Craftable recipes still need checking"),
    "declared_available": ("按所填清单声明可制作", "Declared available in the supplied list"),
    "not_needed": ("本次无需制作配方", "No crafting recipes needed"),
}


def localized(context, chinese, english):
    return english if context.language == "en" else chinese


def quantities(values, catalog, context):
    return "、".join(f"{catalog.items[key]['name'] if context.language == 'zh' else key} × {value}"
                    for key, value in values.items()) or localized(context, "无", "none")


def calculation_summary(calculation, context):
    catalog = Calculator().catalog
    result = calculation["result"]
    if calculation["operation"] == "craft":
        label = localized(context, "尚需获取", "Still needed") if result["inventory_known"] else localized(
            context, "总需求（库存未知，不能判断实际缺口）", "Total requirements (inventory unknown, actual shortfall unknown)")
        partial = result["inventory_known"] and not result.get("inventory_complete",True)
        if partial:
            label = localized(context,"抵扣截图可见材料后，尚需" if calculation.get("visual_usage") else "抵扣已知材料后，尚需",
                "Still needed after using known inventory")
        requirements = result["requirements"]
        lines = [
            localized(context, "制作目标：", "Crafting goal: ") + quantities(result["goals"], catalog, context),
            label + ": " + quantities(result["acquisition"], catalog, context),
            localized(context, "所需工作台等级：", "Required workbench level: ") + str(requirements["workbench"]) + "; " +
                localized(context, *WORKBENCH_TEXT[requirements["workbench_status"]]),
            localized(context, "蓝图检查：", "Blueprint check: ") + localized(context, *BLUEPRINT_TEXT[requirements["blueprints_status"]]),
            localized(context, "材料预算不代表全部前置条件满足；真实库存未扣减。",
                      "A materials budget does not establish all prerequisites; actual inventory is unchanged."),
        ]
        if result["inventory_known"]:
            lines.insert(2,localized(context,"已抵扣：","Inventory used: ") + quantities(result["inventory_used"],catalog,context))
        if partial:
            lines.insert(3,localized(context,"未展示或读不清的物品未计入，实际缺口可能更少。",
                "Unseen or unreadable items are excluded; the actual shortfall may be smaller."))
        visual_usage = calculation.get("visual_usage")
        if visual_usage and not result["inventory_known"]:
            excluded = "截图已过期或不是当前本人库存，未用于抵扣。" if not visual_usage.get("fresh",True) or visual_usage.get("subject") != "self" else "截图没有可用于计算的清晰读数，暂列总需求。"
            lines.insert(2,localized(context,excluded,"The screenshot cannot supply current personal inventory; totals are shown."))
        if requirements["blueprints_to_check"]:
            names = [catalog.items[key]["name"] if context.language == "zh" else key for key in requirements["blueprints_to_check"]]
            lines.append(localized(context, "待核对配方（含默认配方，不等于都要研究）：", "Check recipes (includes defaults, not all require research): ") + "、".join(names))
        if requirements["mixing_table_to_check"]:
            lines.append(localized(context, "另需确认已有混合台。", "Also confirm a mixing table is available."))
        return "\n".join(lines)
    best = result["options"][0]
    return "\n".join([
        localized(context, "计算条件：", "Conditions: ") + f"{result['target']} × {result['target_count']}, {result['hp_each']} HP, {result['side']}",
        localized(context, "已列单一方案中新增硫磺排序首项：", "Lowest additional sulfur among the listed single-method options: ") +
        quantities({best["item"]: best["count"]}, catalog, context),
        localized(context, "新增硫磺：", "Additional sulfur: ") + str(best["new_sulfur"]),
        localized(context, "不是所有资源或混搭的全局最优；科技、发射器与其他材料仍需核对。",
                  "Not a global optimum over all resources or mixed methods; check technology, launchers and other materials."),
    ])


def fallback_text(context):
    return localized(context,
        "这条回复没通过表达与依据检查，先不把它当作结论。请明确目标或补充关键条件；已有工具结果会保留供你核对。",
        "This reply did not pass the expression and evidence checks. Clarify the goal or missing conditions; valid tool results remain available below.")


def calculation_assertions(calculation):
    reference = calculation["calculation_id"]
    result = calculation["result"]
    assertions = [ProtectedAssertion(kind="evidence_limit", reference=reference, value=calculation["provenance"])]
    if calculation["operation"] == "craft":
        assertions.extend([
            ProtectedAssertion(kind="quantity", reference=reference, value={key: result[key] for key in
                ("goals", "acquisition", "inventory_known", "inventory_complete", "inventory_used", "reserved", "batch_leftovers")}),
            ProtectedAssertion(kind="feasibility", reference=reference, value=result["requirements"]),
        ])
    else:
        assertions.append(ProtectedAssertion(kind="comparison", reference=reference, value=result))
    return assertions


def finalize_response(state):
    context = ResponseContext.model_validate(state.get("response_context") or {})
    calculation, advice = state.get("calculation"), state.get("advice")
    blocks = []
    assertions = []
    payload = {}
    checks = list(state.get("response_checks", []))
    repairs = state.get("response_repairs", 0)
    fallback = state.get("response_fallback")
    detail = "main" if context.preferences.verbosity == "detailed" else "detail"

    def add(identifier, text, kind="protected", placement="main", reference=None):
        if text:
            blocks.append(AnswerBlock(id=identifier, kind=kind, text=text, placement=placement, reference=reference))

    if advice:
        kind = "advice"
        repairs += advice.get("repairs", 0)
        evaluations = advice.get("evaluations", {})
        recommendations = [recommendation for recommendation in ((advice.get("answer") or {}).get("recommendation"),
                            (advice.get("answer") or {}).get("alternative")) if recommendation]
        payload = {"evaluations": evaluations, "selected_candidates": [item["candidate_id"] for item in recommendations],
                   "evidence": advice["evidence"], "state_revision": (state.get("player_snapshot") or {}).get("revision")}
        candidates = {candidate["id"]: candidate for candidate in (advice.get("plan") or {}).get("candidates", [])}
        snapshot = state.get("player_snapshot")
        stale = False
        if snapshot:
            current = project(snapshot, datetime.now(timezone.utc))
            stale = any(record.get("status") == "fresh" and current["facts"][key]["status"] != "fresh"
                        for key, record in snapshot["facts"].items()) or any(
                        container.get("usable_for_exact_calculation") and not current["containers"][key]["usable_for_exact_calculation"]
                        for key, container in snapshot["containers"].items())
        visual = state.get("visual") or {}
        accepted = visual_input(visual)
        if accepted and (not visual.get("fresh") or datetime.fromisoformat(accepted["expires_at"]) <= datetime.now(timezone.utc)):
            stale = True
        payload["delivery_state_stale"] = stale
        assertions.append(ProtectedAssertion(kind="selection", reference="advice", value={
            "candidate_ids": payload["selected_candidates"], "delivery_state_stale": stale}))
        for identifier, evaluation in evaluations.items():
            assertions.append(ProtectedAssertion(kind="feasibility", reference=identifier, value={
                "status": evaluation["status"], "missing_fields": evaluation.get("missing_fields", []),
                "delivery_state_stale": stale}))
            if evaluation.get("calculation"):
                assertions.extend(calculation_assertions(evaluation["calculation"]))
        for document in advice["evidence"]["documents"]:
            assertions.append(ProtectedAssertion(kind="evidence_limit", reference=document["id"], value=document))
        add("scope", localized(context, *EVIDENCE_LIMIT) if advice["evidence"]["documents"] else
            localized(context, "建议依据当前已提供的条件；未知信息不会被当作零或没有。",
                      "Advice uses the supplied conditions; unknown information is not treated as zero or absent."))
        if stale:
            add("delivery_stale", localized(context, "生成期间状态已过期；预算只描述此前快照，请重新核对当前条件。",
                                            "State expired during generation. Budgets describe the earlier snapshot; recheck current conditions."))
        if not recommendations:
            add("fallback", state["reply"].content)
            fallback = "advice_unavailable"
        for index, recommendation in enumerate(recommendations):
            identifier = recommendation["candidate_id"]
            evaluation = evaluations.get(identifier, {"status": "unverified", "missing_fields": []})
            status = evaluation["status"] if not stale else "unverified"
            title = candidates.get(identifier, {}).get("title", identifier)
            narrative = [title, recommendation["reason"], recommendation["tradeoff"], *recommendation["prerequisites"],
                         *recommendation["steps"], recommendation["completion"], recommendation["switch_when"]]
            issues = [issue for text in narrative for issue in narrative_issues(text, forbid_numbers=True, status=status)]
            heading = localized(context, "首选方案" if index == 0 else "替代方案", "First option" if index == 0 else "Alternative")
            add(identifier + ".status", heading + " · " + localized(context, *STATUS_TEXT.get(status, STATUS_TEXT["unverified"])), reference=identifier)
            missing = evaluation.get("missing_fields", [])
            if missing:
                add(identifier + ".missing", localized(context, "档案待确认：", "Unconfirmed state fields: ") +
                    "、".join(FIELD_DEFINITIONS[key][1] if context.language == "zh" else key for key in missing), reference=identifier)
            if issues:
                checks.extend({"code": issue, "status": "blocked", "field": identifier} for issue in sorted(set(issues)))
                add(identifier + ".fallback", fallback_text(context))
                fallback = "narrative_rejected"
                continue
            fields = [("title", "行动", "Action", title), ("reason", "理由", "Reason", recommendation["reason"]),
                      ("tradeoff", "取舍", "Trade-off", recommendation["tradeoff"]),
                      ("prerequisites", "前置条件", "Prerequisites", "；".join(recommendation["prerequisites"])),
                      ("steps", "下一步", "Next steps", "\n".join("• " + step for step in recommendation["steps"])),
                      ("completion", "完成标准", "Done when", recommendation["completion"]),
                      ("switch", "停止或切换", "Stop or switch when", recommendation["switch_when"])]
            for field, chinese, english, text in fields:
                placement = "detail" if context.preferences.verbosity == "concise" and field in {"reason", "tradeoff"} else "main"
                add(identifier + "." + field, localized(context, chinese, english) + ": " + text, "narrative", placement, identifier)
        if advice.get("answer"):
            for field, chinese, english, text in [("missing", "需要补充", "Missing information", "；".join(advice["answer"]["missing"])),
                                                   ("uncertainty", "尚未确定", "Uncertainty", advice["answer"]["uncertainty"])]:
                if not narrative_issues(text, forbid_numbers=True):
                    add(field, localized(context, chinese, english) + ": " + text, "narrative")
        for identifier, evaluation in evaluations.items():
            tool = evaluation.get("calculation")
            if tool:
                add(identifier + ".budget", localized(context, "独立候选预算（不能叠加执行）：", "Independent alternative budget (do not combine): ") +
                    identifier + "\n" + calculation_summary(tool, context), reference=tool["calculation_id"])
                add(identifier + ".report", render_calculation(tool, Calculator().catalog), placement=detail, reference=tool["calculation_id"])
        if any(evaluation.get("calculation") for evaluation in evaluations.values()):
            add("rule_limit", localized(context, *RULE_LIMIT))
            add("reserve_limit", localized(context, "预算未计维护与出行预留；实际执行前需核对保留资源。",
                                           "Budgets exclude upkeep and travel reserves; check reserved resources before acting."))
        for index, warning in enumerate(dict.fromkeys(advice["evidence"]["warnings"])):
            add(f"retrieval_warning.{index}", warning)
        for index, document in enumerate(advice["evidence"]["documents"]):
            add(f"source.{index}", document["title"] + "\n" + document["url"] + "\n" + document["fetched_at"],
                placement=detail, reference=document["id"])
    elif calculation and calculation.get("status") == "calculated":
        kind = "calculation"
        payload = calculation
        assertions.extend(calculation_assertions(calculation))
        add("calculation_summary", calculation_summary(calculation, context), reference=calculation["calculation_id"])
        add("rule_limit", localized(context, *RULE_LIMIT))
        if calculation["operation"] == "raid":
            add("raid_assumptions", "\n".join(calculation["result"]["assumptions"]))
        add("original_report", state["reply"].content, placement=detail, reference=calculation["calculation_id"])
    elif state.get("route") == "player_state":
        kind = "state"
        payload = state.get("player_snapshot") or {}
        assertions.append(ProtectedAssertion(kind="observation", reference="player_snapshot", value=payload))
        add("state_report", state["reply"].content)
    elif calculation:
        kind = "clarification"
        payload = calculation
        text = state["reply"].content
        issues = narrative_issues(text)
        add("clarification", fallback_text(context) if issues else text)
        if issues:
            fallback = "clarification_rejected"
            checks.extend({"code": issue, "status": "blocked"} for issue in issues)
    else:
        kind = "chat"
        add("chat", state["reply"].content, "narrative")
    visual = state.get("visual")
    if visual:
        if kind == "chat":
            kind = "visual"
        payload = {"business":payload,"visual":visual}
        assertions.append(ProtectedAssertion(kind="observation",reference=visual["draft_id"],value=visual))
        add("visual_scope",visual_summary(visual,context.language),reference=visual["draft_id"],
            placement="main" if state.get("visual_original_question") or state.get("route") == "visual_observe" or calculation or advice else "detail")
        if blocks[-1].placement == "main":
            blocks.insert(0,blocks.pop())
        if advice and advice.get("answer"):
            for recommendation in (advice["answer"]["recommendation"],advice["answer"].get("alternative")):
                if recommendation and recommendation.get("visual_citations"):
                    add("visual_reference." + recommendation["candidate_id"],localized(context,"图片区域依据：","Image region evidence: ") +
                        ", ".join(recommendation["visual_citations"]),reference=visual["draft_id"])
    envelope = AnswerEnvelope(kind=kind, context=context, protected_payload=payload, blocks=tuple(blocks), assertions=tuple(assertions),
                              checks=tuple(checks), repair_count=repairs, fallback_reason=fallback)
    payload_hash = signature(payload)
    blocks_hash = signature([block.model_dump() for block in blocks if block.kind == "protected"])
    checks.extend(check_envelope(envelope, payload_hash, blocks_hash))
    text = "\n\n".join(block.text for block in blocks)
    presentation = {"context": context.model_dump(mode="json"), "kind": kind, "blocks": [block.model_dump() for block in blocks],
                    "protected_payload_hash": payload_hash, "protected_blocks_hash": blocks_hash,
                    "assertions": [assertion.model_dump(mode="json") for assertion in assertions],
                    "checks": checks, "repair_count": repairs, "fallback_reason": fallback}
    return {"reply": ModelReply(content=text, truncated=state["reply"].truncated), "presentation": presentation}

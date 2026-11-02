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
from rust_coach.architecture.contracts import DecisionResult
from rust_coach.architecture.generation import answer_spec_from_decision


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
    "satisfied": ("你提供的等级满足要求", "the supplied level meets the requirement"),
    "blocked": ("你提供的等级还不够", "the supplied level is insufficient"),
    "not_needed": ("无需工作台", "No workbench required"),
}
BLUEPRINT_TEXT = {
    "not_checked": ("尚未核对可制作配方", "Craftable recipes have not been checked"),
    "check_required": ("仍需核对可制作配方", "Craftable recipes still need checking"),
    "declared_available": ("你提供的清单包含所需配方", "the supplied list includes the required recipes"),
    "not_needed": ("本次无需制作配方", "No crafting recipes needed"),
}


def localized(context, chinese, english):
    return english if context.language == "en" else chinese


def quantities(values, catalog, context):
    return localized(context, "、", ", ").join(f"{value} {catalog.items[key]['name'] if context.language == 'zh' else key}"
                    for key, value in values.items()) or localized(context, "无", "none")


def sentences(values, context):
    ending = localized(context, "。", ".")
    parts = dict.fromkeys(value.strip().rstrip("。.!！;；") for value in values if value.strip())
    return localized(context, "", " ").join(value + ending for value in parts if value)


def calculation_summary(calculation, context):
    catalog = Calculator().catalog
    result = calculation["result"]
    if calculation["operation"] == "craft":
        goal = quantities(result["goals"], catalog, context)
        acquisition = quantities(result["acquisition"], catalog, context)
        partial = result["inventory_known"] and not result.get("inventory_complete",True)
        if not result["inventory_known"]:
            answer = localized(context, f"制作 {goal} 总共需要 {acquisition}；库存未知，不能判断实际缺口。",
                f"Crafting {goal} requires {acquisition} in total. Inventory is unknown, so the actual shortfall is unknown.")
        elif partial:
            scope = "抵扣截图可见材料后" if calculation.get("visual_usage") else "抵扣已知材料后"
            answer = localized(context, f"制作 {goal}，{scope}，尚需 {acquisition}。",
                f"To craft {goal}, you still need {acquisition} after using known inventory.") if result["acquisition"] else localized(
                    context, f"制作 {goal}，{scope}，这份材料预算已没有缺口。",
                    f"The known inventory covers the material budget for {goal}.")
        elif result["acquisition"]:
            answer = localized(context, f"按你提供的库存，制作 {goal} 还差 {acquisition}。",
                f"Based on the inventory you supplied, crafting {goal} still needs {acquisition}.")
        else:
            answer = localized(context, f"按你提供的库存，制作 {goal} 不需要再补材料。",
                f"Based on the inventory you supplied, no additional materials are needed for {goal}.")
        requirements = result["requirements"]
        lines = [answer]
        if partial:
            lines.append(localized(context,"未展示或读不清的物品未计入，实际缺口可能更少。",
                "Unseen or unreadable items are excluded; the actual shortfall may be smaller."))
        visual_usage = calculation.get("visual_usage")
        if visual_usage and not result["inventory_known"]:
            excluded = "截图已过期或不是当前本人库存，未用于抵扣。" if not visual_usage.get("fresh",True) or visual_usage.get("subject") != "self" else "截图没有可用于计算的清晰读数，暂列总需求。"
            lines.append(localized(context,excluded,"The screenshot cannot supply current personal inventory; totals are shown."))
        workbench = requirements["workbench"]
        if requirements["workbench_status"] == "not_needed":
            lines.append(localized(context, "本次无需工作台。", "No workbench is needed."))
        else:
            lines.append(localized(context, f"需要 {workbench} 级工作台，", f"A level {workbench} workbench is required; ") +
                localized(context, *WORKBENCH_TEXT[requirements["workbench_status"]]) + localized(context, "。", "."))
        if not requirements["blueprints_to_check"]:
            lines.append(localized(context, "配方方面，", "For recipes, ") +
                localized(context, *BLUEPRINT_TEXT[requirements["blueprints_status"]]) + localized(context, "。", "."))
        if requirements["blueprints_to_check"]:
            names = [catalog.items[key]["name"] if context.language == "zh" else key for key in requirements["blueprints_to_check"]]
            lines.append(localized(context, "待核对配方（含默认配方，不等于都要研究）：", "Check recipes (includes defaults, not all require research): ") + "、".join(names))
        if requirements["mixing_table_to_check"]:
            lines.append(localized(context, "另需确认已有混合台。", "Also confirm a mixing table is available."))
        if result["reserved"]:
            lines.append(localized(context, "预算已保留 ", "The budget reserves ") +
                quantities(result["reserved"], catalog, context) + localized(context, "。", "."))
        return sentences(lines, context)
    best = result["options"][0]
    side = localized(context, "硬面" if result["side"] == "hard" else "软面", result["side"] + " side")
    target = result["target_name"] if context.language == "zh" else result["target"]
    return sentences([
        localized(context, "在已比较的单一爆炸物方案里，新增硫磺最少的是 ",
                  "Among the compared single-method options, the lowest additional sulfur cost is for ") +
        quantities({best["item"]: best["count"]}, catalog, context) +
        localized(context, f"，需要新增 {best['new_sulfur']} 硫磺", f", requiring {best['new_sulfur']} additional sulfur"),
        localized(context, f"这是按 {result['target_count']} 个{target}、每个 {result['hp_each']} HP、{side}计算的",
                  f"This assumes {result['target_count']} {target}, each at {result['hp_each']} HP, attacked from the {side}"),
        localized(context, "不是所有资源或混搭的全局最优；科技、发射器与其他材料仍需核对。",
                  "Not a global optimum over all resources or mixed methods; check technology, launchers and other materials."),
    ], context)


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
        if text and text.strip():
            blocks.append(AnswerBlock(id=identifier, kind=kind, text=text, placement=placement, reference=reference))

    def add_budget(identifier, evaluation, placement):
        tool = evaluation.get("calculation")
        if tool:
            add(identifier + ".budget", calculation_summary(tool, context), placement=placement,
                reference=tool["calculation_id"])
            add(identifier + ".report", render_calculation(tool, Calculator().catalog), placement=detail,
                reference=tool["calculation_id"])

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
            if not issues:
                opening = localized(context, "可以先考虑" if index == 0 else "另一种选择是", "Consider " if index == 0 else "Alternatively, consider ") + title
                add(identifier + ".opening", sentences([opening], context), "narrative", reference=identifier)
            add(identifier + ".status", sentences([localized(context, *STATUS_TEXT.get(status, STATUS_TEXT["unverified"]))], context), reference=identifier)
            missing = evaluation.get("missing_fields", [])
            if missing:
                add(identifier + ".missing", localized(context, "档案待确认：", "Unconfirmed state fields: ") +
                    "、".join(FIELD_DEFINITIONS[key][1] if context.language == "zh" else key for key in missing), reference=identifier)
            if issues:
                checks.extend({"code": issue, "status": "blocked", "field": identifier} for issue in sorted(set(issues)))
                add(identifier + ".fallback", fallback_text(context))
                fallback = "narrative_rejected"
                add_budget(identifier, evaluation, "main")
                continue
            explanation = [recommendation["reason"], recommendation["tradeoff"]]
            instructions = [*recommendation["prerequisites"], *recommendation["steps"],
                localized(context, "做到这一步就算完成：", "You are done when: ") + recommendation["completion"],
                recommendation["switch_when"]]
            concise = context.preferences.verbosity == "concise"
            add(identifier + ".plan", sentences(instructions if concise else explanation + instructions, context),
                "narrative", reference=identifier)
            if concise:
                add(identifier + ".reason", sentences(explanation, context), "narrative", "detail", identifier)
            add_budget(identifier, evaluation, "main")
        if advice.get("answer"):
            for field, chinese, english, text in [("missing", "需要补充", "Missing information", "；".join(advice["answer"]["missing"])),
                                                   ("uncertainty", "尚未确定", "Uncertainty", advice["answer"]["uncertainty"])]:
                if text.strip() and not narrative_issues(text, forbid_numbers=True):
                    prefix = localized(context, "还需要你补充：", "Please also supply: ") if field == "missing" else ""
                    add(field, sentences([prefix + text], context), "narrative")
        for identifier, evaluation in evaluations.items():
            if identifier not in payload["selected_candidates"]:
                add_budget(identifier, evaluation, detail)
        selected_budget = any(evaluations.get(identifier, {}).get("calculation") for identifier in payload["selected_candidates"])
        if len(recommendations) > 1 and selected_budget:
            add("alternative_limit", localized(context, "两种选择分别计算预算，不能把它们叠加执行。",
                "These alternatives have separate budgets; do not combine them into one execution plan."))
        if any(evaluation.get("calculation") for evaluation in evaluations.values()):
            budget_placement = "main" if selected_budget else detail
            add("rule_limit", localized(context, *RULE_LIMIT), placement=budget_placement)
            add("reserve_limit", localized(context, "预算未计维护与出行预留；实际执行前需核对保留资源。",
                                           "Budgets exclude upkeep and travel reserves; check reserved resources before acting."), placement=budget_placement)
        if advice["evidence"]["documents"]:
            add("scope", localized(context, *EVIDENCE_LIMIT), placement=detail)
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
        if context.teaching.get("mode") in {"guided", "practice"} and context.teaching.get("focus"):
            add("teaching_focus", localized(context, "理解这次结果：", "Understanding this result: ") +
                " ".join(context.teaching["focus"]) if context.language == "zh" else
                "Use the report's stated inventory scope, requirements and assumptions; unknown inputs are not zero.", "narrative")
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
        business_answer = bool(calculation or advice)
        if business_answer:
            add("visual_context", visual_summary(visual, context.language, include_observations=False),
                reference=visual["draft_id"])
        add("visual_scope",visual_summary(visual,context.language),reference=visual["draft_id"],
            placement=detail if business_answer else "main" if state.get("visual_original_question") or state.get("route") == "visual_observe" else "detail")
        if not business_answer and blocks[-1].placement == "main":
            blocks.insert(0,blocks.pop())
        if advice and advice.get("answer"):
            for recommendation in (advice["answer"]["recommendation"],advice["answer"].get("alternative")):
                if recommendation and recommendation.get("visual_citations"):
                    add("visual_reference." + recommendation["candidate_id"],localized(context,"图片区域依据：","Image region evidence: ") +
                        ", ".join(recommendation["visual_citations"]),reference=visual["draft_id"], placement=detail)
    envelope = AnswerEnvelope(kind=kind, context=context, protected_payload=payload, blocks=tuple(blocks), assertions=tuple(assertions),
                              checks=tuple(checks), repair_count=repairs, fallback_reason=fallback)
    payload_hash = signature(payload)
    blocks_hash = signature([block.model_dump() for block in blocks if block.kind == "protected"])
    checks.extend(check_envelope(envelope, payload_hash, blocks_hash))
    text = "\n\n".join(block.text for block in blocks if block.placement == "main")
    presentation = {"context": context.model_dump(mode="json"), "kind": kind, "blocks": [block.model_dump() for block in blocks],
                    "protected_payload_hash": payload_hash, "protected_blocks_hash": blocks_hash,
                    "assertions": [assertion.model_dump(mode="json") for assertion in assertions],
                    "checks": checks, "repair_count": repairs, "fallback_reason": fallback}
    decision_data = state.get("decision_result")
    if decision_data:
        decision_result = DecisionResult.model_validate(decision_data)
        answer_spec = answer_spec_from_decision(decision_result)
        architecture = dict(state.get("architecture") or {})
        trace = architecture.get("trace")
        if trace:
            from rust_coach.architecture.observability import emit_trace
            trace = emit_trace(trace, layer="generation", component="answer-spec",
                               event="spec.created", references={"kind": answer_spec.kind, "status": answer_spec.status})
            architecture["trace"] = trace
        presentation["architecture"] = {
            "task": architecture.get("task"),
            "decision_request": architecture.get("decision_request"),
            "decision_result": decision_result.model_dump(mode="json"),
            "answer_spec": answer_spec.model_dump(mode="json"),
            "trace": trace,
        }
    return {"reply": ModelReply(content=text, truncated=state["reply"].truncated), "presentation": presentation}

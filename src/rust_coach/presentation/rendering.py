from datetime import datetime, timezone

from rust_coach.calculations.engine import Calculator
from rust_coach.calculations.presentation import render_calculation
from rust_coach.contracts import ModelReply
from rust_coach.progress import report_stage
from rust_coach.player_state.domain import project
from rust_coach.player_state.schemas import FIELD_DEFINITIONS
from rust_coach.presentation.contracts import AnswerBlock, AnswerEnvelope, ProtectedAssertion, ResponseContext
from rust_coach.presentation.validation import check_envelope, narrative_issues, signature
from rust_coach.vision.presentation import visual_summary, visual_warnings
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


def localized(context, chinese, english):
    return english if context.language == "en" else chinese


def item_name(identifier, catalog, context):
    if identifier == "explosive.timed" and (context.use_c4_alias or context.preferences.terminology == "game_terms"):
        return "C4"
    return catalog.items[identifier]["name"] if context.language == "zh" else identifier


def quantities(values, catalog, context):
    parts = []
    for identifier, amount in values.items():
        name = item_name(identifier, catalog, context)
        unit = " 个 " if context.language == "zh" and name == "C4" else " "
        parts.append(f"{amount}{unit}{name}")
    return localized(context, "、", ", ").join(parts) or localized(context, "无", "none")


def sentences(values, context):
    ending = localized(context, "。", ".")
    parts = dict.fromkeys(value.strip().rstrip("。.!！;；") for value in values if value.strip())
    return localized(context, "", " ").join(value + ending for value in parts if value)


def crafting_conditions(requirements, catalog, context):
    explicit = context.answer_focus == "feasibility" or context.preferences.verbosity == "detailed"
    workbench = requirements["workbench"]
    status = requirements["workbench_status"]
    needs_recipes = bool(requirements["blueprints_to_check"])
    lines = []
    if status == "blocked":
        lines.append(localized(context, f"你目前的工作台等级还不够，需要 {workbench} 级工作台",
            f"Your current workbench level is too low; a level {workbench} workbench is required"))
    elif status == "unknown":
        lines.append(localized(context, f"制作还需要 {workbench} 级工作台", f"Crafting also requires a level {workbench} workbench"))
        if explicit:
            lines.append(localized(context, "还需确认你是否具备", "Confirm whether you have one"))
    elif explicit:
        lines.append(localized(context, "你的工作台等级已满足要求" if status == "satisfied" else "本次无需工作台",
            "Your supplied workbench level meets the requirement" if status == "satisfied" else "No workbench is needed"))
    if needs_recipes:
        if explicit:
            names = localized(context, "、", ", ").join(item_name(identifier, catalog, context)
                for identifier in requirements["blueprints_to_check"])
            lines.append(localized(context, "还需确认能制作这些配方：", "Confirm you can craft these recipes: ") + names)
        elif status == "unknown":
            lines[-1] += localized(context, "，并确保相关配方能制作", ", and you need to be able to craft the required recipes")
        else:
            lines.append(localized(context, "制作前还要确保相关配方能制作", "Before crafting, confirm you can craft the required recipes"))
    elif explicit and requirements["blueprints_status"] == "declared_available":
        lines.append(localized(context, "所需配方已按你提供的清单核对", "The supplied list includes the required recipes"))
    if requirements["mixing_table_to_check"]:
        lines.append(localized(context, "还需要混合台", "A mixing table is also required"))
    return sentences(lines, context)


def calculation_summary(calculation, context):
    catalog = Calculator().catalog
    result = calculation["result"]
    if calculation["operation"] == "craft":
        goal = quantities(result["goals"], catalog, context)
        acquisition = quantities(result["acquisition"], catalog, context)
        listed = len(result["acquisition"]) > 2
        if listed:
            acquisition = "\n" + "\n".join("• " + quantities({identifier: amount}, catalog, context)
                for identifier, amount in result["acquisition"].items())
        separator = localized(context, "：", ":") if listed else " "
        partial = result["inventory_known"] and not result.get("inventory_complete",True)
        if not result["inventory_known"]:
            answer = localized(context, f"库存未知，不能判断实际缺口。制作 {goal} 的总需求是{separator}{acquisition}",
                f"Inventory is unknown, so the actual shortfall is unknown. Total requirements for {goal}: {acquisition}")
        elif partial:
            scope = "按截图里能确认的库存" if calculation.get("visual_usage") else "按目前能确认的库存"
            answer = localized(context, f"{scope}，制作 {goal} 还差{separator}{acquisition}",
                f"Based on the known inventory, crafting {goal} still needs{separator}{acquisition}") if result["acquisition"] else localized(
                    context, f"{scope}，制作 {goal} 的材料已经够了",
                    f"The known inventory covers the material budget for {goal}.")
        elif result["acquisition"]:
            answer = localized(context, f"按你提供的库存，制作 {goal} 还差{separator}{acquisition}",
                f"Based on the inventory you supplied, crafting {goal} still needs{separator}{acquisition}")
        else:
            answer = localized(context, f"按你提供的库存，制作 {goal} 不需要再补材料。",
                f"Based on the inventory you supplied, no additional materials are needed for {goal}.")
        lines = [answer if listed else sentences([answer], context)]
        conditions = crafting_conditions(result["requirements"], catalog, context)
        if conditions:
            lines.append(conditions)
        if partial and result["acquisition"]:
            lines.append(localized(context,"如果还有未计入的材料，实际缺口可能更少。",
                "If you have other items that were not counted, the actual shortfall may be smaller."))
        visual_usage = calculation.get("visual_usage")
        if visual_usage and not result["inventory_known"]:
            excluded = "截图已过期或不是当前本人库存，未用于抵扣。" if not visual_usage.get("fresh",True) or visual_usage.get("subject") != "self" else "本次截图识别结果没有可抵扣的库存，暂列总需求；具体原因见截图识别详情。"
            lines.append(localized(context,excluded,"The screenshot cannot supply current personal inventory; totals are shown."))
        if result["reserved"]:
            lines.append(localized(context, "预算已保留 ", "The budget reserves ") +
                quantities(result["reserved"], catalog, context) + localized(context, "。", "."))
        return "\n\n".join(lines)
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
        localized(context, "未提供当前血量，暂按满血计算", "Current HP was not supplied; full health is assumed")
            if calculation["input"]["hp"] is None else "",
        localized(context, "只抵扣已知库存，实际缺口和排序可能随未计入的物品变化",
                  "Only known inventory is deducted; other items may change the shortfall and ranking")
            if best["craft"]["inventory_known"] and not best["craft"]["inventory_complete"] else "",
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
    if state.get("calculation") or state.get("route") in {"player_state", "visual_observe"}:
        report_stage("compose")
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
        guide_mode = advice.get("mode") == "onboarding"
        guide = advice.get("answer") if guide_mode else None
        repairs += advice.get("repairs", 0)
        evaluations = advice.get("evaluations", {})
        recommendations = [recommendation for recommendation in ((advice.get("answer") or {}).get("recommendation"),
                            (advice.get("answer") or {}).get("alternative")) if recommendation]
        payload = {"evaluations": evaluations, "selected_candidates": [item["candidate_id"] for item in recommendations],
                   "evidence": advice["evidence"], "state_revision": (state.get("player_snapshot") or {}).get("revision")}
        if guide:
            payload["guide"] = guide
            payload["guide_review"] = advice.get("review")
            assertions.append(ProtectedAssertion(kind="selection", reference="guide", value={
                "stage_ids": [stage["id"] for stage in guide["stages"]], "stages": guide["stages"],
                "assumption": guide["assumption"], "stop_when": guide["stop_when"]}))
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
        if guide:
            add("guide.goal", guide["goal"], "narrative")
            add("guide.assumption", guide["assumption"])
            for index, stage in enumerate(guide["stages"], 1):
                add("guide." + stage["id"], f"{index}. " + stage["action"], "narrative")
                add("guide." + stage["id"] + ".completion", localized(context, "完成标志：", "Completion: ") + stage["completion"],
                    placement=detail)
                for citation_index, citation in enumerate(stage["citations"]):
                    add(f"guide.{stage['id']}.citation.{citation_index}", citation["quote"], placement=detail,
                        reference=citation["document_id"])
            add("guide.completion", localized(context, "这一阶段做到这里就可以：", "Finish this phase when: ") + guide["stages"][-1]["completion"])
            add("guide.stop", guide["stop_when"])
            for index, citation in enumerate(guide.get("stop_citations", [])):
                add(f"guide.stop.citation.{index}", citation["quote"], placement=detail, reference=citation["document_id"])
            add("guide.followup", guide["followup"], "narrative")
            add("guide.limitations", "\n".join(guide["limitations"]), placement=detail)
            checks.append({"code": "guide_evidence_review", "status": "pass"})
        if not recommendations and not guide:
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
        if advice.get("answer") and not guide_mode:
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
            add("rule_limit", localized(context, *RULE_LIMIT), placement=detail)
            add("reserve_limit", localized(context, "预算未计维护与出行预留；实际执行前需核对保留资源。",
                                           "Budgets exclude upkeep and travel reserves; check reserved resources before acting."), placement=budget_placement)
        if advice["evidence"]["documents"]:
            add("scope", localized(context, *EVIDENCE_LIMIT), placement=detail)
        for index, warning in enumerate(dict.fromkeys(advice["evidence"]["warnings"])):
            add(f"retrieval_warning.{index}", warning, placement=detail if guide_mode else "main")
        guide_sources = {citation["document_id"] for stage in (guide or {}).get("stages", []) for citation in stage["citations"]}
        guide_sources.update(citation["document_id"] for citation in (guide or {}).get("stop_citations", []))
        for index, document in enumerate(advice["evidence"]["documents"]):
            if document["id"] in guide_sources:
                add(f"guide.source.{index}", localized(context, "参考：", "Source: ") + document["title"] + "\n" + document["url"],
                    reference=document["id"])
            add(f"source.{index}", document["title"] + "\n" + document["url"] + "\n" + document["fetched_at"],
                placement=detail, reference=document["id"])
    elif calculation and calculation.get("status") == "calculated":
        kind = "calculation"
        payload = calculation
        assertions.extend(calculation_assertions(calculation))
        add("calculation_summary", calculation_summary(calculation, context), reference=calculation["calculation_id"])
        add("rule_limit", localized(context, *RULE_LIMIT), placement=detail)
        if calculation["operation"] == "raid":
            add("raid_assumptions", "\n".join(calculation["result"]["assumptions"]))
        add("original_report", state["reply"].content, placement=detail, reference=calculation["calculation_id"])
        if context.answer_focus != "resource_gap" and context.teaching.get("mode") in {"guided", "practice"} and context.teaching.get("focus"):
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
        summarized_inventory = calculation and calculation.get("status") == "calculated" and calculation["operation"] == "craft"
        if business_answer and not summarized_inventory:
            add("visual_context", visual_warnings(visual, context.language),
                reference=visual["draft_id"])
        add("visual_scope",visual_summary(visual,context.language, include_unknowns=not business_answer),reference=visual["draft_id"],
            placement=detail if business_answer else "main" if state.get("visual_original_question") or state.get("route") == "visual_observe" else "detail")
        if not business_answer and blocks[-1].placement == "main":
            blocks.insert(0,blocks.pop())
        if advice and advice.get("answer"):
            for recommendation in (advice["answer"]["recommendation"],advice["answer"].get("alternative")):
                if recommendation and recommendation.get("visual_citations"):
                    add("visual_reference." + recommendation["candidate_id"],localized(context,"图片区域依据：","Image region evidence: ") +
                        ", ".join(recommendation["visual_citations"]),reference=visual["draft_id"], placement=detail)
    report_stage("check_answer")
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

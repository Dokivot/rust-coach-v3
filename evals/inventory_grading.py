import hashlib
import json
from collections import Counter


GRADER_VERSION = "inventory-grader-v2"


def object_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def independently_calculated_materials(dataset, inventory):
    if dataset["oracle"]["per_c4"] != {"techparts": 2, "cloth": 5, "metal.fragments": 200, "lowgradefuel": 60, "sulfur": 2200, "charcoal": 3000}:
        raise ValueError("unsupported_independent_recipe_worksheet")
    used = {}

    def consume(item, needed):
        owned = min(needed, inventory.get(item, 0))
        if owned:
            used[item] = owned
        return needed - owned

    remaining_c4 = consume("explosive.timed", 10)
    remaining_explosives = consume("explosives", remaining_c4 * 20)
    remaining_gunpowder = consume("gunpowder", remaining_explosives * 50)
    batches = (remaining_gunpowder + 9) // 10
    totals = {"techparts": remaining_c4 * 2, "cloth": remaining_c4 * 5,
              "metal.fragments": remaining_explosives * 10, "lowgradefuel": remaining_explosives * 3,
              "sulfur": remaining_explosives * 10 + batches * 20, "charcoal": batches * 30}
    missing = {item: remainder for item, needed in totals.items() if (remainder := consume(item, needed)) > 0}
    return missing, used


def annotation_for(dataset, image):
    if "images" in dataset:
        return dataset["images"][image]["annotation"]
    annotation = dict(dataset["annotation"])
    if image == "occluded":
        annotation["slots"] = [slot for slot in annotation["slots"] if slot["item_id"] != dataset["occlusion"]["excluded_item"]]
    return annotation


def known_inventory(annotation):
    inventory = Counter()
    for slot in annotation["slots"]:
        inventory[slot["item_id"]] += slot["quantity"]
    return dict(inventory)


def visual_comparison(annotation, observations):
    expected = known_inventory(annotation)
    observed = Counter()
    wrong, missing = [], []
    for observation in observations:
        if observation.get("item_id") and observation.get("quantity") is not None:
            observed[observation["item_id"]] += observation["quantity"]
    if observations and all(observation.get("slot") for observation in observations):
        by_slot = {}
        for observation in observations:
            by_slot.setdefault(observation["slot"], []).append(observation)
        for slot in annotation["slots"]:
            matches = by_slot.get(slot["slot"], [])
            if not matches:
                missing.append(slot["slot"])
            for observation in matches:
                identified = observation.get("item_id") and observation.get("item_status", "identified") == "identified"
                if identified and (observation["item_id"] != slot["item_id"] or
                                   observation.get("quantity") is not None and observation["quantity"] != slot["quantity"]):
                    wrong.append({"slot": slot["slot"], "expected": slot, "actual": observation})
                elif not identified or observation.get("quantity") is None:
                    missing.append(slot["slot"])
            if len(matches) > 1:
                wrong.append({"slot": slot["slot"], "reason": "duplicate_observation", "actual": matches})
    else:
        wrong = [{"item_id": item, "quantity": amount} for item, amount in observed.items()
                 if item not in expected or amount > expected[item]]
        missing = [item for item, amount in expected.items() if observed.get(item, 0) < amount]
    return dict(observed), wrong, missing


def finalize(checks, execution_status="completed"):
    failures = [check for check in checks if check["status"] == "fail"]
    by_id = {check["id"]: check for check in checks}
    for check in checks:
        check["upstream_failures"] = [dependency for dependency in check["depends_on"]
                                      if by_id[dependency]["status"] == "fail"]
    roots = [check for check in failures if not check["upstream_failures"]]
    first = min(roots or failures, key=lambda check: (check["turn_index"], check["sequence"])) if failures else None
    pending = any(check["status"] in {"pending", "blocked"} for check in checks)
    partial = any(check["status"] == "partial" for check in checks)
    status = "fail" if failures else "pending" if pending else "partial" if partial else "pass"
    if execution_status == "not_run":
        status = "not_run"
    return {"grader_version": GRADER_VERSION, "status": status, "execution_status": execution_status,
            "evidence_status": "incomplete" if pending else "sufficient_for_scoped_checks",
            "coverage_status": "partial" if partial else "complete" if not pending else "unverified",
            "first_error_stage": first["stage"] if first else None,
            "first_error_check": first["name"] if first else None,
            "first_error_ref": first["artifact_ref"] if first else None,
            "first_error_id": first["id"] if first else None,
            "attribution": "earliest evidenced divergence by turn and workflow dependencies; not a proven causal root",
            "checks": checks}


def score_case(dataset, case, record):
    checks = []
    step_index = 0

    def check(name, stage, passed, expected, actual, *, evidence=True, dependencies=(), available=True, partial=False, ref=None):
        unresolved = any(checks_by_id[dependency]["status"] in {"pending", "blocked"} for dependency in dependencies)
        status = "blocked" if not available else "pending" if not evidence or unresolved else "pass" if passed else "partial" if partial else "fail"
        entry = {"id": f"turn:{step_index}:{name}", "turn_index": step_index, "sequence": len(checks),
                 "name": name, "stage": stage, "status": status, "expected": expected, "actual": actual,
                 "comparison_matches": bool(passed) if available else None, "depends_on": list(dependencies),
                 "artifact_ref": ref or f"steps/{step_index}/turn", "evidence": "verified" if evidence else "unverified"}
        events = record.get("steps", [])[step_index].get("events", []) if step_index < len(record.get("steps", [])) else []
        stage_events = {"vision": {"recognize_image", "check_image"}, "intent": {"understand"}, "parameters": {"understand"},
                        "state": {"check_image", "understand"}, "rules": {"calculate_craft", "calculate_raid"},
                        "calculation": {"calculate_craft", "calculate_raid"}, "expression": {"compose", "check_answer"}}
        entry["workflow_event_refs"] = [f"steps/{step_index}/events/{index}" for index, event in enumerate(events)
                                        if event.get("stage") in stage_events.get(stage, set())]
        checks.append(entry)
        checks_by_id[entry["id"]] = entry
        return entry["id"]

    checks_by_id = {}
    if record.get("execution_status") == "not_run":
        return finalize([], "not_run")
    expected_kind = case["expected"]
    execution_status = "completed"
    for step_index, specification in enumerate(case["turns"]):
        step = record.get("steps", [])[step_index] if step_index < len(record.get("steps", [])) else {}
        turn = step.get("turn") or {}
        if step.get("error") or not turn:
            error = step.get("error")
            stage = "external_service" if error else "execution"
            check("response_received" if error else "all_turns_executed", stage, False, "completed turn", error or "missing",
                  ref=f"steps/{step_index}")
            execution_status = "failed" if error else "incomplete"
            continue
        visual = turn.get("visual") or {}
        calculation = turn.get("calculation") or {}
        request = calculation.get("input") or {}
        result = calculation.get("result") or {}
        image = specification.get("image")
        vision_dependency = ()
        inventory = {}
        if image:
            annotation = annotation_for(dataset, image)
            trusted = annotation["status"] == "human_confirmed"
            inventory = known_inventory(annotation)
            observed, wrong, missing = visual_comparison(annotation, visual.get("observations", []))
            visual_present = isinstance(visual.get("observations"), list)
            visual_id = check("identified_items_and_counts", "vision", not wrong, "no confidently wrong known slot", wrong,
                              evidence=trusted, available=visual_present, ref=f"steps/{step_index}/turn/visual/observations")
            check("visible_inventory_coverage", "vision", not missing, inventory, {"observed": observed, "missing": missing},
                  evidence=trusted, available=visual_present, partial=True)
            check("visual_evidence_present", "execution", visual_present, "observation list", visual_present)
            vision_dependency = (visual_id,)
            annotated_slots = {slot["slot"] for slot in annotation["slots"]}
            extras = [observation for observation in visual.get("observations", []) if observation.get("slot") and observation["slot"] not in annotated_slots
                      and observation.get("item_id") and observation.get("quantity") is not None]
            if extras and image != "occluded":
                extra_id = check("unannotated_numeric_observations", "vision", False, "human verification of extra numeric observations", extras, evidence=False)
                vision_dependency += (extra_id,)
            if image == "occluded":
                hidden = dataset["occlusion"]["excluded_item"]
                check("occluded_quantity_not_invented", "vision", not observed.get(hidden), "no hidden count", observed.get(hidden))
        craft = expected_kind in {"craft_zero", "craft_image", "craft_occluded", "no_image_stock"} or expected_kind == "switch" and step_index == 0 or expected_kind == "expired" and step_index > 0
        if craft:
            route = check("craft_route", "intent", calculation.get("operation") == "craft" or calculation.get("status") == "needs_input",
                          "craft calculation or explicit clarification", calculation.get("operation"))
            clarification = calculation.get("status") == "needs_input"
            if clarification:
                check("clarification_appropriateness", "expression", False, "review necessity and actionable question", turn.get("assistant"), evidence=False)
            else:
                complete = calculation.get("status") == "calculated" and isinstance(calculation.get("input"), dict) and isinstance(result.get("acquisition"), dict) and isinstance(result.get("inventory_used"), dict)
                completed = check("calculation_evidence_complete", "execution", complete, "calculated input, acquisition and inventory_used", calculation.get("status"), dependencies=(route,))
                goal = check("craft_goal", "parameters", request.get("goals") == {"explosive.timed": 10}, {"explosive.timed": 10}, request.get("goals"), dependencies=(route,), available=complete)
                if expected_kind in {"no_image_stock", "expired"}:
                    check("no_invalid_inventory", "state", request.get("inventory") is None and result.get("inventory_used") == {},
                          "unknown current inventory; no deductions", {"inventory": request.get("inventory"), "used": result.get("inventory_used")}, available=complete)
                    inventory = {}
                    state_dependency = ()
                    coverage_loss = False
                else:
                    admitted = request.get("inventory")
                    safe_subset = isinstance(admitted, dict) and all(item in inventory and quantity == inventory[item] for item, quantity in admitted.items())
                    coverage_loss = bool(image) and not wrong and safe_subset and admitted != inventory
                    state = check("inventory_admitted", "state", request.get("inventory") == inventory, inventory, request.get("inventory"),
                                  dependencies=vision_dependency, available=complete, partial=bool(image) and not wrong and safe_subset)
                    state_dependency = (state,)
                    if image:
                        check("partial_inventory", "state", request.get("inventory_complete") is False, False, request.get("inventory_complete"), available=complete)
                missing_materials, used = independently_calculated_materials(dataset, inventory)
                check("recipe_arithmetic", "calculation", result.get("acquisition") == missing_materials, missing_materials, result.get("acquisition"),
                      dependencies=(goal, *state_dependency), available=complete, partial=coverage_loss)
                check("inventory_deduction", "calculation", result.get("inventory_used") == used, used, result.get("inventory_used"),
                      dependencies=state_dependency, available=complete, partial=coverage_loss)
                actual_inventory = request.get("inventory") or {}
                actual_missing, actual_used = independently_calculated_materials(dataset, actual_inventory)
                check("arithmetic_given_admitted_inventory", "calculation", result.get("acquisition") == actual_missing and result.get("inventory_used") == actual_used,
                      {"acquisition": actual_missing, "inventory_used": actual_used}, result, available=complete, dependencies=(goal,))
                check("game_version", "rules", dataset["oracle"]["version_status"] == "verified", "independently verified game build",
                      dataset["oracle"]["version_status"], evidence=dataset["oracle"]["version_status"] == "verified")
        if expected_kind == "needs_hp":
            check("damaged_target_not_defaulted", "parameters", calculation.get("status") == "needs_input" and not calculation.get("result"),
                  "needs_input without full-health result", calculation)
        if expected_kind == "switch" and step_index > 0:
            route = check("raid_route", "intent", calculation.get("operation") == "raid", "raid", calculation.get("operation"))
            check("raid_target", "parameters", request.get("target") == "building:base-wall:stone" and request.get("count") == 1 and not request.get("goals"),
                  "one stone wall, no craft goal", {key: request.get(key) for key in ("target", "count", "goals")}, dependencies=(route,))
            hp = None if step_index == 1 else 250
            check("raid_hp_input", "parameters", request.get("hp") == hp, hp, request.get("hp"), dependencies=(route,))
            c4 = next((option for option in result.get("options", []) if option.get("item") == "explosive.timed"), {})
            count = dataset["oracle"]["raid"]["expected_counts"]["500" if step_index == 1 else "250"]
            check("raid_rule_independent_verification", "rules", c4.get("count") == count, count, c4.get("count"), evidence=False)
            check("comparison_scope", "parameters", result.get("comparison") == "minimum_additional_sulfur_among_single_method_options",
                  "minimum_additional_sulfur_among_single_method_options", result.get("comparison"), dependencies=(route,))
        check("answer_present", "expression", isinstance(turn.get("assistant"), str) and bool(turn["assistant"].strip()), "nonempty answer", turn.get("assistant"))
        check("final_expression_review", "expression", False, "review facts, conditions, uncertainty, refusal and questions", "awaiting review", evidence=False,
              ref=f"steps/{step_index}/turn/assistant")
    if record.get("stop_reason"):
        check("evaluation_budget_stop", "execution", False, "run within budget", record["stop_reason"], ref="stop_reason")
        execution_status = "stopped_budget"
    assessment = finalize(checks, execution_status)
    for entry in assessment["checks"]:
        if entry["stage"] == "vision" and entry["status"] == "fail":
            gates = [check for check in checks if check["name"] == "no_invalid_inventory" and check["turn_index"] >= entry["turn_index"]]
            entry["budget_effect"] = "blocked_by_scope_gate" if gates and all(check["status"] == "pass" for check in gates) else "inspect_downstream_evidence"
            entry["answer_effect"] = "requires_per_turn_expression_review"
    return assessment

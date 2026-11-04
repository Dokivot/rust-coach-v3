import copy
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

try:
    from .inventory_grading import GRADER_VERSION, finalize, object_digest
except ImportError:
    from inventory_grading import GRADER_VERSION, finalize, object_digest


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_dataset(dataset, cases_path):
    identifiers = [case["id"] for case in dataset["cases"]]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate_case_id")
    supported = {"craft_zero", "craft_image", "craft_occluded", "no_image_stock", "expired", "needs_hp", "switch"}
    groups = {}
    for case in dataset["cases"]:
        if not case.get("turns") or case["expected"] not in supported:
            raise ValueError("unsupported_case")
        if case.get("split"):
            group = case["source_group"]
            if group in groups and groups[group] != case["split"]:
                raise ValueError("source_group_crosses_splits")
            groups[group] = case["split"]
        for turn in case["turns"]:
            if not isinstance(turn.get("message"), str) or not turn["message"].strip():
                raise ValueError("empty_message")
            if turn.get("image") and "images" in dataset:
                fixture = dataset["images"][turn["image"]]
                if file_digest(cases_path.parent / fixture["path"]) != fixture["sha256"]:
                    raise ValueError("fixture_hash_mismatch")
                if case["source_group"] != fixture["source_group"]:
                    raise ValueError("fixture_source_group_mismatch")
    annotations = [value["annotation"] for value in dataset["images"].values()] if "images" in dataset else [dataset["annotation"]]
    for annotation in annotations:
        if annotation["status"] == "human_confirmed" and not annotation.get("evidence"):
            raise ValueError("confirmation_requires_evidence")
        slots = [slot["slot"] for slot in annotation["slots"]]
        if len(slots) != len(set(slots)):
            raise ValueError("duplicate_annotation_slot")
        if any(not isinstance(slot["quantity"], int) or isinstance(slot["quantity"], bool) or slot["quantity"] < 0 for slot in annotation["slots"]):
            raise ValueError("invalid_annotation_quantity")


def source_manifest(root):
    paths = sorted((root / "src/rust_coach").rglob("*.py"))
    paths += sorted((root / "src/rust_coach").rglob("*.json"))
    return {str(path.relative_to(root)): file_digest(path) for path in paths}


def grader_manifest():
    directory = Path(__file__).parent
    return {name: file_digest(directory / name) for name in ("inventory_baseline.py", "inventory_grading.py", "inventory_evidence.py")}


def summarize(report):
    runs = report["runs"]
    calls = [call for run in runs for call in run.get("model_calls", [])]
    latencies = sorted(step["elapsed_seconds"] for run in runs for step in run.get("steps", []) if "elapsed_seconds" in step)
    usage = [call.get("response", {}).get("usage") for call in calls]
    token_totals = {}
    for label, aliases in {"input_tokens": ("input_tokens", "prompt_tokens"), "output_tokens": ("output_tokens", "completion_tokens"), "total_tokens": ("total_tokens",)}.items():
        values = [next((value[key] for key in aliases if isinstance(value.get(key), int)), None) for value in usage if isinstance(value, dict)]
        token_totals[label] = sum(value for value in values if value is not None) if any(value is not None for value in values) else None
    return {"runs": len(runs), "execution": dict(Counter(run.get("execution_status", run.get("assessment", {}).get("execution_status", "legacy_unknown")) for run in runs)),
            "quality": dict(Counter(run["assessment"]["status"] for run in runs if "assessment" in run)),
            "checks": dict(Counter(check["status"] for run in runs for check in run.get("assessment", {}).get("checks", []))),
            "review_findings": dict(Counter(finding for run in runs for check in run.get("assessment", {}).get("checks", [])
                                            for finding in check.get("review", {}).get("findings", []))),
            "model_calls": len(calls), "failed_model_calls": sum(call.get("status") == "failed" for call in calls),
            "usage_reported_calls": sum(bool(value) for value in usage), "billed_cost": None,
            "reported_token_totals": token_totals, "calls_by_role": dict(Counter(call["role"] for call in calls)),
            "cost_status": "unknown; no provider billing receipt or pricing conversion",
            "latency": {"scope": "HTTP chat requests, includes errors when measured; excludes probe/setup",
                        "samples": len(latencies), "p50_seconds": latencies[math.ceil(len(latencies) * .5) - 1] if latencies else None,
                        "p95_seconds": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None},
            "interpretation": "diagnostic counts; not population accuracy or proof of model improvement"}


def review_template(report, source_sha256):
    entries = []
    for run in report["runs"]:
        for turn_index, step in enumerate(run.get("steps", [])):
            if not step.get("turn"):
                continue
            entries.append({"case_id": run["case_id"], "repeat": run["repeat"], "turn_index": turn_index,
                            "turn_sha256": object_digest(step["turn"]), "status": "pending", "reviewer": None,
                            "reviewer_kind": None, "reason": "", "evidence_refs": [f"steps/{turn_index}/turn/assistant"],
                            "findings": [], "clarification_appropriate": None})
    return {"version": "expression-review-v2", "source_sha256": source_sha256, "revision": 1, "entries": entries}


def apply_reviews(report, review, source_sha256):
    if review.get("version") != "expression-review-v2" or review["source_sha256"] != source_sha256:
        raise ValueError("review_source_mismatch")
    result = copy.deepcopy(report)
    runs = {(run["case_id"], run["repeat"]): run for run in result["runs"]}
    seen = set()
    for entry in review["entries"]:
        key = (entry["case_id"], entry["repeat"], entry["turn_index"])
        if key in seen:
            raise ValueError("conflicting_reviews_require_new_revision")
        seen.add(key)
        run = runs[key[:2]]
        turn = run["steps"][key[2]]["turn"]
        if object_digest(turn) != entry["turn_sha256"]:
            raise ValueError("review_turn_mismatch")
        status = entry["status"]
        if status not in {"pass", "fail", "pending"}:
            raise ValueError("invalid_review_status")
        if status != "pending" and (not entry.get("reviewer") or entry.get("reviewer_kind") not in {"human", "coding_agent"} or not entry.get("reason") or not entry.get("evidence_refs")):
            raise ValueError("review_requires_identity_reason_and_evidence")
        for reference in entry["evidence_refs"]:
            if not reference.startswith(f"steps/{key[2]}/turn/"):
                raise ValueError("review_evidence_outside_turn")
            value = run
            try:
                for component in reference.split("/"):
                    value = value[int(component)] if isinstance(value, list) else value[component]
            except (IndexError, KeyError, ValueError, TypeError) as error:
                raise ValueError("review_evidence_missing") from error
        assessment = run["assessment"]
        expression = next(check for check in assessment["checks"] if check["turn_index"] == key[2] and check["name"] == "final_expression_review")
        expression.update(status=status, actual=entry["reason"], evidence="reviewed" if status != "pending" else "unverified", review=entry,
                          comparison_matches=status == "pass")
        for check in assessment["checks"]:
            if check["turn_index"] == key[2] and check["name"] == "clarification_appropriateness" and status != "pending" and isinstance(entry.get("clarification_appropriate"), bool):
                check.update(status="pass" if entry["clarification_appropriate"] else "fail", evidence="reviewed", actual=entry["reason"])
        run["assessment"] = finalize(assessment["checks"], assessment["execution_status"])
    result["review"] = {"version": review["version"], "sha256": object_digest(review), "revision": review["revision"], "source_sha256": source_sha256,
                        "reviewer_kinds": sorted({entry["reviewer_kind"] for entry in review["entries"] if entry.get("reviewer_kind")}),
                        "reviewers": sorted({entry["reviewer"] for entry in review["entries"] if entry.get("reviewer")})}
    return result


def compare_reports(before, after):
    reasons = []
    for key in ("dataset_sha256", "grader_version", "grader_sha256", "configuration", "rules_sha256", "fixture_sha256"):
        if key not in before or key not in after or before[key] != after[key]:
            reasons.append(f"incompatible_or_missing:{key}")
    for key in ("version", "reviewer_kinds"):
        if before.get("review", {}).get(key) != after.get("review", {}).get(key):
            reasons.append(f"different_review_protocol:{key}")
    indexed = [{(run["case_id"], run["repeat"]): run for run in report["runs"]} for report in (before, after)]
    if set(indexed[0]) != set(indexed[1]):
        reasons.append("different_run_selection")
    pairs = [{"case_id": key[0], "repeat": key[1], "before": indexed[0][key]["assessment"]["status"],
              "after": indexed[1][key]["assessment"]["status"],
              "before_first_error": indexed[0][key]["assessment"].get("first_error_id"),
              "after_first_error": indexed[1][key]["assessment"].get("first_error_id")}
             for key in sorted(set(indexed[0]) & set(indexed[1]))]
    live = before.get("mode") == after.get("mode") == "live"
    same_outputs = before.get("source_sha256") and before.get("source_sha256") == after.get("source_sha256")
    return {"mode": "version_comparison", "comparable": not reasons, "reasons": reasons, "pairs": pairs,
            "experiment": "saved_output_regrading" if same_outputs else "live_repeated_execution" if live else "mixed_or_saved_evidence",
            "model_improvement_proven": False,
            "limitation": "Paired statuses are descriptive. Grader/review changes are not business improvement; small samples do not establish generalization."}


def promote_feedback(dataset, feedback):
    if feedback.get("status") != "approved" or feedback.get("reviewer_kind") != "human" or not feedback.get("reviewer") or not feedback.get("evidence"):
        raise ValueError("feedback_requires_human_verification")
    if feedback.get("dataset_sha256") != object_digest(dataset):
        raise ValueError("feedback_dataset_mismatch")
    result = copy.deepcopy(dataset)
    image = feedback["image"]
    if image not in result.get("images", {}):
        raise ValueError("unknown_feedback_image")
    if not feedback.get("new_version") or feedback["new_version"] == dataset["version"]:
        raise ValueError("feedback_requires_new_dataset_version")
    annotation = copy.deepcopy(feedback["annotation"])
    annotation.update(status="human_confirmed", evidence=feedback["evidence"])
    result["images"][image]["annotation"] = annotation
    result["version"] = feedback["new_version"]
    result["feedback_provenance"] = {"sha256": object_digest(feedback), "reviewer": feedback["reviewer"], "previous_version": dataset["version"]}
    return result

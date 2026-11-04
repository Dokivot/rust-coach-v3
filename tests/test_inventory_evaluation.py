import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from evals import inventory_baseline as runner
from evals.inventory_evidence import apply_reviews, compare_reports, file_digest, promote_feedback, review_template, validate_dataset
from evals.inventory_grading import object_digest, score_case
from rust_coach.config import Settings
from rust_coach.contracts import ModelReply
from test_inventory_baseline import DATASET, case, craft_record


ROOT = Path(__file__).parents[1]


def checked(assessment, name, turn=0):
    return next(check for check in assessment["checks"] if check["name"] == name and check["turn_index"] == turn)


def test_independent_intermediate_material_worksheet():
    missing, used = runner.independently_calculated_materials(DATASET, {"cloth": 40, "techparts": 20, "explosives": 2})
    assert missing == {"metal.fragments": 1980, "lowgradefuel": 594, "sulfur": 21780, "charcoal": 29700, "cloth": 10}
    assert used == {"cloth": 40, "techparts": 20, "explosives": 2}
    missing, used = runner.independently_calculated_materials(DATASET, {"explosive.timed": 10})
    assert missing == {}
    assert used == {"explosive.timed": 10}


def test_uncertain_labels_propagate_but_independent_arithmetic_remains_testable():
    dataset = copy.deepcopy(DATASET)
    dataset["annotation"]["status"] = "pending"
    record = craft_record({"cloth": 946}, [{"item_id": "cloth", "quantity": 946}])
    result = score_case(dataset, case("clear_inventory"), record)
    for name in ("identified_items_and_counts", "visible_inventory_coverage", "inventory_admitted", "inventory_deduction", "recipe_arithmetic"):
        assert checked(result, name)["status"] == "pending"
    assert checked(result, "arithmetic_given_admitted_inventory")["status"] == "pass"
    assert result["status"] == "pending"


def test_absent_calculation_cannot_pass_invalid_inventory_exclusion():
    record = {"steps": [{"turn": {"assistant": "你好", "visual": {"observations": []}}}]}
    result = score_case(DATASET, case("historical_inventory"), record)
    assert checked(result, "no_invalid_inventory")["status"] == "blocked"
    assert result["status"] == "fail"


def test_early_observation_failure_precedes_later_route_failure():
    record = craft_record({"cloth": 946}, [{"item_id": "cloth", "quantity": 946}])
    record["steps"].append({"turn": {"assistant": "你好"}})
    result = score_case(DATASET, case("expired_inventory"), record)
    assert result["first_error_stage"] == "vision"
    assert result["first_error_ref"] == "steps/0/turn/visual/observations"


def test_state_gate_blocks_budget_error_without_erasing_bad_observation():
    record = craft_record({}, [{"item_id": "cloth", "quantity": 946}])
    record["steps"][0]["turn"]["calculation"]["input"]["inventory"] = None
    result = score_case(DATASET, case("opponent_inventory"), record)
    assert checked(result, "identified_items_and_counts")["budget_effect"] == "blocked_by_scope_gate"
    assert checked(result, "no_invalid_inventory")["status"] == "pass"
    assert result["status"] == "fail"


def test_conservative_unknown_is_coverage_loss_not_confident_misidentification():
    record = craft_record({"wood": 8147}, [{"item_id": "wood", "quantity": 8147}])
    result = score_case(DATASET, case("clear_inventory"), record)
    assert checked(result, "identified_items_and_counts")["status"] == "pass"
    assert checked(result, "visible_inventory_coverage")["status"] == "partial"
    assert checked(result, "inventory_admitted")["status"] == "partial"
    assert result["status"] == "pending"


def test_wrong_state_is_not_excused_by_correct_vision():
    record = craft_record({"cloth": 946}, DATASET["annotation"]["slots"])
    result = score_case(DATASET, case("clear_inventory"), record)
    assert checked(result, "inventory_admitted")["status"] == "fail"
    assert result["first_error_stage"] == "state"


def test_incorrect_arithmetic_is_not_mislabeled_as_coverage_loss():
    inventory = {slot["item_id"]: slot["quantity"] for slot in DATASET["annotation"]["slots"]}
    record = craft_record(inventory, DATASET["annotation"]["slots"])
    record["steps"][0]["turn"]["calculation"]["result"]["acquisition"] = {}
    result = score_case(DATASET, case("clear_inventory"), record)
    assert checked(result, "recipe_arithmetic")["status"] == "fail"
    assert result["first_error_stage"] == "calculation"


def test_unannotated_numeric_items_remain_unverified_through_deduction():
    observations = [*DATASET["annotation"]["slots"], {"slot": "row_2_col_1", "item_id": "cloth", "quantity": 20}]
    record = craft_record({"cloth": 20}, observations)
    result = score_case(DATASET, case("clear_inventory"), record)
    assert checked(result, "unannotated_numeric_observations")["status"] == "pending"
    assert checked(result, "inventory_deduction")["status"] == "pending"


def test_per_turn_review_can_finish_clarification_without_overriding_failure():
    record = {"case_id": "damaged_unknown_hp", "repeat": 1, "steps": [{"turn": {"assistant": "请提供当前HP", "calculation": {"status": "needs_input"}}}]}
    record["assessment"] = score_case(DATASET, case(record["case_id"]), record)
    report = {"runs": [record]}
    review = review_template(report, "source-hash")
    review["entries"][0].update(status="pass", reviewer="tester", reviewer_kind="human", reason="Asks for unknown HP and does not use full HP.")
    result = apply_reviews(report, review, "source-hash")
    assert result["runs"][0]["assessment"]["status"] == "pass"
    assert report["runs"][0]["assessment"]["status"] == "pending"
    record["steps"][0]["turn"]["calculation"]["status"] = "calculated"
    record["assessment"] = score_case(DATASET, case(record["case_id"]), record)
    review["entries"][0]["turn_sha256"] = object_digest(record["steps"][0]["turn"])
    assert apply_reviews(report, review, "source-hash")["runs"][0]["assessment"]["status"] == "fail"


@pytest.mark.parametrize("mutation", ["source", "turn", "duplicate", "evidence", "reviewer", "revision"])
def test_review_conflicts_and_missing_provenance_are_rejected(mutation):
    record = craft_record({})
    record.update(case_id="text_craft", repeat=1, assessment=score_case(DATASET, case("text_craft"), record))
    report = {"runs": [record]}
    review = review_template(report, "source-hash")
    entry = review["entries"][0]
    entry.update(status="pass", reviewer="tester", reviewer_kind="human", reason="Verified fixture")
    if mutation == "source":
        review["source_sha256"] = "another-source"
    elif mutation == "turn":
        entry["turn_sha256"] = "another-turn"
    elif mutation == "duplicate":
        review["entries"].append(copy.deepcopy(entry))
    elif mutation == "evidence":
        entry["evidence_refs"] = ["steps/0/turn/nonexistent"]
    elif mutation == "revision":
        review["revision"] = 0
    else:
        entry["reviewer"] = None
    with pytest.raises(ValueError):
        apply_reviews(report, review, "source-hash")


def test_comparison_rejects_grader_changes_and_does_not_claim_improvement():
    report = {key: "same" for key in ("dataset_sha256", "grader_version", "grader_sha256", "configuration", "rules_sha256", "fixture_sha256")}
    report.update(mode="saved_output_rescore", source_sha256="same-captured-output", runs=[])
    after = copy.deepcopy(report)
    assert compare_reports(report, after)["comparable"]
    after["grader_version"] = "new-grader"
    result = compare_reports(report, after)
    assert not result["comparable"]
    assert result["experiment"] == "saved_output_regrading"
    assert not result["model_improvement_proven"]


def test_behavior_affecting_configuration_is_captured_and_compared():
    settings = Settings(_env_file=None, api_key="fixture-secret", vision_detail="original", history_turns=7)
    configuration = runner.evaluation_configuration(settings)
    assert configuration["schema"] == "evaluation-configuration-v2"
    assert configuration["vision_detail"] == "original"
    assert configuration["history_turns"] == 7
    assert configuration["vision_max_output_tokens"] == 8192

    report = {key: "same" for key in ("dataset_sha256", "grader_version", "grader_sha256", "rules_sha256", "fixture_sha256")}
    report.update(mode="live", configuration=configuration, runs=[])
    after = copy.deepcopy(report)
    after["configuration"]["vision_detail"] = "high"
    result = compare_reports(report, after)
    assert not result["comparable"]
    assert "incompatible_or_missing:configuration" in result["reasons"]


def test_feedback_requires_human_verification_and_new_version():
    dataset = json.loads((ROOT / "evals/inventory-acceptance-v2.json").read_text())
    feedback = {"status": "pending", "dataset_sha256": object_digest(dataset), "image": "components",
                "new_version": "test-v3", "annotation": dataset["images"]["components"]["annotation"]}
    with pytest.raises(ValueError, match="human_verification"):
        promote_feedback(dataset, feedback)
    feedback.update(status="approved", reviewer="human-reviewer", reviewer_kind="human", evidence="Human rechecked source pixels")
    promoted = promote_feedback(dataset, feedback)
    assert promoted["version"] == "test-v3"
    assert dataset["version"] == "inventory-acceptance-v2"
    validate_dataset(promoted, ROOT / "evals/inventory-acceptance-v2.json")


def test_same_source_scene_cannot_cross_splits():
    dataset = json.loads((ROOT / "evals/inventory-acceptance-v2.json").read_text())
    dataset["cases"][3]["split"] = "diagnostic"
    with pytest.raises(ValueError, match="crosses_splits"):
        validate_dataset(dataset, ROOT / "evals/inventory-acceptance-v2.json")


async def test_runner_drives_real_upload_sse_and_persistence_with_recorded_fixture_model(tmp_path, monkeypatch):
    numbers = iter([123456, 234567])
    monkeypatch.setattr("rust_coach.vision.probe.secrets.randbelow", lambda upper: next(numbers))

    class FixtureModel:
        async def complete(self, messages):
            parts = next(message.content for message in messages if isinstance(message.content, list))
            if len([part for part in parts if getattr(part, "type", None) == "image"]) == 2:
                return ModelReply(content=json.dumps({"codes": ["223456", "334567"]}))
            context = json.loads(parts[0].text)
            attachment_id = context["images"][0]["attachment_id"]
            observations = [{**slot, "id": slot["slot"], "attachment_id": attachment_id, "kind": "inventory", "region_kind": "backpack",
                             "label": slot["item_id"], "item_status": "identified", "quantity_status": "readable", "bbox": [.1, .1, .2, .2]}
                            for slot in DATASET["annotation"]["slots"]]
            return ModelReply(content=json.dumps({"task": "craft", "observations": observations, "inventory_coverage": "partial",
                                                 "calculation": {"operation": "craft", "goals": {"explosive.timed": 10}}}))

    monkeypatch.setattr(runner, "CompatibleChatModel", lambda *args, **kwargs: FixtureModel())
    settings = Settings(_env_file=None, api_key="fixture-secret", search_provider="off", database_path=tmp_path / "must-not-exist.sqlite3")
    record = {"steps": [], "model_calls": []}
    args = SimpleNamespace(cases=ROOT / "evals/inventory-baseline.json")
    async with httpx.AsyncClient() as client:
        await runner.run_case(DATASET, case("clear_inventory"), args, settings, client, record, lambda: None)
    assert not settings.database_path.exists()
    assert record["saved_turn_count"] == 1
    assert record["steps"][0]["status"] == "completed"
    assert record["steps"][0]["attachment"]["id"]
    assert record["steps"][0]["fixture_sha256"]
    assert record["model_calls"][0]["step"] == -1
    assert score_case(DATASET, case("clear_inventory"), record)["status"] == "pending"


async def test_budget_stop_keeps_manifest_and_does_not_call_provider(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, api_key="fixture-secret", search_provider="off")
    monkeypatch.setattr(runner, "Settings", lambda: settings)

    class ForbiddenModel:
        async def complete(self, messages):
            raise AssertionError("Budget must stop before provider call")

    monkeypatch.setattr(runner, "CompatibleChatModel", lambda *args, **kwargs: ForbiddenModel())
    args = SimpleNamespace(cases=ROOT / "evals/inventory-baseline.json", only=["clear_inventory", "opponent_inventory"], repeat=1,
                           output=tmp_path / "run.json", portable_output=tmp_path / "portable.json", max_model_calls=0, max_seconds=30)
    await runner.live(args, DATASET)
    report = json.loads(args.output.read_text())
    assert report["status"] == "stopped_budget"
    assert report["budget"]["actual_model_calls"] == 0
    assert [run["execution_status"] for run in report["runs"]] == ["stopped_budget", "not_run"]
    assert report["runs"][1]["assessment"]["status"] == "not_run"
    assert args.portable_output.exists()


async def test_wall_budget_cancels_inflight_call_and_survives_regrading(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "Settings", lambda: Settings(_env_file=None, api_key="fixture-secret", search_provider="off"))

    class SlowModel:
        async def complete(self, messages):
            await asyncio.sleep(2)
            return ModelReply(content="unexpected late reply")

    monkeypatch.setattr(runner, "CompatibleChatModel", lambda *args, **kwargs: SlowModel())
    args = SimpleNamespace(cases=ROOT / "evals/inventory-baseline.json", only=["clear_inventory", "opponent_inventory"], repeat=1,
                           output=tmp_path / "run.json", portable_output=None, max_model_calls=10, max_seconds=.1)
    await runner.live(args, DATASET)
    report = json.loads(args.output.read_text())
    assert report["status"] == "stopped_budget"
    assert report["budget"]["actual_model_calls"] == 1
    assert report["runs"][0]["model_calls"][0]["status"] == "failed"
    result = score_case(DATASET, case("clear_inventory"), report["runs"][0])
    assert result["execution_status"] == "stopped_budget"
    assert result["status"] == "fail"
    assert report["runs"][1]["execution_status"] == "not_run"


def test_fixture_hash_change_is_rejected():
    dataset = json.loads((ROOT / "evals/inventory-acceptance-v2.json").read_text())
    dataset["images"]["wide"]["sha256"] = "changed"
    with pytest.raises(ValueError, match="fixture_hash_mismatch"):
        validate_dataset(dataset, ROOT / "evals/inventory-acceptance-v2.json")


def test_captured_run_manifest_rejects_duplicate_and_mismatched_cases():
    dataset = json.loads((ROOT / "evals/inventory-acceptance-v2.json").read_text())
    selected = dataset["cases"][:2]
    report = {
        "selection": {"case_ids": [case["id"] for case in selected], "repeat": 1, "planned_runs": 2},
        "runs": [{"case_id": case["id"], "repeat": 1, "expected": case, "steps": [], "model_calls": []}
                 for case in selected],
    }
    runner.validate_captured_runs(report, dataset)
    report["runs"][1] = copy.deepcopy(report["runs"][0])
    with pytest.raises(ValueError, match="duplicate_captured_run"):
        runner.validate_captured_runs(report, dataset)
    report["runs"][1]["case_id"] = selected[1]["id"]
    with pytest.raises(ValueError, match="case_mismatch"):
        runner.validate_captured_runs(report, dataset)


def test_saved_real_run_proves_failure_and_clarification_without_conflating_arithmetic():
    dataset_path = ROOT / "evals/inventory-acceptance-v2.json"
    capture_path = ROOT / "evals/results/inventory-acceptance-v2-live-2026-11-05.json"
    review_path = ROOT / "evals/results/inventory-acceptance-v2-review-2026-11-05.json"
    reviewed_path = ROOT / "evals/results/inventory-acceptance-v2-reviewed-2026-11-05.json"
    dataset = json.loads(dataset_path.read_text())
    report = json.loads(capture_path.read_text())
    runner.validate_captured_runs(report, dataset)
    cases = {case["id"]: case for case in dataset["cases"]}
    rescored = copy.deepcopy(report)
    for record in rescored["runs"]:
        record["assessment"] = score_case(dataset, cases[record["case_id"]], record)
    reviewed = apply_reviews(rescored, json.loads(review_path.read_text()), file_digest(capture_path))
    runs = {record["case_id"]: record for record in reviewed["runs"]}
    saved = {record["case_id"]: record for record in json.loads(reviewed_path.read_text())["runs"]}
    assert all(record["assessment"] == saved[case_id]["assessment"] for case_id, record in runs.items())

    components = runs["components_craft"]["assessment"]
    assert components["status"] == "fail"
    assert components["first_error_stage"] == "vision"
    assert checked(components, "arithmetic_given_admitted_inventory")["status"] == "pass"
    assert checked(components, "recipe_arithmetic")["status"] == "fail"
    assert runs["damaged_unknown_hp"]["assessment"]["status"] == "pass"

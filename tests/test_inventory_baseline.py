import copy
import json
from pathlib import Path

from evals.inventory_baseline import independently_calculated_materials, score_case


DATASET = json.loads((Path(__file__).parents[1] / "evals/inventory-baseline.json").read_text())


def case(identifier):
    return next(value for value in DATASET["cases"] if value["id"] == identifier)


def craft_record(inventory, observations=()):
    acquisition, used = independently_calculated_materials(DATASET, inventory)
    return {"steps": [{"turn": {
        "calculation": {"status": "calculated", "operation": "craft",
                        "input": {"goals": {"explosive.timed": 10}, "inventory": inventory, "inventory_complete": False},
                        "result": {"acquisition": acquisition, "inventory_used": used}},
        "visual": {"observations": list(observations)}, "assistant": "fixture output for scorer tests only"
    }}]}


def test_independent_worksheet_handles_confirmed_inventory():
    inventory = {"wood": 8147, "stones": 550, "metal.fragments": 946, "charcoal": 4, "sulfur": 140}
    missing, used = independently_calculated_materials(DATASET, inventory)
    assert missing == {"techparts": 20, "cloth": 50, "metal.fragments": 1054,
                       "lowgradefuel": 600, "sulfur": 21860, "charcoal": 29996}
    assert used == {"metal.fragments": 946, "sulfur": 140, "charcoal": 4}


def test_matching_arithmetic_does_not_prove_current_game_version_or_expression():
    result = score_case(DATASET, case("text_craft"), craft_record({}))
    assert result["status"] == "pending"
    assert result["first_error_stage"] is None
    assert {check["name"] for check in result["checks"] if check["status"] == "pending"} == {
        "game_version", "final_expression_review"}


def test_misidentification_is_first_error_before_downstream_deduction():
    inventory = {"wood": 8147, "metal.fragments": 550, "cloth": 946, "charcoal": 4, "sulfur": 140}
    observations = [{"item_id": item, "quantity": amount} for item, amount in inventory.items()]
    result = score_case(DATASET, case("clear_inventory"), craft_record(inventory, observations))
    assert result["status"] == "fail"
    assert result["first_error_stage"] == "vision"
    assert any(check["name"] == "recipe_arithmetic" and check["status"] == "fail" for check in result["checks"])


def test_unconfirmed_annotation_is_not_accepted_as_visual_ground_truth():
    dataset = copy.deepcopy(DATASET)
    dataset["annotation"]["status"] = "pending"
    inventory = {slot["item_id"]: slot["quantity"] for slot in dataset["annotation"]["slots"]}
    observations = [{"item_id": item, "quantity": amount} for item, amount in inventory.items()]
    result = score_case(dataset, case("clear_inventory"), craft_record(inventory, observations))
    visual = next(check for check in result["checks"] if check["name"] == "identified_items_and_counts")
    assert visual["status"] == "pending"


def test_expired_stock_cannot_pass_with_deductions():
    record = craft_record({"sulfur": 140})
    observations = DATASET["annotation"]["slots"]
    record["steps"].insert(0, {"turn": {"visual": {"observations": observations}, "assistant": "observation"}})
    result = score_case(DATASET, case("expired_inventory"), record)
    assert result["status"] == "fail"
    assert result["first_error_stage"] == "state"


def test_missing_hp_full_health_answer_is_a_failure():
    record = {"steps": [{"turn": {"calculation": {"status": "calculated", "input": {"hp": None}}}}]}
    result = score_case(DATASET, case("damaged_unknown_hp"), record)
    assert result["first_error_stage"] == "parameters"


def test_external_failure_is_not_counted_as_correct_refusal():
    record = {"steps": [{"error": {"code": "model_timeout"}}]}
    result = score_case(DATASET, case("damaged_unknown_hp"), record)
    assert result["status"] == "fail"
    assert result["first_error_stage"] == "external_service"


def test_partial_multi_turn_run_is_not_a_pass():
    result = score_case(DATASET, case("craft_to_raid"), craft_record({}))
    assert result["status"] == "fail"
    assert result["first_error_stage"] == "execution"

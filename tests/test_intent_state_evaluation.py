import copy
from pathlib import Path

import pytest

from evals.intent_state import FixtureModel, compare_reports, evaluate, run_case
from evals.intent_state_live import check_turn
from rust_coach.contracts import AppError


def test_frozen_intent_state_cases_through_http_and_sse():
    report = evaluate(Path(__file__).resolve().parents[1] / "evals" / "intent-state-cases.json")
    assert report["case_count"] == 40
    assert report["turn_count"] == 54
    assert report["failed"] == 0, [case for case in report["results"] if not case["passed"]]
    assert {case["category"] for case in report["results"]} == {"route", "task", "inventory", "event", "image", "state", "failure"}
    incompatible = copy.deepcopy(report)
    incompatible["dataset_sha256"] = "another-dataset"
    with pytest.raises(ValueError, match="incompatible_evaluation"):
        compare_reports(incompatible, report)


def test_model_failure_is_not_scored_as_correct_missing_inventory(tmp_path, monkeypatch):
    async def unavailable(self, messages):
        raise AppError("model_unavailable", "测试不可用", 503)

    monkeypatch.setattr(FixtureModel, "complete", unavailable)
    case = {"id": "failure", "category": "failure", "steps": [{"message": "制作一个C4",
        "expected": {"inventory": None, "profile_revision": 1}}]}
    result = run_case(case, tmp_path)
    assert result["passed"] is False
    assert result["first_failed_turn"] == 0
    assert any(check["field"] == "terminal_type" and not check["passed"] for check in result["turns"][0]["checks"])


@pytest.mark.parametrize("calculation", [
    {"status": "needs_input", "input": {"operation": "craft"}},
    {"status": "calculated", "input": {"operation": "craft"}},
    {"status": "calculated", "result": {}},
])
def test_live_grader_rejects_matching_input_without_completed_calculation(calculation):
    checks = check_turn({"type": "result", "turn": {"calculation": calculation}},
                        {"operation": "craft"}, {"operation": "craft"})
    assert not all(check["passed"] for check in checks)


def test_live_grader_accepts_completed_result_but_rejects_wrong_shortfall():
    terminal = {"type": "result", "turn": {"calculation": {"status": "calculated", "result": {"acquisition": {"sulfur": 3400}}}}}
    expected = {"operation": "craft", "sulfur_needed": 3400}
    assert all(check["passed"] for check in check_turn(terminal, expected, expected))
    assert not all(check["passed"] for check in check_turn(terminal, expected | {"sulfur_needed": 4400}, expected))

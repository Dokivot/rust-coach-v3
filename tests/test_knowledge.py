import copy
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from rust_coach.app import create_app
from rust_coach.calculations import Calculator
from rust_coach.calculations.schemas import CalculationRequest
from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelReply
from rust_coach.knowledge.bundle import default_release, release_identity
from rust_coach.knowledge.manage import main
from rust_coach.knowledge.schemas import KnowledgeFeedback
from rust_coach.knowledge.service import LocalKnowledge
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime


class NoModel:
    async def complete(self, messages):
        raise AssertionError("Fixed knowledge must not call a model")


@pytest.fixture
def knowledge(tmp_path):
    return LocalKnowledge(ConversationStore(tmp_path / "knowledge.sqlite3"))


def publish(knowledge, package):
    installed = knowledge.registry.install(package, knowledge.catalog.sha256)
    knowledge.registry.activate(package["id"], installed["release_sha256"], "test fixture review, no game certification")
    return installed


def send(client, owner, message, **kwargs):
    response = client.post(f"/api/conversations/{owner}/messages", json={
        "request_id": str(uuid4()), "message": message, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def test_release_is_immutable_and_activation_is_hash_checked(knowledge):
    initial = knowledge.reference()
    package = default_release()
    package["id"] = "candidate-v2"
    installed = knowledge.registry.install(package, knowledge.catalog.sha256)
    assert installed["release_sha256"] == release_identity(package)
    assert knowledge.reference() == initial
    with pytest.raises(AppError, match="发布包"):
        knowledge.registry.activate(package["id"], "0" * 64, "review")
    assert knowledge.reference() == initial
    package["documents"][-1]["title"] += " changed"
    with pytest.raises(AppError) as error:
        knowledge.registry.install(package, knowledge.catalog.sha256)
    assert error.value.code == "knowledge_release_immutable"
    knowledge.registry.activate("candidate-v2", installed["release_sha256"], "reviewed scope and provenance only")
    assert LocalKnowledge(knowledge.registry.store).reference()["release_id"] == "candidate-v2"
    knowledge.registry.activate(initial["release_id"], initial["release_sha256"], "rollback")
    assert knowledge.reference()["registry_revision"] > initial["registry_revision"]


@pytest.mark.parametrize("change", ["rules_hash", "rule_text", "rule_shadow", "extra_rule"])
def test_rule_package_cannot_override_numeric_authority(knowledge, change):
    package = default_release()
    package["id"] = change
    if change == "rules_hash":
        package["rules_sha256"] = "0" * 64
    elif change == "rule_text":
        package["documents"][0]["text"] = "invented recipe"
    elif change == "rule_shadow":
        package["documents"][0]["kind"] = "explanation"
    else:
        package["documents"][0]["id"] = "recipe:invented"
    initial = knowledge.reference()
    with pytest.raises(AppError):
        knowledge.registry.install(package, knowledge.catalog.sha256)
    assert knowledge.reference() == initial


@pytest.mark.parametrize("field,value,reason", [
    ("snapshot", "2025-old", "scope_mismatch"), ("platform", "console", "scope_mismatch"),
    ("branch", "staging", "scope_mismatch"), ("ruleset", "modded", "scope_mismatch"),
    ("admission", "pending", "pending"), ("admission", "conflicted", "conflicted"),
])
def test_eligibility_precedes_retrieval_and_wrong_evidence_is_not_rendered(knowledge, field, value, reason):
    package = default_release()
    package["id"] = "ineligible-fixture"
    document = next(item for item in package["documents"] if item["id"] == "explain:rounding")
    document[field] = value
    publish(knowledge, package)
    result = knowledge.retrieve("为什么石墙拆除数量向上取整")
    assert result["status"] == "missing_evidence"
    assert {"id": "explain:rounding", "reason": reason} in result["excluded"]
    assert "claim:rounding" in result["missing"]
    assert "ceil(" not in knowledge.render(result)


def test_explanations_are_found_by_fts_not_reserved_ids(knowledge):
    package = default_release()
    package["id"] = "renamed-explanation"
    document = next(item for item in package["documents"] if item["id"] == "explain:rounding")
    document["id"] = "curated:rounding-alternative"
    publish(knowledge, package)
    result = knowledge.retrieve("为什么石墙数量向上取整")
    assert result["status"] == "supported_snapshot"
    assert "curated:rounding-alternative" in {item["id"] for item in result["documents"]}
    assert knowledge.retrieve("为什么石墙数量向上取整")["cache_hit"]


def test_previous_target_and_claims_are_both_required(knowledge):
    previous = {"operation": "raid", "target": "building:base-wall:stone", "hp": 250}
    result = knowledge.retrieve("为什么250血还需要一颗C4", previous)
    assert {item["id"] for item in result["documents"]} >= {
        "raid:building:base-wall:stone", "recipe:explosive.timed", "explain:rounding"}
    assert result["model_calls"] == result["network_calls"] == 0


@pytest.mark.parametrize("message,status", [
    ("C4最新版本配方是什么", "latest_unverified"),
    ("主机版C4配方是什么", "scope_mismatch"),
    ("2025-11-01的C4配方是什么", "scope_mismatch"),
    ("/knowledge 为什么C4会吸引巡逻直升机", "missing_evidence"),
    ("/knowledge C4伤害翻倍的原理", "missing_evidence"),
    ("/knowledge 请解释电子零件的真实化学机制", "missing_evidence"),
])
def test_unsupported_questions_do_not_get_unrelated_recipes(knowledge, message, status):
    result = knowledge.retrieve(message)
    assert result["status"] == status
    assert result["documents"] == []


def test_context_budget_and_index_failure_are_explicit(knowledge):
    assert knowledge.retrieve("C4配方是什么", max_chars=1)["status"] == "missing_evidence"
    with knowledge.registry.store.connection() as connection:
        connection.execute("DROP TABLE knowledge_fts")
    result = knowledge.retrieve("为什么石墙向上取整")
    assert result["status"] == "index_unavailable"
    assert result["documents"] == []
    assert knowledge.retrieve("C4配方是什么")["status"] == "supported_snapshot"


def test_source_captures_match_saved_crosscheck_and_keep_unknown_build(knowledge):
    report = json.loads((Path(__file__).resolve().parents[1] / "evals/results/inventory-rule-evidence-2026-11-05.json").read_text())
    snapshot = knowledge.registry.snapshot()
    assert snapshot["release"]["game_build"] is None
    excerpts = [document for document in snapshot["release"]["documents"] if document["kind"] == "captured_excerpt"]
    assert len(excerpts) == 3
    for excerpt in excerpts:
        captured = next(source for source in report["sources"] if source["url"] == excerpt["source"])
        assert (excerpt["text"], excerpt["source_hash"], excerpt["fetched_at"]) == (
            captured["excerpt"], captured["sha256"], captured["retrieved_at"])


def test_failed_install_rolls_back_every_index_row_and_corruption_is_detected(knowledge):
    original = knowledge.reference()
    with knowledge.registry.store.connection() as connection:
        connection.execute("CREATE TRIGGER fail_install BEFORE INSERT ON knowledge_units WHEN NEW.release_id='broken' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    package = default_release()
    package["id"] = "broken"
    with pytest.raises(sqlite3.IntegrityError):
        knowledge.registry.install(package, knowledge.catalog.sha256)
    assert knowledge.reference() == original
    with knowledge.registry.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_releases WHERE id='broken'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM knowledge_fts WHERE release_id='broken'").fetchone()[0] == 0
        connection.execute("UPDATE knowledge_releases SET payload='{}'")
    with pytest.raises(AppError) as error:
        knowledge.reference()
    assert error.value.code == "knowledge_integrity_error"


def test_revocation_invalidates_cache_and_survives_rollback(knowledge):
    initial = knowledge.reference()
    assert knowledge.retrieve("C4配方是什么")["status"] == "supported_snapshot"
    assert knowledge.retrieve("C4配方是什么")["cache_hit"]
    knowledge.registry.revoke(initial["release_id"], "recipe:explosive.timed", "suspect recipe")
    result = knowledge.retrieve("C4配方是什么")
    assert not result["cache_hit"]
    assert result["status"] == "missing_evidence"
    with pytest.raises(AppError):
        knowledge.registry.validate(initial)
    knowledge.registry.activate(initial["release_id"], initial["release_sha256"], "rollback does not undo revocation")
    assert LocalKnowledge(knowledge.registry.store).retrieve("C4配方是什么")["status"] == "missing_evidence"


@pytest.mark.parametrize("inputs,dependency", [
    ({"operation": "craft", "goals": {"explosive.timed": 1}}, "recipe:explosives"),
    ({"operation": "raid", "target": "building:base-wall:stone"}, "raid:building:base-wall:stone"),
    ({"operation": "raid", "target": "building:base-wall:stone", "expand_fuel": True}, "recipe:lowgradefuel"),
    ({"operation": "craft", "goals": {"gunpowder": 10}, "gunpowder_recipe": "mixing_table"}, "recipe:mix"),
])
def test_calculation_dependency_closure_blocks_revoked_rules(knowledge, inputs, dependency):
    calculator = Calculator(knowledge)
    payload = CalculationRequest.model_validate(inputs)
    result = calculator.calculate(payload)
    assert dependency in result["knowledge"]["rule_dependencies"]
    knowledge.registry.revoke(knowledge.reference()["release_id"], dependency, "test revocation")
    with pytest.raises(AppError) as error:
        calculator.calculate(payload)
    assert error.value.code == "knowledge_evidence_unavailable"


def test_mixing_recipe_does_not_depend_on_workbench_recipe(knowledge):
    knowledge.registry.revoke(knowledge.reference()["release_id"], "recipe:gunpowder", "workbench only")
    result = Calculator(knowledge).calculate(CalculationRequest(operation="craft", goals={"gunpowder": 10}, gunpowder_recipe="mixing_table"))
    assert result["status"] == "calculated"


def test_http_sse_calculation_feedback_and_protected_evidence(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "http.sqlite3", api_key="", search_provider="off")
    application = create_app(settings, NoModel())
    with TestClient(application) as client:
        owner = client.post("/api/conversations", json={}).json()["id"]
        turn = send(client, owner, "C4配方是什么")
        assert "工作台等级3" in turn["assistant"]
        assert turn["presentation"]["knowledge"]["status"] == "supported_snapshot"
        events = turn["presentation"]["architecture"]["trace"]["events"]
        assert next(index for index, event in enumerate(events) if event["layer"] == "knowledge") < next(
            index for index, event in enumerate(events) if event["event"] == "decision.finalized")
        assert next(block for block in turn["presentation"]["blocks"] if block["id"] == "chat")["kind"] == "protected"
        response = client.post(f"/api/conversations/{owner}/messages", headers={"Accept": "text/event-stream"},
                               json={"request_id": str(uuid4()), "message": "C4来源依据是什么"})
        assert response.status_code == 200
        assert "excerpt:c4" in response.text
        assert client.get("/api/knowledge/documents/recipe:explosive.timed").status_code == 200
        feedback = {"request_id": "report-1", "turn_request_id": turn["request_id"],
                    "document_ids": ["recipe:explosive.timed"], "reason": "其他攻略说法不同"}
        endpoint = f"/api/conversations/{owner}/knowledge-feedback"
        first = client.post(endpoint, json=feedback)
        assert first.status_code == 200
        assert first.json()["status"] == "pending_review"
        assert client.post(endpoint, json=feedback).json() == first.json()
        assert client.post(endpoint, json={**feedback, "reason": "changed"}).status_code == 409
        assert client.post(endpoint, json={**feedback, "document_ids": ["recipe:gunpowder"]}).status_code == 422
        payload = {"operation": "craft", "goals": {"explosive.timed": 10}}
        calculation = client.post("/api/calculations", json=payload).json()
        assert calculation["result"]["acquisition"] == {"cloth": 50, "techparts": 20, "metal.fragments": 2000,
            "lowgradefuel": 600, "sulfur": 22000, "charcoal": 30000}
        registry = application.state.runtime.knowledge.registry
        registry.revoke(turn["presentation"]["knowledge"]["release_id"], "recipe:explosive.timed", "fixture withdrawal")
        assert client.post("/api/calculations", json=payload).status_code == 422
        replay = client.post(f"/api/conversations/{owner}/messages", json={"request_id": turn["request_id"], "message": turn["user"]})
        assert replay.status_code == 409
        blocked = send(client, owner, '/calc {"operation":"craft","goals":{"explosive.timed":1}}')
        assert blocked["calculation"]["error_code"] == "knowledge_evidence_unavailable"
        assert blocked["calculation"].get("result") is None
        assert client.delete(f"/api/conversations/{owner}").status_code == 204
        with registry.store.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM knowledge_feedback").fetchone()[0] == 0


async def test_inflight_change_prevents_save_and_stale_history_is_excluded(tmp_path):
    class RecordingModel:
        def __init__(self):
            self.messages = []
            self.change = None

        async def complete(self, messages):
            self.messages = messages
            if self.change:
                self.change()
            return ModelReply(content="继续讨论")

    settings = Settings(_env_file=None, database_path=tmp_path / "runtime.sqlite3")
    store = ConversationStore(settings.database_path)
    model = RecordingModel()
    runtime = ChatRuntime(settings, model, store)
    owner = store.create().id
    await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="C4配方是什么"))
    registry = runtime.knowledge.registry
    release = runtime.knowledge.reference()["release_id"]
    registry.revoke(release, "recipe:explosive.timed", "withdrawal")
    await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="你好"))
    assert [message.role for message in model.messages] == ["system", "user"]
    model.change = lambda: registry.revoke(release, "recipe:explosives", "inflight change")
    with pytest.raises(AppError) as error:
        await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="谢谢"))
    assert error.value.code == "knowledge_changed"
    assert len(store.get(owner).turns) == 2


def test_feedback_review_and_cli_does_not_publish_facts(knowledge, tmp_path, capsys):
    owner = knowledge.registry.store.create().id
    reference = knowledge.retrieve("C4配方是什么")
    request = KnowledgeFeedback(request_id="report", turn_request_id="turn", document_ids=["recipe:explosive.timed"], reason="待核实")
    knowledge.registry.feedback(owner, request, reference)
    initial = knowledge.reference()
    reviewed = knowledge.registry.review_feedback(owner, "report", "needs_release", "requires a separate reviewed package")
    assert reviewed["status"] == "needs_release"
    assert knowledge.reference() == initial
    database = str(knowledge.registry.store.path)
    output = tmp_path / "export.json"
    assert main(["--database", database, "export", str(output)]) == 0
    assert main(["--database", database, "export", str(output)]) == 1
    assert main(["--database", database, "install", str(output)]) == 0
    assert main(["--database", database, "activate", initial["release_id"], "--sha256", initial["release_sha256"], "--review-note", "rollback"]) == 0
    assert main(["--database", database, "feedback"]) == 0
    assert "needs_release" in capsys.readouterr().out


def test_fixed_evaluation_and_grader_reject_missing_or_ineligible_support():
    from evals.knowledge import evaluate, grade

    cases_path = Path(__file__).resolve().parents[1] / "evals/knowledge-cases.json"
    report = evaluate(cases_path)
    assert report["case_count"] == 24
    assert report["summary"]["B2_exact_fts"]["passed"] == 24
    assert report["summary"]["B2_exact_fts"]["reserved_passed"] == 8
    case = json.loads(cases_path.read_text())["cases"][0]
    result = {"status": "supported_snapshot", "documents": [{"id": "unrelated"}],
              "context_chars": 10, "model_calls": 0, "network_calls": 0}
    assert not grade(case, result, {"unrelated"})["passed"]
    invalid = copy.deepcopy(result)
    invalid["documents"] = [{"id": "recipe:explosive.timed"}]
    assert not grade(case, invalid, set())["passed"]

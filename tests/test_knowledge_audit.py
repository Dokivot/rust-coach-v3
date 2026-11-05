from uuid import uuid4
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from rust_coach.calculations import Calculator
from rust_coach.calculations.schemas import CalculationRequest
from rust_coach.contracts import AppError, ChatRequest, ModelReply
from rust_coach.knowledge.bundle import default_release
from rust_coach.knowledge.service import LocalKnowledge
from rust_coach.runtime import ChatRuntime
from rust_coach.config import Settings
from rust_coach.persistence import ConversationStore
from test_knowledge import NoModel, knowledge, publish


@pytest.mark.parametrize("message,identifier", [
    ("火箭弹配方是什么", "recipe:ammo.rocket.basic"),
    ("高速火箭弹配方是什么", "recipe:ammo.rocket.hv"),
    ("石墙满血多少？", "raid:building:base-wall:stone"),
    ("钢铁门满血多少？", "raid:item:door.hinged.toptier"),
    ("C4配方", "recipe:explosive.timed"),
    ("炸药包配方是什么", "recipe:explosive.satchel"),
])
async def test_registered_numeric_questions_reach_local_knowledge_without_model(tmp_path, message, identifier):
    store = ConversationStore(tmp_path / "chat.sqlite3")
    runtime = ChatRuntime(Settings(_env_file=None, database_path=store.path), NoModel(), store)
    result = await runtime.chat(store.create().id, ChatRequest(request_id=uuid4(), message=message))
    evidence = result.presentation["knowledge"]
    assert evidence["status"] == "supported_snapshot"
    assert {document["id"] for document in evidence["documents"]} == {identifier}


@pytest.mark.parametrize("message", [
    "2026年11月5日C4配方是什么", "2026/11/05的C4配方是什么", "build 12345的C4配方是什么",
    "2026年11月C4配方是什么",
    "11月5日C4配方是什么", "版本v123的C4配方是什么",
])
def test_explicit_other_version_never_uses_demo_rules(knowledge, message):
    result = knowledge.retrieve(message)
    assert result["status"] in {"scope_mismatch", "latest_unverified"}
    assert result["documents"] == []


def test_revoked_identical_rule_does_not_return_in_new_release(knowledge):
    reference = knowledge.reference()
    knowledge.registry.revoke(reference["release_id"], "recipe:explosive.timed", "wrong or outdated evidence")
    package = default_release()
    package["id"] = "copy-after-revocation"
    publish(knowledge, package)
    assert knowledge.retrieve("C4配方是什么")["status"] == "missing_evidence"
    with pytest.raises(AppError) as error:
        Calculator(knowledge).calculate(CalculationRequest(operation="craft", goals={"explosive.timed": 1}))
    assert error.value.code == "knowledge_evidence_unavailable"


def test_revoked_explanation_cannot_be_restored_by_renaming(knowledge):
    knowledge.registry.revoke(knowledge.reference()["release_id"], "explain:rounding", "invalid explanation")
    package = default_release()
    package["id"] = "renamed-withdrawn-explanation"
    next(document for document in package["documents"] if document["id"] == "explain:rounding")["id"] = "explain:renamed"
    publish(knowledge, package)
    assert knowledge.retrieve("为什么石墙向上取整")["status"] == "missing_evidence"


async def test_old_general_answer_cannot_replay_after_knowledge_change(tmp_path):
    class Model:
        async def complete(self, messages):
            return ModelReply(content="先核对入口情况。")

    store = ConversationStore(tmp_path / "replay.sqlite3")
    runtime = ChatRuntime(Settings(_env_file=None, database_path=store.path), Model(), store)
    owner = store.create().id
    request = ChatRequest(request_id=uuid4(), message="你好")
    turn = await runtime.chat(owner, request)
    assert "knowledge_context" in turn.presentation
    runtime.knowledge.registry.revoke(runtime.knowledge.reference()["release_id"], "explain:inventory", "withdrawn")
    with pytest.raises(AppError) as error:
        await runtime.chat(owner, request)
    assert error.value.code == "knowledge_changed"


@pytest.mark.parametrize("payload", ["{", "null", '{"not":"ranked_ids"}', '["raid:building:base-wall:stone"]'])
def test_corrupt_query_cache_does_not_disable_valid_evidence(knowledge, payload):
    knowledge.retrieve("为什么石墙向上取整")
    with knowledge.registry.store.connection() as connection:
        connection.execute("UPDATE knowledge_queries SET payload=?", (payload,))
    result = knowledge.retrieve("为什么石墙向上取整")
    assert result["status"] == "supported_snapshot"
    assert not result["cache_hit"]


def test_empty_index_after_restart_is_an_operational_failure(knowledge):
    with knowledge.registry.store.connection() as connection:
        connection.execute("DROP TABLE knowledge_fts")
    restarted = LocalKnowledge(knowledge.registry.store)
    assert restarted.retrieve("为什么石墙向上取整")["status"] == "index_unavailable"
    reference = restarted.reference()
    repaired = restarted.registry.rebuild_index()
    assert repaired["indexed_documents"] == 84
    assert restarted.reference() == reference
    assert restarted.retrieve("为什么石墙向上取整")["status"] == "supported_snapshot"


def test_grader_does_not_trust_self_reported_budget_or_unrelated_evidence():
    from evals.knowledge import grade

    case = {"expected_status": "supported_snapshot", "required_ids": ["recipe:explosive.timed"], "max_chars": 100}
    result = {"status": "supported_snapshot", "documents": [
        {"id": "recipe:explosive.timed", "text": "x" * 200}, {"id": "unrelated", "text": "noise"}],
        "context_chars": 1, "model_calls": 0, "network_calls": 0}
    assert not grade(case, result, {"recipe:explosive.timed", "unrelated"})["passed"]


def test_rounding_explanation_checks_false_premise_with_current_rules(knowledge):
    previous = {"operation": "raid", "target": "building:base-wall:stone", "hp": 500}
    result = knowledge.retrieve("为什么250血还需要两颗C4", previous)
    assert result["status"] == "supported_snapshot"
    assert result["derivation"]["input"]["hp"] == 250
    assert result["derivation"]["result"]["options"][0]["count"] == 1
    assert "ceil(250 / 275) × 1 = 1" in knowledge.render(result)


@pytest.mark.parametrize("health", ["-250血", "250.5血", "三百血", "250血或300血"])
def test_reasoning_does_not_replace_ambiguous_health_with_previous_value(knowledge, health):
    result = knowledge.retrieve("为什么" + health + "还需要一颗C4", {"operation": "raid",
        "target": "building:base-wall:stone", "hp": 500})
    assert result["status"] == "missing_evidence"
    assert not result.get("derivation")


def test_reasoning_uses_explicit_new_target_count(knowledge):
    result = knowledge.retrieve("为什么3面石墙每面250血还需要一颗C4", {"operation": "raid",
        "target": "building:base-wall:stone", "hp": 500, "count": 1})
    assert result["derivation"]["result"]["options"][0]["count"] == 3


def test_technology_conditions_do_not_retrieve_damage_conditions(knowledge):
    result = knowledge.retrieve("C4需要几级工作台，解释蓝图条件")
    assert {document["id"] for document in result["documents"]} == {"recipe:explosive.timed", "explain:technology"}


def test_every_registered_recipe_name_is_exactly_retrievable(knowledge):
    for identifier in knowledge.catalog.recipes:
        result = knowledge.retrieve(knowledge.catalog.items[identifier]["name"] + "配方是什么")
        assert result["status"] == "supported_snapshot", identifier
        assert [document["id"] for document in result["documents"]] == ["recipe:" + identifier], identifier


async def test_rounding_reasoning_flows_through_chat_and_revocation(tmp_path):
    store = ConversationStore(tmp_path / "reason.sqlite3")
    runtime = ChatRuntime(Settings(_env_file=None, database_path=store.path), NoModel(), store)
    owner = store.create().id
    await runtime.chat(owner, ChatRequest(request_id=uuid4(), message=
        '/calc {"operation":"raid","target":"building:base-wall:stone","hp":500,"methods":["explosive.timed"]}'))
    result = await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="为什么250血还需要两颗C4"))
    assert "ceil(250 / 275) × 1 = 1" in result.assistant
    assert result.presentation["architecture"]["decision_result"]["supported_claims"][-1]["calculation_id"]
    runtime.knowledge.registry.revoke(runtime.knowledge.reference()["release_id"], "recipe:explosives", "withdrawn dependency")
    blocked = await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="为什么250血还需要两颗C4"))
    assert blocked.presentation["knowledge"]["status"] == "missing_evidence"
    assert blocked.presentation["architecture"]["decision_result"]["supported_claims"] == []
    assert "ceil(250 / 275)" not in blocked.assistant


def test_index_rebuild_preserves_revocations_and_cli_reports_count(knowledge, capsys):
    from rust_coach.knowledge.manage import main

    knowledge.registry.revoke(knowledge.reference()["release_id"], "explain:rounding", "withdrawn")
    with knowledge.registry.store.connection() as connection:
        connection.execute("DELETE FROM knowledge_fts")
    assert main(["--database", str(knowledge.registry.store.path), "rebuild-index"]) == 0
    assert '"indexed_documents": 84' in capsys.readouterr().out
    assert knowledge.retrieve("为什么石墙向上取整")["status"] == "missing_evidence"


def test_grader_detects_mutated_content_and_eligibility_implementation(knowledge, monkeypatch):
    from evals.knowledge import fixture_oracle, grade

    case = {"expected_status": "missing_evidence", "required_ids": [],
            "fault": {"document_id": "explain:rounding", "revoke": True}}
    corpus, eligible = fixture_oracle(case)
    monkeypatch.setattr(knowledge, "eligible", lambda snapshot: (snapshot["release"]["documents"], []))
    result = knowledge.retrieve("为什么石墙向上取整")
    assert not grade(case, result, eligible, corpus)["checks"]["eligible_evidence"]
    positive = {"expected_status": "supported_snapshot", "required_ids": ["recipe:explosive.timed"]}
    result = knowledge.retrieve("C4配方是什么")
    result["documents"][0]["text"] = "C4工作台等级0"
    result["context_chars"] = len(result["documents"][0]["text"])
    assert not grade(positive, result, eligible, corpus)["checks"]["canonical_content"]


def test_revocation_uses_canonical_package_not_mutable_unit_projection(knowledge):
    knowledge.registry.revoke(knowledge.reference()["release_id"], "recipe:explosive.timed", "withdrawn")
    with knowledge.registry.store.connection() as connection:
        connection.execute("UPDATE knowledge_units SET payload='{}' WHERE id='recipe:explosive.timed'")
    package = default_release()
    package["id"] = "canonical-revocation"
    publish(knowledge, package)
    assert knowledge.retrieve("C4配方是什么")["status"] == "missing_evidence"


def test_unreadable_release_is_explicit_integrity_failure(knowledge):
    with knowledge.registry.store.connection() as connection:
        connection.execute("UPDATE knowledge_releases SET payload='{' ")
    with pytest.raises(AppError) as error:
        knowledge.reference()
    assert error.value.code == "knowledge_integrity_error"


def test_audit_dataset_and_repeated_cache_queries():
    from evals.knowledge import evaluate

    report = evaluate(Path(__file__).resolve().parents[1] / "evals/knowledge-audit-cases.json")
    assert report["summary"]["B2_exact_fts"]["passed"] == report["case_count"] == 18
    benchmark = report["cache_benchmark"]
    assert benchmark["summary"]["cold"]["cache_hits"] == 0
    assert benchmark["summary"]["warm"]["cache_hits"] == 20
    assert all(sample["status"] == "supported_snapshot" and set(sample["selected"]) == {
        "recipe:explosive.timed", "explain:technology"}
        for samples in benchmark["samples"].values() for sample in samples)


async def test_expired_web_evidence_cannot_replay_or_enter_history(tmp_path):
    class RecordingModel:
        def __init__(self):
            self.messages = []

        async def complete(self, messages):
            self.messages = messages
            return ModelReply(content="请重新核对条件。")

    store = ConversationStore(tmp_path / "web-age.sqlite3")
    model = RecordingModel()
    runtime = ChatRuntime(Settings(_env_file=None, database_path=store.path), model, store)
    owner = store.create().id
    request = ChatRequest(request_id=uuid4(), message="之前的建议")
    document = {"id": "old-web", "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()}
    store.append(owner, str(request.request_id), request.message, "过期攻略的结论", False,
        advice={"evidence": {"documents": [document]}, "answer": {"reason": "过期攻略的结论"}},
        presentation={"knowledge_context": runtime.knowledge.reference()})
    with pytest.raises(AppError) as error:
        await runtime.chat(owner, request)
    assert error.value.code == "knowledge_evidence_expired"
    await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="你好"))
    assert not any("过期攻略" in str(message.content) for message in model.messages)


async def test_web_evidence_freshness_propagates_to_followup_answers(tmp_path):
    import json

    class Model:
        async def complete(self, messages):
            return ModelReply(content="这份建议仍需核对适用版本。")

    store = ConversationStore(tmp_path / "web-followup.sqlite3")
    runtime = ChatRuntime(Settings(_env_file=None, database_path=store.path), Model(), store)
    owner = store.create().id
    document = {"id": "current-web", "fetched_at": datetime.now(timezone.utc).isoformat()}
    store.append(owner, str(uuid4()), "解释旧建议", "历史建议", False,
        advice={"evidence": {"documents": [document]}}, presentation={"knowledge_context": runtime.knowledge.reference()})
    request = ChatRequest(request_id=uuid4(), message="你好")
    turn = await runtime.chat(owner, request)
    assert turn.presentation["web_evidence_context"] == [document]
    turn.presentation["web_evidence_context"][0]["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    with store.connection() as connection:
        connection.execute("UPDATE turns SET presentation=? WHERE request_id=?", (json.dumps(turn.presentation), str(request.request_id)))
    with pytest.raises(AppError) as error:
        await runtime.chat(owner, request)
    assert error.value.code == "knowledge_evidence_expired"


async def test_expired_previous_advice_is_not_passed_to_planner(tmp_path):
    import json
    from rust_coach.advice.service import AdviceService
    from test_advice import FakeResearch, PLAN, ScriptedModel, answer

    store = ConversationStore(tmp_path / "web-plan.sqlite3")
    model = ScriptedModel([PLAN, answer()])
    runtime = ChatRuntime(Settings(_env_file=None, database_path=store.path), model, store,
                          advice_service=AdviceService(model, FakeResearch()))
    owner = store.create().id
    document = {"id": "expired", "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()}
    store.append(owner, str(uuid4()), "先前问题", "过期建议", False,
        advice={"evidence": {"documents": [document]}, "request": "先前问题", "answer": {"reason": "过期建议"}},
        presentation={"knowledge_context": runtime.knowledge.reference()})
    await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="/coach 比较下一步行动"))
    assert len(model.calls) == 2
    assert all(json.loads(messages[-1].content)["previous_answer_for_reference_only"] is None for messages in model.calls)


async def test_web_evidence_expiring_during_generation_prevents_save(tmp_path, monkeypatch):
    import rust_coach.runtime as runtime_module
    from rust_coach.knowledge.store import current_web_references

    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(runtime_module, "current_web_references",
                        lambda refs, ttl: current_web_references(refs, ttl, now=clock[0]))

    class Model:
        async def complete(self, messages):
            clock[0] += timedelta(hours=2)
            return ModelReply(content="仍需核对适用条件。")

    store = ConversationStore(tmp_path / "web-expiry.sqlite3")
    runtime = ChatRuntime(Settings(_env_file=None, database_path=store.path), Model(), store)
    owner = store.create().id
    document = {"id": "boundary", "fetched_at": (clock[0] - timedelta(hours=5)).isoformat()}
    store.append(owner, str(uuid4()), "旧建议", "待核对的建议", False,
        advice={"evidence": {"documents": [document]}}, presentation={"knowledge_context": runtime.knowledge.reference()})
    with pytest.raises(AppError) as error:
        await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="你好"))
    assert error.value.code == "knowledge_evidence_expired"
    assert len(store.get(owner).turns) == 1

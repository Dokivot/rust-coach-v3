import asyncio
import json
import sqlite3
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelMessage, ModelReply
from rust_coach.knowledge.store import EvidenceStore
from rust_coach.knowledge.web import WebResearch
from rust_coach.llm import CompatibleChatModel
from rust_coach.performance import PerformanceStore, metered_model, normalize_usage, span
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime
from test_vision import VisionModel, png


class Model:
    def __init__(self):
        self.calls = 0
        self.failure = False

    async def complete(self, messages):
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.failure:
            raise AppError("model_timeout", "private prompt must not be logged", 504)
        return ModelReply(content="你好，先说明你这次的目标。", usage={"prompt_tokens": 10, "completion_tokens": 3,
            "total_tokens": 13, "prompt_tokens_details": {"cached_tokens": 4}})


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, api_key="fixture-secret", search_provider="off", database_path=tmp_path / "performance.sqlite3")


def send(client, owner, message, request_id=None, **fields):
    request_id = request_id or str(uuid4())
    response = client.post(f"/api/conversations/{owner}/messages", json={"request_id": request_id, "message": message, **fields})
    report = client.get(f"/api/conversations/{owner}/performance", params={"request_id": request_id}).json()
    return response, report


@pytest.mark.parametrize("raw,status,expected", [
    (None, "missing", None), ({}, "missing", None),
    ({"total_tokens": 12}, "partial", None),
    ({"input_tokens": 10, "output_tokens": 3}, "complete", 10),
    ({"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}, "complete", 10),
    ({"input_tokens": True, "output_tokens": 3}, "invalid", None),
    ({"input_tokens": [], "output_tokens": 3}, "invalid", None),
    ({"input_tokens": 10, "prompt_tokens": 11, "output_tokens": 3}, "invalid", None),
    ({"input_tokens": 10, "output_tokens": 3, "total_tokens": 14}, "invalid", None),
    ({"input_tokens": 10, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 11}}, "invalid", None),
    ({"input_tokens": 10, "output_tokens": 3, "output_tokens_details": []}, "invalid", None),
])
def test_usage_is_not_estimated_or_double_counted(raw, status, expected):
    result = normalize_usage(raw)
    assert result["status"] == status
    assert result["input_tokens"] == expected


def test_real_chat_latency_replay_failure_restart_and_delete(settings):
    model = Model()
    with TestClient(create_app(settings, model)) as client:
        owner = client.post("/api/conversations", json={}).json()["id"]
        request_id = str(uuid4())
        response, report = send(client, owner, "你好", request_id)
        assert response.status_code == 200
        run = report["runs"][0]
        call = next(item for item in run["spans"] if item["kind"] == "model")
        stage = next(item for item in run["spans"] if item["name"] == "generation.chat")
        assert run["duration_ms"] >= stage["duration_ms"] >= call["duration_ms"] >= 8
        assert call["parent_span_id"] == stage["id"]
        assert call["usage"]["cached_input_tokens"] == 4
        assert run["summary"]["model_calls"] == 1
        assert run["summary"]["tokens"]["input_tokens"]["known_sum"] == 10
        assert run["summary"]["billed_cost"] is None
        assert run["outcome"]["route"] == "chat"
        assert report["shared_request_budget"] is False
        replay, report = send(client, owner, "你好", request_id)
        assert replay.json() == response.json()
        assert report["runs"][-1]["outcome"]["replayed"]
        assert report["runs"][-1]["summary"]["model_calls"] == 0
        assert report["summary"]["model_calls"] == 1
        model.failure = True
        failed, report = send(client, owner, "private prompt must not be logged")
        assert failed.status_code == 504
        assert report["runs"][0]["status"] == "failed"
        assert report["summary"]["failed_calls"] == 1
        assert "private prompt" not in json.dumps(report)
        assert "fixture-secret" not in json.dumps(report)
        assert len(client.get(f"/api/conversations/{owner}").json()["turns"]) == 1
    with TestClient(create_app(settings, model)) as client:
        assert client.get(f"/api/conversations/{owner}/performance").json()["total_runs"] == 3
        assert client.delete(f"/api/conversations/{owner}").status_code == 204
        assert client.get("/api/performance").json()["total_runs"] == 0


def test_screenshot_to_raid_followup_records_reuse_without_new_vision(settings):
    model = VisionModel()
    model.task = "craft"
    application = create_app(settings, model)
    with TestClient(application) as client:
        vision = application.state.vision
        vision.drafts.metadata("probe:" + vision.config_id, {"status": "passed"})
        owner = client.post("/api/conversations", json={}).json()["id"]
        image = client.post(f"/api/conversations/{owner}/attachments", files={"file": ("fixture.png", png(), "image/png")}).json()
        crafted, report = send(client, owner, "制作十份火药", attachment_ids=[image["id"]])
        assert crafted.json()["calculation"]["status"] == "calculated"
        assert report["summary"]["model_calls"] == 1
        assert report["summary"]["usage_complete_calls"] == 0
        assert report["summary"]["tokens"]["total_tokens"]["known_sum"] == 12
        switched, report = send(client, owner, "改成拆一面石墙最省资源")
        assert switched.json()["calculation"]["input"]["operation"] == "raid"
        assert report["summary"]["model_calls"] == 0
        final, report = send(client, owner, "它还剩250血")
        assert final.json()["calculation"]["input"]["hp"] == 250
        assert report["summary"]["model_calls"] == 0
        assert any(item["name"] == "visual_draft_reused" for item in report["runs"][0]["observations"])
        assert len(model.calls) == 1


def test_local_paths_and_schema_repair_are_counted_separately(settings):
    model = VisionModel()
    model.task, model.invalid = "craft", 1
    application = create_app(settings, model)
    with TestClient(application) as client:
        application.state.vision.drafts.metadata("probe:" + application.state.vision.config_id, {"status": "passed"})
        owner = client.post("/api/conversations", json={}).json()["id"]
        for message in ['/calc {"operation":"raid","target":"building:base-wall:stone"}', '/knowledge C4配方是什么']:
            response, report = send(client, owner, message)
            assert response.status_code == 200
            assert report["summary"]["model_calls"] == 0
            assert report["summary"]["cost_status"] == "no_external_calls"
        image = client.post(f"/api/conversations/{owner}/attachments", files={"file": ("fixture.png", png(), "image/png")}).json()
        response, report = send(client, owner, "制作十份火药", attachment_ids=[image["id"]])
        assert response.status_code == 200
        assert report["summary"]["model_calls"] == 2
        assert any(item["name"] == "vision_schema_rejected" for item in report["runs"][0]["observations"])


async def test_cancelled_request_and_busy_are_saved_without_turn(settings):
    entered = asyncio.Event()

    class Blocking:
        async def complete(self, messages):
            entered.set()
            await asyncio.Event().wait()

    runtime = ChatRuntime(settings, Blocking(), ConversationStore(settings.database_path))
    owner = runtime.store.create().id
    request = ChatRequest(request_id=uuid4(), message="你好")
    running = asyncio.create_task(runtime.chat(owner, request))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(AppError, match="已有对话"):
        await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="你好"))
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    report = runtime.performance.inspect(conversation_id=owner)
    assert report["summary"]["cancelled_calls"] == 1
    assert {run["status"] for run in report["runs"]} == {"cancelled", "failed"}
    assert not runtime.lock.locked()
    assert runtime.store.get(owner).turns == []


async def test_background_memory_has_own_run_and_original_request(settings):
    class Memory:
        async def complete(self, messages):
            return ModelReply(content='{"candidates":[]}', usage={"input_tokens": 5, "output_tokens": 2})

    runtime = ChatRuntime(settings, Model(), ConversationStore(settings.database_path), memory_model=Memory())
    owner = runtime.store.create().id
    request = ChatRequest(request_id=uuid4(), message="昨晚我的基地被偷家了")
    await runtime.chat(owner, request)
    await runtime.memory.task
    report = runtime.performance.inspect(conversation_id=owner, request_id=str(request.request_id))
    front = next(run for run in report["runs"] if run["kind"] == "chat")
    back = next(run for run in report["runs"] if run["kind"] == "background_memory")
    assert back["parent_run_id"] == front["id"]
    assert front["summary"]["model_calls"] == back["summary"]["model_calls"] == 1
    assert report["background_memory_status"] == "complete"


async def test_provider_usage_on_invalid_response_and_web_failure(settings):
    store = ConversationStore(settings.database_path)
    performance = PerformanceStore(store)

    def handler(request):
        if request.url.path.endswith("responses"):
            return httpx.Response(200, json={"status": "completed", "usage": {"input_tokens": 10, "output_tokens": 2}, "output": []})
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        model = metered_model(CompatibleChatModel(settings, client), performance, "chat", settings)
        with performance.run("fixture"):
            with pytest.raises(AppError):
                await model.complete([ModelMessage(role="user", content="private")])
            research = WebResearch(settings, client, EvidenceStore(store))
            with pytest.raises(Exception):
                await research._read("https://wiki.facepunch.com/rust/private?secret=hidden")
    report = performance.inspect()
    assert report["summary"]["dispatched_model_requests"] == 1
    assert report["summary"]["usage_complete_calls"] == 1
    assert report["summary"]["failed_calls"] == 2
    assert report["summary"]["web_requests"] == 1
    assert "private" not in json.dumps(report) and "hidden" not in json.dumps(report)


async def test_parallel_spans_share_parent_but_not_each_other(settings):
    performance = PerformanceStore(ConversationStore(settings.database_path))

    async def work(name):
        with span(name):
            await asyncio.sleep(0.005)

    with performance.run("fixture"):
        with span("parent"):
            await asyncio.gather(work("first"), work("second"))
    spans = performance.inspect()["runs"][0]["spans"]
    parent = next(item for item in spans if item["name"] == "parent")
    assert all(item["parent_span_id"] == parent["id"] for item in spans if item["name"] != "parent")


def test_recording_failure_is_explicit_and_does_not_replace_business_result(settings, monkeypatch):
    application = create_app(settings, Model())
    with TestClient(application) as client:
        owner = client.post("/api/conversations", json={}).json()["id"]
        performance = application.state.runtime.performance
        original = performance.write_span

        def fail(run_id, data):
            performance.write_failed = True
            return False

        monkeypatch.setattr(performance, "write_span", fail)
        response, report = send(client, owner, "你好")
        assert response.status_code == 200
        assert report["summary"]["recording_status"] == "incomplete"
        assert report["summary"]["cost_status"] == "unknown"
        monkeypatch.setattr(performance, "write_span", original)
    with sqlite3.connect(settings.database_path) as connection:
        payload = json.loads(connection.execute("SELECT payload FROM performance_runs").fetchone()[0])
    assert payload["recording_incomplete"]


async def test_no_new_shared_budget_and_missing_usage_remains_unknown(settings):
    class NoUsage:
        async def complete(self, messages):
            return ModelReply(content="fixture")

    performance = PerformanceStore(ConversationStore(settings.database_path))
    model = metered_model(NoUsage(), performance, "fixture", settings)
    with performance.run("fixture"):
        for attempt in range(8):
            await model.complete([ModelMessage(role="user", content=str(attempt))])
    report = performance.inspect()
    assert report["summary"]["model_calls"] == 8
    assert report["summary"]["usage_complete_calls"] == 0
    assert report["summary"]["tokens"]["total_tokens"]["known_sum"] is None
    assert report["summary"]["billed_cost"] is None


def test_knowledge_revocation_blocks_replay_and_records_failure(settings):
    application = create_app(settings, Model())
    with TestClient(application) as client:
        owner = client.post("/api/conversations", json={}).json()["id"]
        request_id = str(uuid4())
        message = '/calc {"operation":"raid","target":"building:base-wall:stone"}'
        response, report = send(client, owner, message, request_id)
        assert response.status_code == 200
        knowledge = application.state.runtime.knowledge
        knowledge.registry.revoke(knowledge.reference()["release_id"], "raid:building:base-wall:stone", "fixture")
        response, report = send(client, owner, message, request_id)
        assert response.status_code == 409
        assert report["runs"][-1]["status"] == "failed"
        assert report["runs"][-1]["error_code"] == "knowledge_changed"
        assert report["summary"]["model_calls"] == 0


async def test_actual_database_recording_error_is_reported(settings, monkeypatch, caplog):
    performance = PerformanceStore(ConversationStore(settings.database_path))
    original = performance.conversations.connection

    def unavailable():
        raise sqlite3.OperationalError("private database details")

    monkeypatch.setattr(performance.conversations, "connection", unavailable)
    with performance.run("fixture"):
        with span("fixture"):
            pass
    monkeypatch.setattr(performance.conversations, "connection", original)
    assert performance.inspect()["summary"]["recording_status"] == "incomplete"
    assert "performance_recording_failed" in caplog.text
    assert "private database details" not in caplog.text


def test_synthetic_end_to_end_evaluation():
    from evals.performance import evaluate

    report = evaluate(1)
    assert report["passed"] == report["total"] == 11
    assert report["mode"] == "synthetic_model_real_application_http_sse"
    assert report["summary"]["image_cold"]["model_calls"] == [1]
    assert report["summary"]["image_warm"]["model_calls"] == [0]
    assert report["summary"]["image_repair"]["model_calls"] == [2]
    assert all(group["p95_ms"] is None for group in report["summary"].values())


def test_legacy_events_do_not_claim_business_duration():
    from rust_coach.architecture.observability import emit_trace, new_trace

    trace = {"context": new_trace(uuid4(), uuid4()).model_dump(mode="json"), "events": []}
    event = emit_trace(trace, layer="input", component="ingress", event="received")["events"][0]
    assert event["duration_ms"] is None and event["ended_at"] is None


def test_running_records_and_limited_listing_are_not_complete_history(settings):
    performance = PerformanceStore(ConversationStore(settings.database_path))
    with performance.run("fixture"):
        with span("external", "model", usage=normalize_usage(None)):
            report = performance.inspect()
            assert report["runs"][0]["status"] == "running"
            assert report["summary"]["unfinished_calls"] == 1
    with performance.run("fixture"):
        pass
    report = performance.inspect(limit=1)
    assert report["total_runs"] == 2 and report["truncated"]
    assert len(report["runs"]) == 1


async def test_advice_nested_calls_are_not_one_outer_tool(settings):
    import copy
    from rust_coach.advice.service import AdviceService
    from test_advice import FakeResearch, PLAN, ScriptedModel, answer

    plan = copy.deepcopy(PLAN)
    plan["topics"] = []
    plan["candidates"] = [plan["candidates"][0], {"id": "craft", "title": "准备火药", "kind": "craft",
        "required_fields": [], "goals": {"gunpowder": 10}}]
    model = ScriptedModel(["invalid JSON", plan, answer()])
    runtime = ChatRuntime(settings, model, ConversationStore(settings.database_path), advice_service=AdviceService(model, FakeResearch()))
    owner = runtime.store.create().id
    turn = await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="/coach 比较下一步行动"))
    assert turn.advice["status"] == "conditional"
    report = runtime.performance.inspect(conversation_id=owner)
    assert report["summary"]["model_calls"] == len(model.calls) == 3
    assert any(item["name"] == "advice_validation_rejected" for item in report["runs"][0]["observations"])


def test_probe_and_player_state_preview_are_separate_runs(settings, monkeypatch):
    from rust_coach.player_state.schemas import ScopeCreate

    numbers = iter([26, 42])
    monkeypatch.setattr("rust_coach.vision.probe.secrets.randbelow", lambda upper: next(numbers))

    class ProbeModel:
        async def complete(self, messages):
            return ModelReply(content='{"codes":["100026","100042"]}', usage={"total_tokens": 5})

    application = create_app(settings, Model(), vision_model=ProbeModel())
    with TestClient(application) as client:
        response = client.post("/api/vision/probe", json={})
        assert response.status_code == 200
        scope = application.state.runtime.player_states.create(ScopeCreate(label="test", server_id="fixture", wipe_id="fixture"))
        response = client.post(f"/api/player-state/scopes/{scope['scope']['id']}/preview", json={"message": "我有一级工作台"})
        assert response.status_code == 422
        report = client.get("/api/performance").json()
        assert {run["kind"] for run in report["runs"]} == {"vision_probe", "player_state_preview"}
        assert all(run["summary"]["model_calls"] == 1 for run in report["runs"])
        assert client.get("/performance").status_code == 200
        assert client.post("/api/calculations", json={"operation": "raid", "target": "building:base-wall:stone"}).status_code == 200
        calculation = client.get("/api/performance").json()["runs"][-1]
        assert calculation["kind"] == "local_calculation" and calculation["summary"]["model_calls"] == 0

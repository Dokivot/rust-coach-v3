import asyncio
import json
import sqlite3
import threading
from time import perf_counter
from uuid import uuid4

import httpx
import pytest

from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelMessage, ModelReply
from rust_coach.llm import CompatibleChatModel
from rust_coach.knowledge.store import EvidenceStore
from rust_coach.knowledge.web import RetrievalError, WebResearch
from rust_coach.performance import PerformanceStore, metered_model, provider_usage, span
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, api_key="fixture", search_provider="off", database_path=tmp_path / "audit.sqlite3")


def test_recording_does_not_wait_for_business_write_lock(settings):
    store = ConversationStore(settings.database_path)
    performance = PerformanceStore(store)
    lock = sqlite3.connect(settings.database_path, check_same_thread=False)
    lock.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.4, lock.rollback)
    release.start()
    try:
        started = perf_counter()
        with performance.run("locked"):
            with span("work"):
                result = "business completed"
        elapsed = perf_counter() - started
    finally:
        release.join()
        lock.close()
    assert result == "business completed"
    assert elapsed < 0.2
    assert performance.inspect()["summary"]["recording_status"] == "incomplete"


def test_conversation_lookup_failure_does_not_replace_business(settings, monkeypatch):
    store = ConversationStore(settings.database_path)
    owner = store.create().id
    performance = PerformanceStore(store)
    original = store.connection

    def fail_once():
        monkeypatch.setattr(store, "connection", original)
        raise sqlite3.OperationalError("private path")

    monkeypatch.setattr(store, "connection", fail_once)
    with performance.run("fixture", conversation_id=owner):
        result = "business completed"
    assert result == "business completed"
    assert performance.inspect()["summary"]["recording_status"] == "incomplete"


def test_failed_final_write_remains_incomplete_after_restart(settings, monkeypatch):
    store = ConversationStore(settings.database_path)
    performance = PerformanceStore(store)
    original = performance.write_run

    def drop_final(data):
        return False if data["status"] != "running" else original(data)

    monkeypatch.setattr(performance, "write_run", drop_final)
    with performance.run("fixture"):
        pass
    report = PerformanceStore(store).inspect()
    assert report["runs"][0]["status"] == "running"
    assert report["summary"]["recording_status"] == "incomplete"
    assert report["summary"]["cost_status"] == "unknown"


def test_missing_persisted_span_is_not_complete_accounting(settings):
    store = ConversationStore(settings.database_path)
    performance = PerformanceStore(store)
    with performance.run("fixture"):
        with span("model", "model"):
            pass
    with store.connection() as connection:
        connection.execute("DELETE FROM performance_spans")
    report = PerformanceStore(store).inspect()
    assert report["summary"]["recording_status"] == "incomplete"
    assert report["summary"]["cost_status"] == "unknown"


@pytest.mark.parametrize("failure", ["disconnect", "malformed", "cancel"])
async def test_received_stream_usage_survives_failure(settings, failure):
    usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    event = {"type": "response.completed", "response": {"status": "completed", "usage": usage}}

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            frame = ("data: " + json.dumps(event) + "\r\n\r\n").encode()
            for offset in range(0, len(frame), 7):
                yield frame[offset:offset + 7]
            if failure == "disconnect":
                raise httpx.ReadError("private transport details")
            if failure == "cancel":
                raise asyncio.CancelledError()
            yield b"data: invalid\n\n"

    performance = PerformanceStore(ConversationStore(settings.database_path))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))) as client:
        model = metered_model(CompatibleChatModel(settings, client, stream_responses=True), performance, "chat", settings)
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else AppError):
            await model.complete([ModelMessage(role="user", content="hello")])
    report = performance.inspect()
    call = report["runs"][0]["spans"][0]
    assert call["status"] == ("cancelled" if failure == "cancel" else "failed")
    assert call["usage"]["total_tokens"] == 12
    assert call["http_status"] == 200
    assert report["summary"]["tokens"]["total_tokens"]["known_sum"] == 12
    assert report["summary"]["billed_cost"] is None
    assert "private transport" not in json.dumps(report)


async def test_background_commit_failure_is_not_completed_run(settings, monkeypatch):
    class Model:
        async def complete(self, messages):
            return ModelReply(content='{"candidates":[]}')

    runtime = ChatRuntime(settings, Model(), ConversationStore(settings.database_path), memory_model=Model())
    original = runtime.memory.finish_job

    def fail_success(sequence, metadata, candidates, status):
        if status == "complete":
            raise sqlite3.OperationalError("commit failed")
        return original(sequence, metadata, candidates, status)

    monkeypatch.setattr(runtime.memory, "finish_job", fail_success)
    owner = runtime.store.create().id
    request = ChatRequest(request_id=uuid4(), message="昨晚我的基地被偷家了")
    await runtime.chat(owner, request)
    await runtime.memory.task
    report = runtime.performance.inspect(conversation_id=owner, request_id=str(request.request_id))
    background = next(run for run in report["runs"] if run["kind"] == "background_memory")
    assert report["background_memory_status"] == "failed"
    assert background["status"] == "failed"
    assert background["error_code"] == "OperationalError"


def test_nested_runs_keep_parent_identity(settings):
    performance = PerformanceStore(ConversationStore(settings.database_path))
    with performance.run("visual_confirmation") as parent:
        with performance.run("chat") as child:
            assert child.data["parent_run_id"] == parent.data["id"]


@pytest.mark.parametrize("status", [429, 503])
async def test_failed_transport_retains_http_status_without_private_content(settings, status):
    store = ConversationStore(settings.database_path)
    performance = PerformanceStore(store)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        model = metered_model(CompatibleChatModel(settings, client), performance, "chat", settings)
        research = WebResearch(settings, client, EvidenceStore(store))
        with performance.run("fixture"):
            with pytest.raises(AppError):
                await model.complete([])
            with pytest.raises(RetrievalError):
                await research._read("https://wiki.facepunch.com/rust/private?key=secret")
    report = performance.inspect()
    assert report["summary"]["failed_calls"] == 2
    assert all(item["http_status"] == status for item in report["runs"][0]["spans"])
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("mutation", ["missing_terminal", "wrong_request", "wrong_answer"])
def test_evaluation_rejects_corrupted_replay_delivery(monkeypatch, mutation):
    from evals.performance import evaluate
    from rust_coach import app

    original = app.chat_events
    seen = set()

    async def corrupted(runtime, owner, request):
        replay = request.request_id in seen and request.message.startswith("/calc")
        seen.add(request.request_id)
        async for frame in original(runtime, owner, request):
            event = json.loads(frame[6:])
            if replay and event["type"] == "result":
                if mutation == "missing_terminal":
                    continue
                if mutation == "wrong_request":
                    event["request_id"] = str(uuid4())
                else:
                    event["turn"]["assistant"] = "incorrect answer"
                frame = "data: " + json.dumps(event) + "\n\n"
            yield frame

    monkeypatch.setattr(app, "chat_events", corrupted)
    report = evaluate(1)
    replay = next(sample for sample in report["samples"] if sample["scenario"] == "request_replay")
    assert not replay["passed"]
    assert report["passed"] < report["total"]


def test_evaluation_checks_usage_against_independent_fixture_ledger(monkeypatch):
    from evals.performance import FixtureModel, evaluate

    original = FixtureModel.complete

    async def incorrect_usage(self, messages):
        reply = await original(self, messages)
        return reply.model_copy(update={"usage": {"input_tokens": 9999, "output_tokens": 1}})

    monkeypatch.setattr(FixtureModel, "complete", incorrect_usage)
    report = evaluate(1)
    cold = next(sample for sample in report["samples"] if sample["scenario"] == "image_cold")
    assert not cold["checks"]["input_tokens"]
    assert not cold["passed"]


@pytest.mark.parametrize("problem", ["empty", "stage_only", "duplicate", "unsaved"])
def test_delivery_grader_requires_one_persisted_terminal(problem):
    from evals.performance import delivery_checks

    identifier = str(uuid4())
    turn = {"request_id": identifier, "assistant": "answer"}
    terminal = {"type": "result", "request_id": identifier, "turn": turn}
    events = [terminal]
    saved = [turn]
    if problem == "empty":
        events = []
    elif problem == "stage_only":
        events = [{"type": "stage", "request_id": identifier}]
    elif problem == "duplicate":
        events = [terminal, terminal]
    else:
        saved = []
    assert not all(delivery_checks(events, identifier, 200, [], saved).values())


async def test_later_missing_usage_does_not_erase_received_counts(settings):
    class Model:
        async def complete(self, messages):
            provider_usage({"input_tokens": 10, "output_tokens": 2})
            provider_usage(None)
            return ModelReply(content="answer")

    performance = PerformanceStore(ConversationStore(settings.database_path))
    model = metered_model(Model(), performance, "fixture", settings)
    await model.complete([])
    report = performance.inspect()
    assert report["summary"]["tokens"]["input_tokens"]["known_sum"] == 10


async def test_existing_timeout_cancels_call_without_saving_false_success(settings):
    class Blocking:
        async def complete(self, messages):
            await asyncio.Event().wait()

    settings = settings.model_copy(update={"request_timeout_seconds": 0.02})
    runtime = ChatRuntime(settings, Blocking(), ConversationStore(settings.database_path))
    owner = runtime.store.create().id
    with pytest.raises(AppError) as caught:
        await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="你好"))
    assert caught.value.status == 504
    assert runtime.store.get(owner).turns == []
    report = runtime.performance.inspect(conversation_id=owner)
    assert report["runs"][0]["status"] == "failed"
    assert report["summary"]["cancelled_calls"] == 1
    assert report["summary"]["cost_status"] == "unknown"
    assert not runtime.lock.locked()

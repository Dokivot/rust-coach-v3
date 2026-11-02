import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.contracts import ChatRequest, ModelReply
from rust_coach.persistence import ConversationStore
from rust_coach.progress import STAGE_LABELS, chat_events, report_stage
from rust_coach.runtime import ChatRuntime


class Model:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        return ModelReply(content="你好，先说说这次的目标。")


class BlockingModel(Model):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def complete(self, messages):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return await super().complete(messages)


def events_from_text(text):
    return [json.loads(frame[6:]) for frame in text.split("\n\n") if frame.startswith("data: ")]


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, api_key="test", search_provider="off", database_path=tmp_path / "progress.sqlite3")


@pytest.mark.parametrize("message,expected,absent", [
    ("你好", "compose", "calculate_craft"),
    ('/calc {"operation":"craft","goals":{"gunpowder":10}}', "calculate_craft", "calculate_raid"),
    ('/calc {"operation":"raid","target":"building:base-wall:stone"}', "calculate_raid", "calculate_craft"),
    ("/state", "player_state", "calculate_craft"),
])
def test_http_stream_stages_match_route_and_result_is_persisted(settings, message, expected, absent):
    model = Model()
    with TestClient(create_app(settings, model)) as client:
        owner = client.post("/api/conversations", json={}).json()["id"]
        payload = {"request_id": str(uuid4()), "message": message}
        path = f"/api/conversations/{owner}/messages"
        response = client.post(path, json=payload, headers={"Accept": "text/event-stream"})
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        events = events_from_text(response.text)
        progress = [event for event in events if event["type"] == "stage"]
        stages = [event["stage"] for event in progress]
        assert stages[0] == "context" and stages[-1] == "save"
        assert expected in stages and absent not in stages
        assert "recognize_image" not in stages and "retrieve" not in stages
        assert [event["sequence"] for event in progress] == list(range(1, len(progress) + 1))
        assert all(event["request_id"] == payload["request_id"] for event in events)
        assert all(event["label"] == STAGE_LABELS[event["stage"]] for event in progress)
        assert events[-1]["type"] == "result"
        turn = events[-1]["turn"]
        assert client.get(f"/api/conversations/{owner}").json()["turns"] == [turn]
        assert client.post(path, json=payload).json() == turn
        before = model.calls
        replay = events_from_text(client.post(path, json=payload, headers={"Accept": "text/event-stream"}).text)
        assert [event["stage"] for event in replay if event["type"] == "stage"] == ["replay"]
        assert replay[-1]["turn"] == turn and model.calls == before


def test_stream_errors_and_input_validation_keep_existing_guards(settings):
    with TestClient(create_app(settings, Model())) as client:
        owner = client.post("/api/conversations", json={}).json()["id"]
        headers = {"Accept": "text/event-stream"}
        path = f"/api/conversations/{owner}/messages"
        assert client.post(path, json={"message": ""}, headers=headers).status_code == 422
        payload = {"request_id": str(uuid4()), "message": "你好"}
        assert client.post(path, json=payload, headers={**headers, "Origin": "https://other.example"}).status_code == 403
        missing = client.post(f"/api/conversations/{uuid4()}/messages", json=payload, headers=headers)
        events = events_from_text(missing.text)
        assert events[-1]["type"] == "error" and events[-1]["error"]["status"] == 404
        assert client.get(f"/api/conversations/{owner}").json()["turns"] == []


async def test_progress_arrives_before_model_finishes_and_busy_requests_are_isolated(settings):
    model = BlockingModel()
    store = ConversationStore(settings.database_path)
    runtime = ChatRuntime(settings, model, store)
    owner, other = store.create().id, store.create().id
    request = ChatRequest(request_id=uuid4(), message="你好")
    composing = asyncio.Event()
    events = []

    async def consume():
        async for frame in chat_events(runtime, owner, request):
            for event in events_from_text(frame):
                events.append(event)
                if event.get("stage") == "compose":
                    composing.set()

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        await asyncio.wait_for(composing.wait(), 3)
        assert not consumer.done() and store.get(owner).turns == []
        second = ChatRequest(request_id=uuid4(), message="另一个会话")
        rejected = [event async for frame in chat_events(runtime, other, second) for event in events_from_text(frame)]
        assert len(rejected) == 1 and rejected[0]["error"]["code"] == "chat_busy"
        assert rejected[0]["request_id"] == str(second.request_id)
        model.release.set()
        await asyncio.wait_for(consumer, 3)
        assert events[-1]["type"] == "result"
        assert all(event["request_id"] == str(request.request_id) for event in events)
        assert len(store.get(owner).turns) == 1
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


async def test_disconnect_cancels_work_releases_lock_and_allows_retry(settings):
    model = BlockingModel()
    store = ConversationStore(settings.database_path)
    runtime = ChatRuntime(settings, model, store)
    owner = store.create().id
    request = ChatRequest(request_id=uuid4(), message="你好")
    stream = chat_events(runtime, owner, request)
    try:
        while True:
            frame = await asyncio.wait_for(anext(stream), 3)
            if any(event.get("stage") == "compose" for event in events_from_text(frame)):
                break
        await asyncio.wait_for(model.started.wait(), 3)
    finally:
        await stream.aclose()
    assert model.cancelled.is_set()
    assert not runtime.lock.locked() and store.get(owner).turns == []
    model.release.set()
    events = [event async for frame in chat_events(runtime, owner, request) for event in events_from_text(frame)]
    assert events[0]["sequence"] == 1
    assert events[-1]["type"] == "result"
    assert len(store.get(owner).turns) == 1


async def test_threaded_progress_is_ordered_deduplicated_and_errors_are_sanitized():
    class BrokenRuntime:
        async def chat(self, owner, request):
            report_stage("context")
            report_stage("context")
            report_stage("secret-prompt")
            await asyncio.to_thread(report_stage, "understand")
            raise RuntimeError("secret-key")

    request = ChatRequest(request_id=uuid4(), message="test")
    frames = [frame async for frame in chat_events(BrokenRuntime(), "owner", request)]
    events = events_from_text("".join(frames))
    assert [event["stage"] for event in events if event["type"] == "stage"] == ["context", "understand"]
    assert events[-1]["error"]["code"] == "internal_error"
    assert "secret" not in "".join(frames)
    report_stage("compose")


@pytest.mark.parametrize("disconnect", [False, True])
async def test_asgi_sends_progress_while_model_waits_and_handles_disconnect(settings, disconnect):
    model = BlockingModel()
    application = create_app(settings, model)
    async with application.router.lifespan_context(application):
        runtime = application.state.runtime
        owner = runtime.store.create().id
        request_id = str(uuid4())
        path = f"/api/conversations/{owner}/messages"
        body = json.dumps({"request_id": request_id, "message": "你好"}).encode()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1", "method": "POST", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"), (b"accept", b"text/event-stream")],
            "client": ("127.0.0.1", 12345), "server": ("testserver", 80)}
        body_sent = False
        closed = asyncio.Event()
        progress_sent = asyncio.Event()
        chunks = []

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await closed.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b"").decode())
                if any(event.get("stage") == "compose" for event in events_from_text("".join(chunks))):
                    progress_sent.set()

        handling = asyncio.create_task(application(scope, receive, send))
        try:
            await asyncio.wait_for(model.started.wait(), 3)
            await asyncio.wait_for(progress_sent.wait(), 3)
            assert not handling.done() and runtime.store.get(owner).turns == []
            assert all(event["type"] == "stage" for event in events_from_text("".join(chunks)))
            if disconnect:
                closed.set()
            else:
                model.release.set()
            await asyncio.wait_for(handling, 3)
            assert not runtime.lock.locked()
            if disconnect:
                assert model.cancelled.is_set() and runtime.store.get(owner).turns == []
            else:
                assert events_from_text("".join(chunks))[-1]["type"] == "result"
                assert len(runtime.store.get(owner).turns) == 1
        finally:
            handling.cancel()
            await asyncio.gather(handling, return_exceptions=True)

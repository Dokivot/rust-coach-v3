import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelReply
from rust_coach.llm import CompatibleChatModel
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime


class RecordingModel:
    def __init__(self):
        self.calls = []
        self.failure = False

    async def complete(self, messages):
        self.calls.append(messages)
        if self.failure:
            raise AppError("model_timeout", "模型响应超时", 504)
        return ModelReply(content=f"回复：{messages[-1].content}")


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, api_key="test-secret", database_path=tmp_path / "chat.sqlite3")


def send(client, identifier, message, request_id=None):
    return client.post(f"/api/conversations/{identifier}/messages", json={
        "request_id": request_id or str(uuid4()), "message": message,
    })


def test_conversation_context_replay_restart_and_delete(settings):
    model = RecordingModel()
    with TestClient(create_app(settings, model)) as client:
        identifier = client.post("/api/conversations", json={}).json()["id"]
        request_id = str(uuid4())
        first = send(client, identifier, "记住我的名字", request_id)
        assert first.status_code == 200
        assert send(client, identifier, "记住我的名字", request_id).json() == first.json()
        assert len(model.calls) == 1
        assert send(client, identifier, "另一条消息", request_id).status_code == 409
        assert send(client, identifier, "继续").status_code == 200
        assert [message.role for message in model.calls[-1]] == ["system", "user", "assistant", "user"]
        assert model.calls[-1][1].content == "记住我的名字"
    with TestClient(create_app(settings, model)) as client:
        restored = client.get(f"/api/conversations/{identifier}").json()
        assert len(restored["turns"]) == 2
        assert client.delete(f"/api/conversations/{identifier}").status_code == 204
        assert client.get(f"/api/conversations/{identifier}").status_code == 404
    with ConversationStore(settings.database_path).connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


def test_failure_not_saved_and_same_request_can_retry(settings):
    model = RecordingModel()
    model.failure = True
    with TestClient(create_app(settings, model)) as client:
        identifier = client.post("/api/conversations", json={}).json()["id"]
        request_id = str(uuid4())
        assert send(client, identifier, "你好", request_id).status_code == 504
        assert client.get(f"/api/conversations/{identifier}").json()["turns"] == []
        model.failure = False
        assert send(client, identifier, "你好", request_id).status_code == 200


@pytest.mark.parametrize("api_mode,terminal", [
    ("responses", "completed"),
    ("responses", "incomplete"),
    ("chat_completions", "completed"),
])
def test_chat_provider_transport_and_persistence(settings, monkeypatch, api_mode, terminal):
    settings.api_mode = api_mode
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if api_mode == "chat_completions":
            assert request.url.path == "/v1/chat/completions"
            assert payload["stream"] is False
            return httpx.Response(200, json={"choices": [
                {"message": {"content": "你好！"}, "finish_reason": "stop"},
            ]})
        assert request.url.path == "/v1/responses"
        assert payload["store"] is False
        if not payload["stream"]:
            return httpx.Response(200, json={"status": "completed", "output": []})
        events = [
            {"type": "response.output_item.added", "output_index": 0,
             "item": {"id": "message", "type": "message", "role": "assistant"}},
            {"type": "response.output_text.delta", "item_id": "message", "content_index": 0, "delta": "你好"},
            {"type": "response.output_text.delta", "item_id": "message", "content_index": 0, "delta": "！"},
            {"type": f"response.{terminal}", "response": {
                "status": terminal, "output": [],
                "incomplete_details": {"reason": "max_output_tokens"} if terminal == "incomplete" else None,
            }},
        ]
        body = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    async_client = httpx.AsyncClient
    monkeypatch.setattr("rust_coach.app.httpx.AsyncClient", lambda **kwargs: async_client(
        **kwargs, transport=httpx.MockTransport(handler)))
    with TestClient(create_app(settings)) as client:
        identifier = client.post("/api/conversations", json={}).json()["id"]
        request_id = str(uuid4())
        response = send(client, identifier, "你好", request_id)
        assert response.status_code == 200
        turn = response.json()
        assert turn["assistant"] == "你好！"
        assert turn["truncated"] is (terminal == "incomplete")
        assert send(client, identifier, "你好", request_id).json() == turn
        assert len(calls) == 1
        assert client.get(f"/api/conversations/{identifier}").json()["turns"] == [turn]
        assert send(client, identifier, "继续").status_code == 200
        messages = calls[-1]["input" if api_mode == "responses" else "messages"]
        assert [message["role"] for message in messages] == ["system", "user", "assistant", "user"]
        assert messages[2]["content"] == "你好！"


def test_configuration_validation_and_local_guard(settings):
    with TestClient(create_app(settings, RecordingModel())) as client:
        response = client.get("/api/config")
        assert response.json()["configured"] is True
        assert "test-secret" not in response.text
        assert response.json()["capabilities"]["calculators"] is True
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert client.post("/api/conversations", json={}, headers={"Origin": "https://other.example"}).status_code == 403
        assert client.post("/api/conversations", content="{}").status_code == 415
        identifier = client.post("/api/conversations", json={}).json()["id"]
        for message in ["  ", "字" * 6001]:
            assert send(client, identifier, message).status_code == 422
        assert send(client, str(uuid4()), "你好").status_code == 404


def test_missing_key_returns_actionable_error(settings):
    settings.api_key = Settings(_env_file=None, api_key="").api_key
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/config").json()["configured"] is False
        identifier = client.post("/api/conversations", json={}).json()["id"]
        response = send(client, identifier, "你好")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "model_not_configured"


async def test_context_budget_keeps_complete_recent_pairs(settings):
    settings.history_turns = 2
    settings.context_char_limit = 6000
    store = ConversationStore(settings.database_path)
    identifier = store.create().id
    for index in range(3):
        store.append(identifier, str(uuid4()), f"问题{index}", "文" * 3000, False)
    model = RecordingModel()
    runtime = ChatRuntime(settings, model, store)
    await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="继续"))
    assert [message.role for message in model.calls[0]] == ["system", "user", "assistant", "user"]
    assert model.calls[0][1].content == "问题2"


async def test_responses_runtime_history_failure_and_retry(settings):
    settings.api_mode = "responses"
    calls = []

    def handler(request):
        assert request.url.path == "/v1/responses"
        calls.append(json.loads(request.content))
        if len(calls) == 2:
            return httpx.Response(200, json={"status": "failed", "error": {"message": "private"}})
        return httpx.Response(200, json={
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "你好！"},
            ]}],
        })

    store = ConversationStore(settings.database_path)
    identifier = store.create().id
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = ChatRuntime(settings, CompatibleChatModel(settings, client), store)
        first = await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="你好"))
        assert first.truncated is True
        request = ChatRequest(request_id=uuid4(), message="继续")
        with pytest.raises(AppError) as caught:
            await runtime.chat(identifier, request)
        assert caught.value.code == "model_request_failed"
        assert len(store.get(identifier).turns) == 1
        second = await runtime.chat(identifier, request)
        assert await runtime.chat(identifier, request) == second
    assert len(calls) == 3
    assert calls[1] == calls[2]
    assert [message["role"] for message in calls[2]["input"]] == ["system", "user", "assistant", "user"]
    assert calls[2]["input"][2]["content"] == first.assistant
    restored = ConversationStore(settings.database_path).get(identifier)
    assert len(restored.turns) == 2
    assert all(turn.truncated for turn in restored.turns)


async def test_busy_guard_and_timeout_release_lock(settings):
    started = asyncio.Event()

    class BlockingModel:
        async def complete(self, messages):
            started.set()
            await asyncio.Event().wait()

    settings.request_timeout_seconds = 1
    store = ConversationStore(settings.database_path)
    identifier = store.create().id
    runtime = ChatRuntime(settings, BlockingModel(), store)
    request = ChatRequest(request_id=uuid4(), message="你好")
    task = asyncio.create_task(runtime.chat(identifier, request))
    await started.wait()
    with pytest.raises(AppError, match="已有对话"):
        await runtime.chat(identifier, request)
    with pytest.raises(AppError, match="等待回复"):
        await runtime.delete(identifier)
    with pytest.raises(AppError, match="超时"):
        await task
    assert not runtime.lock.locked()
    assert store.get(identifier).turns == []

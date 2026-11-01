import json

import httpx
import pytest
from pydantic import ValidationError

from rust_coach.config import Settings
from rust_coach.contracts import AppError, ModelMessage
from rust_coach.extensions import CalculationInput, DecisionInput, EvidenceQuery, UnavailableExtensions
from rust_coach.llm import CompatibleChatModel


@pytest.mark.parametrize("token_parameter", ["max_tokens", "max_completion_tokens"])
async def test_openai_compatible_payload_and_truncation(token_parameter):
    def handler(request):
        assert str(request.url) == "https://provider.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-5.6-sol"
        assert payload[token_parameter] == 2048
        assert payload["messages"] == [{"role": "user", "content": "你好"}]
        assert payload["stream"] is False
        return httpx.Response(200, json={"choices": [{"message": {"content": "你好！"}, "finish_reason": "length"}]})

    settings = Settings(_env_file=None, base_url="https://provider.example/v1/", api_key="test-secret", api_mode="chat_completions", output_token_parameter=token_parameter)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await CompatibleChatModel(settings, client).complete([ModelMessage(role="user", content="你好")])
    assert reply.content == "你好！"
    assert reply.truncated is True


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
@pytest.mark.parametrize("status,body,expected", [
    (401, {"error": "test-secret"}, "model_auth_failed"),
    (403, {}, "model_auth_failed"),
    (429, {}, "model_rate_limited"),
    (500, {}, "model_request_failed"),
    (302, {}, "model_request_failed"),
    (200, {}, "model_invalid_response"),
    (200, {"choices": []}, "model_invalid_response"),
    (200, {"choices": [{"message": {"content": ""}}]}, "model_invalid_response"),
    (200, {"choices": [{"message": {"content": "text"}, "finish_reason": "tool_calls"}]}, "model_invalid_response"),
])
async def test_provider_errors_are_sanitized(api_mode, status, body, expected):
    settings = Settings(_env_file=None, api_key="test-secret", api_mode=api_mode)
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AppError) as caught:
            await CompatibleChatModel(settings, client).complete([])
    assert caught.value.code == expected
    assert "test-secret" not in caught.value.message


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
@pytest.mark.parametrize("error_type,expected", [(httpx.ReadTimeout, "model_timeout"), (httpx.ConnectError, "model_unreachable")])
async def test_network_errors(api_mode, error_type, expected):
    def handler(request):
        raise error_type("private provider detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AppError) as caught:
            await CompatibleChatModel(Settings(_env_file=None, api_key="test-secret", api_mode=api_mode), client).complete([])
    assert caught.value.code == expected
    assert "private" not in caught.value.message


def response_body(content=None, **overrides):
    return {
        "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": (
            content if content is not None else [{"type": "output_text", "text": "你好！"}]
        )}],
        **overrides,
    }


@pytest.mark.parametrize("instruction_role", ["system", "developer"])
@pytest.mark.parametrize("token_parameter", ["max_tokens", "max_completion_tokens"])
async def test_responses_payload_preserves_conversation(instruction_role, token_parameter):
    messages = [
        ModelMessage(role=instruction_role, content="使用中文回答"),
        ModelMessage(role="user", content="你好"),
        ModelMessage(role="assistant", content="你好！"),
        ModelMessage(role="user", content="继续"),
    ]

    def handler(request):
        assert str(request.url) == "https://provider.example/v1/responses"
        assert request.headers["authorization"] == "Bearer test-secret"
        assert json.loads(request.content) == {
            "model": "responses-only-model",
            "input": [message.model_dump() for message in messages],
            "stream": False,
            "store": False,
            "max_output_tokens": 4096,
        }
        return httpx.Response(200, json=response_body())

    settings = Settings(
        _env_file=None, api_mode="responses", base_url="https://provider.example/v1/",
        model="responses-only-model", api_key="test-secret", max_output_tokens=4096,
        output_token_parameter=token_parameter,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await CompatibleChatModel(settings, client).complete(messages)
    assert reply.content == "你好！"
    assert reply.truncated is False


@pytest.mark.parametrize("body,expected,truncated", [
    (response_body(), "你好！", False),
    (response_body([{"type": "refusal", "refusal": "无法回答这个问题。"}]), "无法回答这个问题。", False),
    (response_body(status="incomplete", incomplete_details={"reason": "max_output_tokens"}), "你好！", True),
    (response_body(output=[
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "不要展示"}]},
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": " 第一段"},
            {"type": "output_text", "text": "\n第二段"},
        ]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "\n第三段 "}]},
    ]), "第一段\n第二段\n第三段", False),
])
async def test_responses_output_parsing(body, expected, truncated):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    async with httpx.AsyncClient(transport=transport) as client:
        reply = await CompatibleChatModel(Settings(_env_file=None, api_key="test-secret", api_mode="responses"), client).complete([])
    assert reply.content == expected
    assert reply.truncated is truncated


@pytest.mark.parametrize("body,expected", [
    (response_body(status="failed", error={"message": "test-secret"}), "model_request_failed"),
    (response_body(error={"message": "test-secret"}), "model_request_failed"),
    (response_body(status="queued"), "model_invalid_response"),
    (response_body(status="in_progress"), "model_invalid_response"),
    (response_body(status="incomplete", incomplete_details={"reason": "content_filter"}), "model_invalid_response"),
    (response_body(status="incomplete", incomplete_details=None), "model_invalid_response"),
    (response_body(output=[]), "model_invalid_response"),
    (response_body(output=None), "model_invalid_response"),
    (response_body(output=[{"type": "reasoning"}]), "model_invalid_response"),
    (response_body(output=[{"type": "function_call"}]), "model_invalid_response"),
    (response_body(output=[None]), "model_invalid_response"),
    (response_body([]), "model_invalid_response"),
    (response_body([{"type": "output_text", "text": "  "}]), "model_invalid_response"),
    (response_body([{"type": "output_text", "text": 42}]), "model_invalid_response"),
    (response_body([{"type": "output_text", "text": "字" * 60001}]), "model_invalid_response"),
    (response_body([{"type": "unknown", "text": "test-secret"}]), "model_invalid_response"),
    (None, "model_invalid_response"),
    ([], "model_invalid_response"),
])
async def test_responses_errors_are_sanitized(body, expected):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=json.dumps(body)))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AppError) as caught:
            await CompatibleChatModel(Settings(_env_file=None, api_key="test-secret", api_mode="responses"), client).complete([])
    assert caught.value.code == expected
    assert "test-secret" not in caught.value.message


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
@pytest.mark.parametrize("body,expected", [
    (b"not JSON", "model_invalid_response"),
    (b"x" * 2_000_001, "model_response_too_large"),
])
async def test_invalid_or_oversized_response(api_mode, body, expected):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AppError) as caught:
            await CompatibleChatModel(Settings(_env_file=None, api_key="test-secret", api_mode=api_mode), client).complete([])
    assert caught.value.code == expected


def test_api_mode_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("COACH_API_MODE", raising=False)
    assert Settings(_env_file=None).api_mode == "responses"
    env_file = tmp_path / ".env"
    env_file.write_text("COACH_API_MODE=chat_completions\n", encoding="utf-8")
    assert Settings(_env_file=env_file).api_mode == "chat_completions"
    monkeypatch.setenv("COACH_API_MODE", "responses")
    assert Settings(_env_file=env_file).api_mode == "responses"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_mode="unknown")


async def test_extension_placeholders_never_return_fabricated_results():
    extensions = UnavailableExtensions()
    results = [
        await extensions.retrieve(EvidenceQuery(question="石墙")),
        await extensions.search(EvidenceQuery(question="版本")),
        await extensions.calculate(CalculationInput(operation="raid.calculate", user_input="拆墙")),
        await extensions.plan_gap(CalculationInput(operation="craft.plan_gap", user_input="合成")),
        await extensions.advise(DecisionInput(goal="下一步")),
    ]
    assert all(result.status == "unavailable" for result in results)

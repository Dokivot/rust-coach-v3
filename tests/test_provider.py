import json

import httpx
import pytest

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

    settings = Settings(_env_file=None, base_url="https://provider.example/v1/", api_key="test-secret", output_token_parameter=token_parameter)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reply = await CompatibleChatModel(settings, client).complete([ModelMessage(role="user", content="你好")])
    assert reply.content == "你好！"
    assert reply.truncated is True


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
async def test_provider_errors_are_sanitized(status, body, expected):
    settings = Settings(_env_file=None, api_key="test-secret")
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(AppError) as caught:
            await CompatibleChatModel(settings, client).complete([])
    assert caught.value.code == expected
    assert "test-secret" not in caught.value.message


@pytest.mark.parametrize("error_type,expected", [(httpx.ReadTimeout, "model_timeout"), (httpx.ConnectError, "model_unreachable")])
async def test_network_errors(error_type, expected):
    def handler(request):
        raise error_type("private provider detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AppError) as caught:
            await CompatibleChatModel(Settings(_env_file=None, api_key="test-secret"), client).complete([])
    assert caught.value.code == expected
    assert "private" not in caught.value.message


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

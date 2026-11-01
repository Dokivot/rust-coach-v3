import json
from typing import Protocol

import httpx

from rust_coach.config import Settings
from rust_coach.contracts import AppError, ModelMessage, ModelReply


class ChatModel(Protocol):
    async def complete(self, messages: list[ModelMessage]) -> ModelReply: ...


class CompatibleChatModel:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def complete(self, messages: list[ModelMessage]) -> ModelReply:
        if not self.settings.configured:
            raise AppError("model_not_configured", "请在本地 .env 配置 COACH_API_KEY，然后重启服务。", 503)
        payload = {
            "model": self.settings.model,
            "messages": [message.model_dump() for message in messages],
            "stream": False,
            self.settings.output_token_parameter: self.settings.max_output_tokens,
        }
        try:
            async with self.client.stream(
                "POST",
                f"{self.settings.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.settings.api_key.get_secret_value()}"},
            ) as response:
                if response.status_code in {401, 403}:
                    raise AppError("model_auth_failed", "模型服务拒绝了凭据，请检查 API Key 和模型权限。", 502)
                if response.status_code == 429:
                    raise AppError("model_rate_limited", "模型服务限流或额度不足，请稍后再试。", 429)
                if not response.is_success:
                    raise AppError("model_request_failed", "模型请求失败，请检查 Base URL、模型名和兼容参数。", 502)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2_000_000:
                        raise AppError("model_response_too_large", "模型返回内容过大，请缩短问题后重试。", 502)
        except httpx.TimeoutException as error:
            raise AppError("model_timeout", "模型响应超时，请稍后重试。", 504) from error
        except httpx.RequestError as error:
            raise AppError("model_unreachable", "无法连接模型服务，请检查网络和 Base URL。", 502) from error
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            content = choice["message"].get("content") or choice["message"].get("refusal")
            if not isinstance(content, str) or not content.strip() or len(content) > 60000:
                raise ValueError("Invalid content")
            if choice.get("finish_reason") in {"tool_calls", "function_call"}:
                raise ValueError("Tool calls are not enabled")
            return ModelReply(content=content.strip(), truncated=choice.get("finish_reason") == "length")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
            raise AppError("model_invalid_response", "模型返回了无法显示的内容，请检查接口是否兼容 Chat Completions。", 502) from error

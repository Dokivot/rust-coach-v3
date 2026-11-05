import json
from typing import Protocol

import httpx

from rust_coach.config import Settings
from rust_coach.contracts import AppError, ImagePart, ModelMessage, ModelReply
from rust_coach.performance import provider_dispatched, provider_usage, transport_status


class StreamUsage:
    def __init__(self):
        self.pending = bytearray()
        self.lines = []

    def feed(self, chunk):
        self.pending.extend(chunk)
        while b"\n" in self.pending:
            line, _, remaining = self.pending.partition(b"\n")
            self.pending = remaining
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                self.lines.append(line[5:].lstrip(b" "))
            elif not line and self.lines:
                frame = b"\n".join(self.lines)
                self.lines = []
                try:
                    event = json.loads(frame)
                except (ValueError, UnicodeError):
                    continue
                response = event.get("response") if isinstance(event, dict) else None
                if isinstance(response, dict) and response.get("usage") is not None:
                    provider_usage(response["usage"])


class ChatModel(Protocol):
    async def complete(self, messages: list[ModelMessage]) -> ModelReply: ...


class CompatibleChatModel:
    records_transport = True

    def __init__(self, settings: Settings, client: httpx.AsyncClient, *, stream_responses: bool = False):
        self.settings = settings
        self.client = client
        self.stream_responses = stream_responses

    async def complete(self, messages: list[ModelMessage]) -> ModelReply:
        if not self.settings.configured:
            raise AppError("model_not_configured", "请在本地 .env 配置 COACH_API_KEY，然后重启服务。", 503)
        payload = {
            "model": self.settings.model,
            "stream": False,
        }
        serialized_messages = []
        for message in messages:
            if isinstance(message.content, str):
                serialized_messages.append(message.model_dump())
                continue
            parts = []
            for part in message.content:
                if isinstance(part, ImagePart):
                    parts.append({"type": "input_image", "image_url": part.data_url, "detail": part.detail}
                        if self.settings.api_mode == "responses" else
                        {"type": "image_url", "image_url": {"url": part.data_url, "detail": part.detail}})
                else:
                    parts.append({"type": "input_text" if self.settings.api_mode == "responses" else "text", "text": part.text})
            serialized_messages.append({"role": message.role, "content": parts})
        if self.settings.api_mode == "responses":
            endpoint = "responses"
            payload["stream"] = self.stream_responses
            payload.update(input=serialized_messages, max_output_tokens=self.settings.max_output_tokens, store=False)
        else:
            endpoint = "chat/completions"
            payload.update(messages=serialized_messages)
            payload[self.settings.output_token_parameter] = self.settings.max_output_tokens
        try:
            stream_usage = StreamUsage() if self.settings.api_mode == "responses" and self.stream_responses else None
            provider_dispatched()
            async with self.client.stream(
                "POST",
                f"{self.settings.base_url}/{endpoint}",
                json=payload,
                headers={"Authorization": f"Bearer {self.settings.api_key.get_secret_value()}"},
            ) as response:
                transport_status(response.status_code)
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
                    if stream_usage is not None:
                        stream_usage.feed(chunk)
        except httpx.TimeoutException as error:
            raise AppError("model_timeout", "模型响应超时，请稍后重试。", 504) from error
        except httpx.RequestError as error:
            raise AppError("model_unreachable", "无法连接模型服务，请检查网络和 Base URL。", 502) from error
        try:
            if self.settings.api_mode == "responses" and self.stream_responses:
                reply = self._parse_response_stream(raw)
            elif self.settings.api_mode == "responses":
                data = json.loads(raw)
                reply = self._parse_response(data)
            else:
                data = json.loads(raw)
                provider_usage(data.get("usage"))
                choice = data["choices"][0]
                content = choice["message"].get("content") or choice["message"].get("refusal")
                if not isinstance(content, str):
                    raise ValueError("Invalid content")
                if choice.get("finish_reason") in {"tool_calls", "function_call"}:
                    raise ValueError("Tool calls are not enabled")
                reply = ModelReply(content=content, truncated=choice.get("finish_reason") == "length", usage=data.get("usage"))
            if not reply.content.strip() or len(reply.content) > 60000:
                raise ValueError("Invalid content")
            return ModelReply(content=reply.content.strip(), truncated=reply.truncated, usage=reply.usage)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
            raise AppError("model_invalid_response", "模型返回了无法显示的内容，请检查接口是否与 COACH_API_MODE 配置兼容。", 502) from error

    def _parse_response_stream(self, raw: bytes) -> ModelReply:
        frames, data_lines = [], []
        for line in raw.decode("utf-8").splitlines() + [""]:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            elif not line and data_lines:
                frame = "\n".join(data_lines)
                if frame != "[DONE]":
                    frames.append(json.loads(frame))
                data_lines = []
        messages, parts, final = {}, {}, None
        for event in frames:
            kind = event["type"]
            if isinstance(event.get("response"), dict) and event["response"].get("usage") is not None:
                provider_usage(event["response"]["usage"])
            if kind == "error" or kind == "response.failed":
                raise AppError("model_request_failed", "模型生成失败，请稍后重试或检查模型配置。", 502)
            if kind == "response.output_item.added":
                item = event["item"]
                if item["type"] == "reasoning":
                    continue
                if item["type"] != "message" or item["role"] != "assistant":
                    raise ValueError("Unsupported streaming output item")
                identifier = item["id"]
                if identifier in messages:
                    raise ValueError("Duplicate streaming message")
                messages[identifier] = event["output_index"]
            elif kind in {"response.output_text.delta", "response.refusal.delta"}:
                identifier = event["item_id"]
                if identifier not in messages or final is not None or not isinstance(event["delta"], str):
                    raise ValueError("Invalid streaming delta")
                key = (messages[identifier], event["content_index"], identifier)
                parts.setdefault(key, []).append(event["delta"])
            elif kind in {"response.completed", "response.incomplete"}:
                if final is not None:
                    raise ValueError("Duplicate terminal event")
                final = event["response"]
                expected = "completed" if kind == "response.completed" else "incomplete"
                if final.get("status") != expected:
                    raise ValueError("Inconsistent terminal event")
        if final is None:
            raise ValueError("Missing terminal event")
        output = [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "".join(chunks)},
        ]} for _, chunks in sorted(parts.items())]
        return self._parse_response({**final, "output": output})

    def _parse_response(self, data: dict) -> ModelReply:
        provider_usage(data.get("usage"))
        if data.get("error") or data.get("status") == "failed":
            raise AppError("model_request_failed", "模型生成失败，请稍后重试或检查模型配置。", 502)
        truncated = data.get("status") == "incomplete"
        if truncated:
            if data["incomplete_details"]["reason"] != "max_output_tokens":
                raise ValueError("Unsupported incomplete response")
        elif data.get("status") != "completed":
            raise ValueError("Response is not complete")
        output = data["output"]
        if not isinstance(output, list):
            raise ValueError("Invalid output")
        parts = []
        for item in output:
            if item["type"] == "reasoning":
                continue
            if item["type"] != "message" or item["role"] != "assistant":
                raise ValueError("Unsupported output item")
            if not isinstance(item["content"], list):
                raise ValueError("Invalid message content")
            for part in item["content"]:
                if part["type"] == "output_text":
                    text = part["text"]
                elif part["type"] == "refusal":
                    text = part["refusal"]
                else:
                    raise ValueError("Unsupported content type")
                if not isinstance(text, str):
                    raise ValueError("Invalid content")
                parts.append(text)
        return ModelReply(content="".join(parts), truncated=truncated, usage=data.get("usage"))

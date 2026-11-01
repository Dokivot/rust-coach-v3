from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COACH_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    base_url: str = "https://cf.api.fan/v1"
    model: str = Field(default="gpt-5.6-sol", min_length=1)
    api_mode: Literal["responses", "chat_completions"] = "responses"
    api_key: SecretStr = SecretStr("")
    database_path: Path = Path("data/coach.sqlite3")
    request_timeout_seconds: float = Field(default=90, ge=1, le=300)
    max_output_tokens: int = Field(default=2048, ge=128, le=16384)
    output_token_parameter: Literal["max_completion_tokens", "max_tokens"] = (
        "max_completion_tokens"
    )
    instruction_role: Literal["system", "developer"] = "system"
    history_turns: int = Field(default=12, ge=1, le=50)
    context_char_limit: int = Field(default=24000, ge=6000, le=100000)
    search_provider: Literal["tavily", "off"] = "tavily"
    search_api_key: SecretStr = SecretStr("")
    evidence_ttl_hours: int = Field(default=6, ge=1, le=72)
    evidence_timeout_seconds: float = Field(default=25, ge=1, le=60)
    advice_timeout_seconds: float = Field(default=180, ge=10, le=300)
    vision_enabled: bool = True
    vision_model: str | None = None
    vision_base_url: str | None = None
    vision_api_key: SecretStr | None = None
    vision_api_mode: Literal["responses", "chat_completions"] | None = None
    vision_detail: Literal["high", "original", "auto"] = "high"
    attachment_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    attachment_max_pixels: int = Field(default=20_000_000, ge=10000, le=40_000_000)
    attachment_retention_hours: int = Field(default=168, ge=1, le=720)
    visual_draft_hours: int = Field(default=24, ge=1, le=168)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        local_http = parsed.scheme == "http" and parsed.hostname in {
            "localhost", "127.0.0.1", "::1"
        }
        if (
            not parsed.hostname
            or (parsed.scheme != "https" and not local_http)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Base URL must be HTTPS, or HTTP on localhost, without credentials/query")
        return value.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value().strip())

    @property
    def search_configured(self) -> bool:
        return self.search_provider == "tavily" and bool(self.search_api_key.get_secret_value().strip())

    @field_validator("vision_base_url")
    @classmethod
    def validate_vision_base_url(cls, value):
        return cls.validate_base_url(value) if value else None

    def for_vision(self):
        return self.model_copy(update={"model": self.vision_model or self.model,
            "base_url": self.vision_base_url or self.base_url,
            "api_key": self.vision_api_key or self.api_key,
            "api_mode": self.vision_api_mode or self.api_mode,
            "max_output_tokens": max(8192, self.max_output_tokens)})

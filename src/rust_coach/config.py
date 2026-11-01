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
    search_provider: Literal["bing", "brave", "off"] = "bing"
    search_api_key: SecretStr = SecretStr("")
    evidence_ttl_hours: int = Field(default=6, ge=1, le=72)
    evidence_timeout_seconds: float = Field(default=25, ge=1, le=60)
    advice_timeout_seconds: float = Field(default=180, ge=10, le=300)

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

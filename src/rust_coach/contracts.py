from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: str


class ModelReply(BaseModel):
    content: str
    truncated: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    message: str = Field(min_length=1, max_length=6000)

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be empty")
        return value


class Turn(BaseModel):
    request_id: str
    user: str
    assistant: str
    created_at: str
    truncated: bool = False


class Conversation(BaseModel):
    id: str
    turns: list[Turn]


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

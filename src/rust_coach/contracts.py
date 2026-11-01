from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from rust_coach.presentation.contracts import PresentationPreferences


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    data_url: str = Field(pattern=r"^data:image/(png|jpeg|webp);base64,")
    detail: Literal["high", "original", "auto"] = "high"


class ModelMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[TextPart | ImagePart]


class ModelReply(BaseModel):
    content: str
    truncated: bool = False
    usage: dict | None = None


class ImageContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: Literal["self", "opponent", "teammate", "unknown"] = "unknown"
    captured_at: datetime | None = None

    @field_validator("captured_at")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Timestamp needs timezone")
        return value


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    message: str = Field(default="", max_length=6000)
    player_inventory_container: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    presentation_preferences: PresentationPreferences | None = None
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=4)
    image_context: dict[UUID, ImageContext] = Field(default_factory=dict, max_length=4)
    visual_draft_id: UUID | None = None

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        value = value.strip()
        return value

    @model_validator(mode="after")
    def nonempty(self):
        if not self.message and not self.attachment_ids:
            raise ValueError("Message or image required")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("Duplicate attachments")
        if set(self.image_context) - set(self.attachment_ids):
            raise ValueError("Unknown attachment context")
        if self.attachment_ids and self.visual_draft_id:
            raise ValueError("Choose new images or an existing draft")
        return self


class Turn(BaseModel):
    request_id: str
    user: str
    assistant: str
    created_at: str
    truncated: bool = False
    calculation: dict | None = None
    player_state_ref: dict | None = None
    advice: dict | None = None
    presentation: dict | None = None
    visual: dict | None = None


class Conversation(BaseModel):
    id: str
    turns: list[Turn]
    presentation_preferences: PresentationPreferences = Field(default_factory=PresentationPreferences)


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

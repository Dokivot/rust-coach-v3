from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rust_coach.calculations.schemas import CalculationRequest


Subject = Literal["self", "opponent", "teammate", "unknown"]
Text = Annotated[str, Field(min_length=1, max_length=400)]
ItemId = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]{1,80}$")]
Quantity = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VisualObservation(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    attachment_id: UUID
    kind: Literal["inventory", "equipment", "base", "other"]
    region_kind: Literal["backpack", "hotbar", "equipped", "storage", "tool_cupboard", "crafting", "scene", "unknown"]
    container_label: str = Field(default="", max_length=80)
    slot: str = Field(default="", max_length=80)
    subject: Subject = "unknown"
    label: Text
    item_id: ItemId | None = None
    item_status: Literal["identified", "ambiguous", "unknown"] | None = None
    quantity: Quantity | None = None
    quantity_status: Literal["readable", "ambiguous", "absent", "unknown"] = "unknown"
    raw_quantity: str = Field(default="", max_length=40)
    description: str = Field(default="", max_length=400)
    bbox: tuple[float, float, float, float]
    uncertainty: list[Text] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def valid_region(self):
        left, top, right, bottom = self.bbox
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError("Invalid normalized bbox")
        if self.quantity_status != "readable" and self.quantity is not None:
            raise ValueError("Uncertain quantity must be null")
        if self.quantity_status == "readable" and self.quantity is None:
            raise ValueError("Readable quantity requires value")
        return self


class ScreenshotContext(StrictModel):
    attachment_id: UUID
    subject: Subject = "self"
    temporal_context: Literal["current", "historical", "example", "unknown"] = "current"
    reason: str = Field(default="", max_length=300)


class VisualExtraction(StrictModel):
    task: Literal["observe", "craft", "raid", "advice"] = "observe"
    observations: list[VisualObservation] = Field(default_factory=list, max_length=120)
    unknowns: list[Text] = Field(default_factory=list, max_length=12)
    image_relationship: Literal["same_snapshot", "different_snapshots", "unknown"] = "unknown"
    inventory_coverage: Literal["full_view", "partial", "unknown"] = "unknown"
    calculation: CalculationRequest | None = None
    screenshot_context: list[ScreenshotContext] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def identities(self):
        if len({item.id for item in self.observations}) != len(self.observations):
            raise ValueError("Duplicate observation ID")
        if len({item.attachment_id for item in self.screenshot_context}) != len(self.screenshot_context):
            raise ValueError("Duplicate screenshot context")
        if self.calculation and (self.calculation.inventory is not None or self.calculation.reserve):
            raise ValueError("Visual extraction cannot set aggregate inventory")
        if self.calculation and self.calculation.operation != self.task:
            raise ValueError("Task mismatch")
        return self


class ObservationEdit(StrictModel):
    observation_id: str = Field(min_length=1, max_length=40)
    accepted: bool = True
    item_id: ItemId | None = None
    quantity: Quantity | None = None
    description: str | None = Field(default=None, max_length=400)


class VisualConfirmation(StrictModel):
    confirmation_id: UUID
    draft_revision: int = Field(strict=True, ge=1)
    expected_state_revision: int | None = Field(default=None, strict=True, ge=0)
    action: Literal["accept_for_turn", "save_to_profile"] = "accept_for_turn"
    observed_at: datetime
    subject: Subject
    container_id: str = Field(default="backpack", pattern=r"^[a-zA-Z0-9_.:-]{1,80}$")
    inventory_complete: bool = False
    same_snapshot: bool = False
    quantities_are_container_totals: bool = False
    edits: list[ObservationEdit] = Field(default_factory=list, max_length=120)
    calculation: CalculationRequest | None = None
    continue_task: bool = True

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("Timestamp requires timezone")
        return value

    @model_validator(mode="after")
    def unique_edits(self):
        if len({edit.observation_id for edit in self.edits}) != len(self.edits):
            raise ValueError("Duplicate edit")
        if self.calculation and (self.calculation.inventory is not None or self.calculation.reserve):
            raise ValueError("Use confirmed observation quantities for inventory")
        return self

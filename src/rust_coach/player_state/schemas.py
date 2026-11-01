from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from rust_coach.vision.schemas import VisualObservation


Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")]
ItemId = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")]
Quantity = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
Text = Annotated[str, Field(min_length=1, max_length=500)]
ItemList = Annotated[list[ItemId], Field(max_length=100)]
Duration = Annotated[int, Field(strict=True, ge=0, le=100_000)]


FIELD_DEFINITIONS = {
    "technology.workbench": (Annotated[int, Field(strict=True, ge=0, le=3)], "已有工作台等级", "number", 1440, 0, 3),
    "technology.unlocked_blueprints": (ItemList, "已确认可制作的配方 ID（未列出不代表未解锁）", "items", 10080, None, None),
    "base.has_base": (bool, "是否已有基地", "boolean", 1440, None, None),
    "base.entrance_secured": (bool, "已确认入口封闭且上锁", "boolean", 120, None, None),
    "base.grade": (Literal["twig", "wood", "stone", "metal", "armored", "mixed"], "基地主要等级", "choice", 1440, None, None),
    "base.damage_notes": (Text, "已知基地缺口或受损情况", "text", 120, None, None),
    "base.upkeep_minutes": (Duration, "工具柜显示的剩余维护时间（分钟）", "number", 120, 0, 100000),
    "base.location": (Text, "基地位置", "text", 1440, None, None),
    "session.location": (Text, "当前玩家位置", "text", 30, None, None),
    "session.team_size": (Annotated[int, Field(strict=True, ge=1, le=100)], "队伍人数（含自己）", "number", 1440, 1, 100),
    "session.teammates_online": (Annotated[int, Field(strict=True, ge=0, le=99)], "在线队友人数（不含自己）", "number", 30, 0, 99),
    "session.online_minutes": (Duration, "本次还可在线多久（分钟）", "number", 30, 0, 100000),
    "session.offline_minutes": (Duration, "预计离线多久（分钟）", "number", 120, 0, 100000),
    "session.threats": (Text, "已观察到的威胁", "text", 30, None, None),
    "progress.stage": (Literal["starting", "developing", "established", "recovering"], "玩家自报阶段", "choice", 1440, None, None),
    "progress.wipe_age_hours": (Annotated[int, Field(strict=True, ge=0, le=100000)], "本轮开服已过小时数", "number", 1440, 0, 100000),
    "goals.primary": (Text, "当前主要目标", "text", 1440, None, None),
    "preferences.risk": (Literal["cautious", "balanced", "aggressive"], "风险偏好", "choice", 43200, None, None),
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeCreate(StrictModel):
    server_id: str = Field(min_length=1, max_length=80)
    wipe_id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=100)
    ruleset: Literal["pc_vanilla"] = "pc_vanilla"

    @field_validator("server_id", "wipe_id", "label")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Cannot be blank")
        return value.strip()


class InventoryChange(StrictModel):
    container_id: Identifier = "backpack"
    mode: Literal["patch", "replace"] = "patch"
    complete: bool = Field(default=False, strict=True)
    items: dict[ItemId, Quantity] = Field(default_factory=dict, max_length=150)

    @model_validator(mode="after")
    def coverage(self):
        if self.complete and self.mode != "replace":
            raise ValueError("Complete inventory must be a replacement")
        if self.mode == "patch" and not self.items:
            raise ValueError("Empty patch")
        return self


class StateChanges(StrictModel):
    facts: dict[str, object] = Field(default_factory=dict, max_length=30)
    inventory: list[InventoryChange] = Field(default_factory=list, max_length=10)
    clear_fields: list[str] = Field(default_factory=list, max_length=30)
    invalidate_containers: list[Identifier] = Field(default_factory=list, max_length=20)
    event: Literal["death", "raided", "blueprint_reset", "invalidate_all"] | None = None
    visual_observations: list[VisualObservation] = Field(default_factory=list, max_length=120)

    @field_validator("facts")
    @classmethod
    def validate_facts(cls, value):
        result = {}
        for key, fact in value.items():
            if key not in FIELD_DEFINITIONS:
                raise ValueError("Unsupported state field: " + key)
            result[key] = TypeAdapter(FIELD_DEFINITIONS[key][0]).validate_python(fact, strict=True)
            if isinstance(result[key], str) and not result[key].strip():
                raise ValueError("Blank state value")
            if isinstance(result[key], list) and len(result[key]) != len(set(result[key])):
                raise ValueError("Duplicate items")
        return result

    @field_validator("clear_fields")
    @classmethod
    def validate_clear_fields(cls, value):
        if any(key not in FIELD_DEFINITIONS for key in value) or len(value) != len(set(value)):
            raise ValueError("Invalid field to clear")
        return value

    @model_validator(mode="after")
    def unambiguous(self):
        containers = [change.container_id for change in self.inventory]
        if len(containers) != len(set(containers)):
            raise ValueError("Duplicate container changes")
        if set(self.clear_fields) & self.facts.keys() or set(containers) & set(self.invalidate_containers):
            raise ValueError("Conflicting changes")
        if len(self.invalidate_containers) != len(set(self.invalidate_containers)):
            raise ValueError("Duplicate container invalidation")
        if self.event and (self.facts or self.inventory or self.clear_fields or self.invalidate_containers or self.visual_observations):
            raise ValueError("Submit event separately from observations")
        if not (self.facts or self.inventory or self.clear_fields or self.invalidate_containers or self.event or self.visual_observations):
            raise ValueError("No state changes")
        return self


class StateUpdate(StrictModel):
    observation_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    observed_at: datetime
    source: Literal["manual", "text_confirmed"] = "manual"
    changes: StateChanges

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Observation timestamp requires a timezone")
        return value


class StateBinding(StrictModel):
    scope_id: UUID | None


class StatePreview(StrictModel):
    message: str = Field(min_length=1, max_length=6000)


def field_catalog():
    return {key: {"label": definition[1], "input": definition[2], "ttl_minutes": definition[3],
                  "minimum": definition[4], "maximum": definition[5],
                  "schema": TypeAdapter(definition[0]).json_schema()}
            for key, definition in FIELD_DEFINITIONS.items()}

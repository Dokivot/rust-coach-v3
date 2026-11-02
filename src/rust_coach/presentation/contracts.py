from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PresentationPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verbosity: Literal["concise", "standard", "detailed"] = "standard"
    language: Literal["auto", "zh", "en"] = "auto"
    terminology: Literal["plain", "game_terms"] = "plain"


class ResponseContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_version: str = "rust-coach-core-v1"
    prompt_version: str = "coach-prompts-v1"
    renderer_version: str = "coach-render-v3"
    preferences: PresentationPreferences = Field(default_factory=PresentationPreferences)
    language: Literal["zh", "en"] = "zh"
    answer_focus: Literal["general", "resource_gap", "feasibility"] = "general"
    use_c4_alias: bool = False
    scenario: Literal["normal", "urgent", "teaching", "recovery"] = "normal"
    scenario_reason: str = "default"
    capabilities: tuple[str, ...] = ()
    model_config_id: str = "unspecified"
    teaching: dict = Field(default_factory=dict)


class AnswerBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: Literal["protected", "narrative"]
    text: str
    placement: Literal["main", "detail"] = "main"
    reference: str | None = None


class ProtectedAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["quantity", "feasibility", "observation", "comparison", "evidence_limit", "selection"]
    reference: str
    value: dict


class AnswerEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["chat", "calculation", "advice", "state", "clarification", "visual"]
    context: ResponseContext
    protected_payload: dict = Field(default_factory=dict)
    blocks: tuple[AnswerBlock, ...]
    assertions: tuple[ProtectedAssertion, ...] = ()
    checks: tuple[dict, ...] = ()
    repair_count: int = 0
    fallback_reason: str | None = None

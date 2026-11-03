from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rust_coach.calculations.schemas import PositiveQuantity


Text = Annotated[str, Field(min_length=1, max_length=500)]
Topic = Literal["first_day", "building", "upkeep", "progression", "recycling", "monuments", "combat", "updates"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    title: Text
    kind: Literal["observe", "craft", "strategy"]
    goals: dict[str, PositiveQuantity] = Field(default_factory=dict, max_length=8)
    required_fields: list[str] = Field(default_factory=list, max_length=18)

    @model_validator(mode="after")
    def craft_goals(self):
        if bool(self.goals) != (self.kind == "craft"):
            raise ValueError("Only craft actions require goals")
        return self


class AdvicePlan(StrictModel):
    goal: Text
    topics: list[Topic] = Field(default_factory=list, max_length=2)
    evidence_reason: Text
    candidates: list[Candidate] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_actions(self):
        if len({candidate.id for candidate in self.candidates}) != len(self.candidates):
            raise ValueError("Duplicate candidate IDs")
        return self


class Citation(StrictModel):
    document_id: str = Field(min_length=1, max_length=64)
    quote: str = Field(min_length=20, max_length=1200)


class Recommendation(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=31)
    reason: Text
    tradeoff: Text
    prerequisites: list[Text] = Field(min_length=1, max_length=5)
    steps: list[Text] = Field(min_length=1, max_length=5)
    completion: Text
    switch_when: Text
    citations: list[Citation] = Field(default_factory=list, max_length=4)
    visual_citations: list[str] = Field(default_factory=list, max_length=12)


class AdviceAnswer(StrictModel):
    recommendation: Recommendation
    alternative: Recommendation | None = None
    missing: list[Text] = Field(default_factory=list, max_length=5)
    uncertainty: Text


class GuideDraftStage(StrictModel):
    action: Text
    completion: Text
    evidence_refs: list[str] = Field(min_length=1, max_length=3)


class GuideContent(StrictModel):
    goal: Text
    assumption: Text
    stop_when: Text
    followup: str = Field(default="", max_length=150)
    limitations: list[Text] = Field(default_factory=list, max_length=3)


class GuideDraft(GuideContent):
    stages: list[GuideDraftStage] = Field(min_length=3, max_length=5)
    stop_evidence_refs: list[str] = Field(default_factory=list, max_length=3)


class GuideStage(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    action: Text
    completion: Text
    citations: list[Citation] = Field(min_length=1, max_length=3)


class GuideAnswer(GuideContent):
    stages: list[GuideStage] = Field(min_length=3, max_length=5)
    stop_citations: list[Citation] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def unique_stages(self):
        if len({stage.id for stage in self.stages}) != len(self.stages):
            raise ValueError("Duplicate stage IDs")
        return self


class GuideCorrection(StrictModel):
    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    action: Text
    completion: Text


class GuideReview(StrictModel):
    supported_stage_ids: list[str] = Field(max_length=5, description="无需改写即可由引用支持的阶段 ID")
    corrections: list[GuideCorrection] = Field(default_factory=list, max_length=5, description="已根据原引用修正并核对的阶段")
    issues: list[Text] = Field(default_factory=list, max_length=5)

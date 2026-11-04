from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,160}$")
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=12000)
    entities: list[str] = Field(default_factory=list, max_length=20)
    claims: list[str] = Field(min_length=1, max_length=20)
    terms: str = Field(default="", max_length=2000)
    kind: Literal["rule", "captured_excerpt", "explanation"]
    source: str = Field(min_length=1, max_length=500)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    fetched_at: str | None = None
    snapshot: str
    platform: str = "pc"
    ruleset: str = "pc_vanilla"
    branch: str = "demo_snapshot"
    admission: Literal["snapshot_checked", "pending", "conflicted"] = "snapshot_checked"
    verification: Literal["snapshot_only", "excerpt_crosscheck_build_unknown", "implementation_explanation"]


class KnowledgeRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")
    snapshot: Literal["rustdata-pc-vanilla-2026-11-01"]
    rules_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    verification: Literal["build_unverified_no_independent_human_review"]
    game_build: None = None
    documents: list[EvidenceUnit] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_documents(self):
        identifiers = [document.id for document in self.documents]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate_knowledge_document")
        return self


class KnowledgeFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=100)
    turn_request_id: str = Field(min_length=1, max_length=100)
    document_ids: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=1000)

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


SchemaVersion = Literal["input-v1", "understanding-v1", "decision-v1", "generation-v1"]
RouteKind = Literal["simple_call", "plan_execute", "chat", "state", "unsupported"]
DecisionStatus = Literal[
    "verified", "conditional", "partial", "blocked", "unavailable", "needs_input", "failed"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Provenance(StrictModel):
    source_ref: str = Field(min_length=1, max_length=240)
    source_type: Literal["text", "attachment", "visual", "state", "memory", "tool", "knowledge"]
    confidence: float | None = Field(default=None, ge=0, le=1)
    observed_at: datetime | None = None


class AttachmentRef(StrictModel):
    attachment_id: UUID
    sha256: str | None = Field(default=None, min_length=16, max_length=128)
    media_type: Literal["image/png", "image/jpeg", "image/webp"] = "image/png"


class RequestCandidate(StrictModel):
    kind: str = Field(min_length=1, max_length=80)
    operation: str | None = Field(default=None, max_length=80)
    target: str | None = Field(default=None, max_length=120)
    desired_outcome: str | None = Field(default=None, max_length=500)
    text: str = Field(min_length=1, max_length=6000)
    source_ref: str = Field(min_length=1, max_length=240)


class StateCandidate(StrictModel):
    field: str = Field(min_length=1, max_length=120)
    value: Any = None
    status: Literal["candidate", "confirmed", "unknown", "conflict"] = "candidate"
    source: Provenance
    coverage: Literal["full", "partial", "unknown"] = "unknown"


class ParsedInput(StrictModel):
    schema_version: Literal["input-v1"] = "input-v1"
    input_revision: int = Field(default=1, ge=1)
    request_id: UUID
    task_id: UUID
    prompt: str = Field(default="", max_length=6000)
    attachments: tuple[AttachmentRef, ...] = ()
    request_candidates: tuple[RequestCandidate, ...] = ()
    player_state_candidates: tuple[StateCandidate, ...] = ()
    unknowns: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    provenance: tuple[Provenance, ...] = ()

    @model_validator(mode="after")
    def has_input(self):
        if not self.prompt.strip() and not self.attachments:
            raise ValueError("ParsedInput requires prompt or attachment")
        return self


class IntentSpec(StrictModel):
    kind: str = Field(min_length=1, max_length=100)
    target: str | None = Field(default=None, max_length=120)
    desired_outcome: str = Field(min_length=1, max_length=500)
    mode: Literal["exact", "advisory", "conversational"]
    confidence: float = Field(ge=0, le=1)


class StateRef(StrictModel):
    scope_id: str | None = None
    revision: int | None = Field(default=None, ge=0)


class Uncertainty(StrictModel):
    semantic: Literal["resolved", "partial", "unknown"] = "unknown"
    factual: Literal["known", "partial", "unknown", "conflict"] = "unknown"
    coverage: Literal["full", "partial", "unknown"] = "unknown"
    freshness: Literal["known", "stale", "unknown"] = "unknown"
    capability: Literal["known", "unknown", "unsupported"] = "unknown"


class CoachTaskRepresentation(StrictModel):
    schema_version: Literal["understanding-v1"] = "understanding-v1"
    task_id: UUID
    understanding_revision: int = Field(default=1, ge=1)
    input_revision: int = Field(default=1, ge=1)
    intent: IntentSpec
    goals: tuple[str, ...] = ()
    constraints: dict[str, Any] = Field(default_factory=dict)
    state_ref: StateRef = Field(default_factory=StateRef)
    unknowns: tuple[str, ...] = ()
    uncertainty: Uncertainty = Field(default_factory=Uncertainty)
    route: RouteKind
    required_evidence: tuple[str, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    action: Literal["new", "continue", "correct", "switch", "cancel", "clarify", "inspect"] = "new"
    execution_node: str | None = None
    routing_reason: str | None = None
    previous_task_id: UUID | None = None


class Budget(StrictModel):
    max_steps: int = Field(default=6, ge=1, le=50)
    max_tool_calls: int = Field(default=12, ge=1, le=100)
    deadline_ms: int = Field(default=30_000, ge=100, le=300_000)


class DecisionRequest(StrictModel):
    schema_version: Literal["decision-v1"] = "decision-v1"
    request_id: UUID
    task_id: UUID
    understanding_revision: int = Field(ge=1)
    route: Literal["simple_call", "plan_execute"]
    intent: IntentSpec
    goals: tuple[str, ...] = ()
    constraints: dict[str, Any] = Field(default_factory=dict)
    state_ref: StateRef = Field(default_factory=StateRef)
    required_evidence: tuple[str, ...] = ()
    budget: Budget = Field(default_factory=Budget)


class ToolCall(StrictModel):
    tool_id: str = Field(min_length=1, max_length=120)
    operation: str = Field(min_length=1, max_length=120)
    call_id: UUID = Field(default_factory=uuid4)
    input_hash: str = Field(min_length=16, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=180)
    side_effect: Literal["none"] = "none"
    provider_version: str = Field(default="unknown", min_length=1, max_length=80)


class ExecutionStep(StrictModel):
    step_id: str = Field(min_length=1, max_length=80)
    operation: str = Field(min_length=1, max_length=120)
    depends_on: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()


class ExecutionPlan(StrictModel):
    plan_revision: int = Field(default=1, ge=1)
    steps: tuple[ExecutionStep, ...] = Field(min_length=1, max_length=50)
    status: Literal["planned", "running", "completed", "partial"] = "planned"


class StepResult(StrictModel):
    step_id: str = Field(min_length=1, max_length=80)
    status: Literal["ok", "blocked", "failed", "skipped"]
    input_hash: str = Field(min_length=16, max_length=128)
    output_hash: str | None = Field(default=None, max_length=128)
    state_revision: int | None = Field(default=None, ge=0)
    provenance: tuple[Provenance, ...] = ()
    error_code: str | None = None


class DecisionResult(StrictModel):
    schema_version: Literal["decision-v1"] = "decision-v1"
    task_id: UUID
    status: DecisionStatus
    result_kind: Literal["calculation", "recommendation", "chat", "state"]
    solution: dict[str, Any] = Field(default_factory=dict)
    supported_claims: tuple[dict[str, Any], ...] = ()
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    state_ref: StateRef = Field(default_factory=StateRef)
    plan_revision: int | None = Field(default=None, ge=1)
    tool_calls: tuple[ToolCall, ...] = ()
    step_results: tuple[StepResult, ...] = ()


class AnswerSpec(StrictModel):
    schema_version: Literal["generation-v1"] = "generation-v1"
    answer_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    kind: Literal["chat", "calculation", "advice", "state", "clarification"]
    status: DecisionStatus
    protected_payload: dict[str, Any] = Field(default_factory=dict)
    required_blocks: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    provenance: dict[str, Any] = Field(default_factory=dict)


class TraceContext(StrictModel):
    trace_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    request_id: UUID
    input_revision: int = Field(default=1, ge=1)
    understanding_revision: int | None = Field(default=None, ge=1)
    decision_revision: int | None = Field(default=None, ge=1)


class SpanEvent(StrictModel):
    trace_id: UUID
    span_id: UUID = Field(default_factory=uuid4)
    parent_span_id: UUID | None = None
    task_id: UUID
    request_id: UUID
    input_revision: int = Field(default=1, ge=1)
    understanding_revision: int | None = Field(default=None, ge=1)
    decision_revision: int | None = Field(default=None, ge=1)
    layer: Literal["input", "understanding", "knowledge", "decision", "generation", "evaluation"]
    component: str = Field(min_length=1, max_length=120)
    event: str = Field(min_length=1, max_length=120)
    status: Literal["started", "ok", "blocked", "failed"]
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    references: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None


class MetricPoint(StrictModel):
    trace_id: UUID | None = None
    name: str = Field(min_length=1, max_length=120)
    value: float
    unit: str = Field(min_length=1, max_length=40)
    labels: dict[str, str] = Field(default_factory=dict)


class EvalCase(StrictModel):
    case_id: str = Field(min_length=1, max_length=120)
    dataset_version: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=6000)
    expected_route: RouteKind
    rubric: dict[str, Any] = Field(default_factory=dict)


class EvalRun(StrictModel):
    run_id: UUID = Field(default_factory=uuid4)
    dataset_version: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    case_ids: tuple[str, ...] = ()
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class BadcaseRecord(StrictModel):
    case_id: str = Field(min_length=1, max_length=120)
    trace_id: UUID | None = None
    category: str = Field(min_length=1, max_length=120)
    severity: Literal["low", "medium", "high", "critical"]
    symptom: str = Field(min_length=1, max_length=500)
    expected: dict[str, Any] = Field(default_factory=dict)
    observed_refs: tuple[str, ...] = ()
    status: Literal["open", "fixed_pending_release", "closed"] = "open"


class EvalReport(StrictModel):
    run_id: UUID
    dataset_version: str
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    metrics: tuple[MetricPoint, ...] = ()
    badcases: tuple[BadcaseRecord, ...] = ()


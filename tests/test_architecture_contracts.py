from uuid import uuid4

import pytest
from pydantic import ValidationError

from rust_coach.architecture.contracts import (
    AnswerSpec,
    CoachTaskRepresentation,
    DecisionRequest,
    DecisionResult,
    IntentSpec,
    ParsedInput,
    RequestCandidate,
    SpanEvent,
    TraceContext,
)
from rust_coach.architecture.decision import BudgetLedger
from rust_coach.contracts import AppError


def test_shared_contracts_preserve_task_lifecycle():
    request_id = uuid4()
    task_id = uuid4()
    parsed = ParsedInput(
        request_id=request_id,
        task_id=task_id,
        prompt="合成一个 C4 还差什么",
        request_candidates=(RequestCandidate(
            kind="calculation.craft_gap",
            operation="craft",
            target="explosive.timed",
            desired_outcome="精确计算材料缺口",
            text="合成一个 C4 还差什么",
            source_ref="text:0",
        ),),
    )
    task = CoachTaskRepresentation(
        task_id=task_id,
        input_revision=parsed.input_revision,
        intent=IntentSpec(kind="calculation.craft_gap", target="explosive.timed",
                          desired_outcome="精确计算材料缺口", mode="exact", confidence=1),
        route="simple_call",
    )
    decision = DecisionRequest(request_id=request_id, task_id=task_id,
                               understanding_revision=task.understanding_revision,
                               route="simple_call", intent=task.intent)
    result = DecisionResult(task_id=task_id, status="verified", result_kind="calculation",
                            solution={"calculation_id": "calc-1"})
    answer = AnswerSpec(task_id=task_id, kind="calculation", status=result.status,
                        protected_payload=result.solution)
    assert parsed.task_id == task.task_id == decision.task_id == result.task_id == answer.task_id


def test_parsed_input_rejects_empty_input():
    with pytest.raises(ValidationError):
        ParsedInput(request_id=uuid4(), task_id=uuid4())


def test_trace_event_has_cross_layer_identity():
    context = TraceContext(task_id=uuid4(), request_id=uuid4())
    event = SpanEvent(trace_id=context.trace_id, task_id=context.task_id,
                      request_id=context.request_id, layer="input",
                      component="ingress", event="request.received", status="ok")
    assert event.trace_id == context.trace_id
    assert event.task_id == context.task_id


def test_budget_ledger_blocks_over_budget_execution():
    request = DecisionRequest(
        request_id=uuid4(), task_id=uuid4(), understanding_revision=1,
        route="simple_call", intent=IntentSpec(kind="calculation.craft_gap",
        desired_outcome="计算", mode="exact", confidence=1),
    )
    request.budget.max_tool_calls = 1
    ledger = BudgetLedger(request)
    ledger.consume(step=True, tool_call=True)
    with pytest.raises(AppError) as error:
        ledger.consume(tool_call=True)
    assert error.value.code == "decision_budget_exceeded"

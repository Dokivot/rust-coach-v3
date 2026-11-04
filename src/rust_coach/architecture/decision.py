from __future__ import annotations

import hashlib
import json
from typing import Awaitable, Callable
from uuid import uuid4

from rust_coach.architecture.contracts import DecisionRequest, DecisionResult, ExecutionPlan, ExecutionStep, Provenance, StepResult, ToolCall
from rust_coach.contracts import AppError


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}

    def register(self, tool_id: str, operation: str, *, provider_version: str, side_effect: str = "none"):
        if side_effect != "none":
            raise ValueError("Demo decision tools must be side-effect free")
        self._tools[operation] = {"tool_id": tool_id, "provider_version": provider_version, "side_effect": side_effect}

    def get(self, operation: str) -> dict:
        try:
            return self._tools[operation]
        except KeyError as error:
            raise ValueError(f"unknown_tool:{operation}") from error


class BudgetLedger:
    def __init__(self, request: DecisionRequest):
        self.max_steps = request.budget.max_steps
        self.max_tool_calls = request.budget.max_tool_calls
        self.steps = 0
        self.tool_calls = 0

    def consume(self, *, step: bool = False, tool_call: bool = False):
        next_steps = self.steps + int(step)
        next_calls = self.tool_calls + int(tool_call)
        if next_steps > self.max_steps or next_calls > self.max_tool_calls:
            raise AppError("decision_budget_exceeded", "决策任务达到步骤或工具调用预算。", 422)
        self.steps, self.tool_calls = next_steps, next_calls


class DecisionLayer:
    """Controlled adapters around the existing deterministic and advice services."""

    def __init__(self, registry: ToolRegistry | None = None):
        self.registry = registry or ToolRegistry()
        self.registry.register("calculator", "calculation", provider_version="calculator-v1")
        self.registry.register("advice-agent", "recommendation", provider_version="advice-v1")

    def _tool(self, request: DecisionRequest, operation: str, payload: dict) -> ToolCall:
        descriptor = self.registry.get(operation)
        input_hash = _hash(payload)
        return ToolCall(tool_id=descriptor["tool_id"], operation=operation,
                        input_hash=input_hash, idempotency_key=f"{request.task_id}:{input_hash}",
                        provider_version=descriptor["provider_version"])

    async def simple_call(self, request: DecisionRequest, invoke: Callable[[], Awaitable[dict]]):
        """Execute one registered, side-effect-free tool and validate its result."""
        if request.route != "simple_call":
            raise ValueError("simple_call_requires_simple_route")
        BudgetLedger(request).consume(step=True, tool_call=True)
        output = await invoke()
        if not isinstance(output, dict):
            raise ValueError("tool_result_must_be_object")
        return output, self.from_calculation(request, output.get("calculation") or output)

    async def plan_execute(self, request: DecisionRequest, invoke: Callable[[], Awaitable[dict]]):
        """Run the bounded advice graph as the decision layer's plan/execute adapter."""
        if request.route != "plan_execute":
            raise ValueError("plan_execute_requires_plan_route")
        BudgetLedger(request).consume(step=True, tool_call=True)
        # This plan is deliberately local to the decision layer. It is never
        # copied into DecisionResult or the generation context.
        ExecutionPlan(steps=(ExecutionStep(step_id="advice-agent", operation="recommendation",
                                           success_criteria=("validated recommendation",)),), status="running")
        output = await invoke()
        if not isinstance(output, dict):
            raise ValueError("agent_result_must_be_object")
        return output, self.from_advice(request, output)

    def from_calculation(self, request: DecisionRequest, output: dict) -> DecisionResult:
        status = "verified" if output.get("status") == "calculated" else "needs_input"
        usage = output.get("input_usage") or {}
        if status == "verified" and (usage.get("inventory_source") == "image_inferred" or
                                     (output.get("input") or {}).get("inventory") is None or
                                     not (output.get("input") or {}).get("inventory_complete", True)):
            status = "conditional"
        tool = self._tool(request, "calculation", output.get("input") or output)
        step = StepResult(step_id="calculation", status="ok" if status in {"verified", "conditional"} else "blocked",
                          input_hash=tool.input_hash, output_hash=_hash(output),
                          state_revision=request.state_ref.revision,
                          provenance=(Provenance(source_ref=output.get("calculation_id", "calculation"), source_type="tool"),))
        return DecisionResult(task_id=request.task_id, status=status, result_kind="calculation",
                              solution={"calculation": output},
                              supported_claims=({"calculation_id": output.get("calculation_id")},) if output.get("calculation_id") else (),
                              limitations=("计算结果只适用于当前规则与输入快照",),
                              state_ref=request.state_ref, tool_calls=(tool,), step_results=(step,))

    def from_advice(self, request: DecisionRequest, output: dict) -> DecisionResult:
        advice = output.get("advice") or {}
        raw_status = advice.get("status", "conditional")
        status = raw_status if raw_status in {"verified", "conditional", "partial", "blocked", "unavailable", "needs_input"} else "conditional"
        tool = self._tool(request, "recommendation", {"request": advice.get("request"), "plan": advice.get("plan")})
        step_status = "ok" if advice.get("answer") else "blocked"
        step = StepResult(step_id="advice-agent", status=step_status, input_hash=tool.input_hash,
                          output_hash=_hash(advice), state_revision=request.state_ref.revision,
                          provenance=(Provenance(source_ref="advice-v1", source_type="tool"),))
        answer = advice.get("answer") or {}
        evaluations = {
            key: {"status": value.get("status"), "missing_fields": value.get("missing_fields", [])}
            for key, value in (advice.get("evaluations") or {}).items()
        }
        public_solution = {
            "recommendation": answer.get("recommendation"),
            "alternative": answer.get("alternative"),
            "missing": answer.get("missing", []),
            "uncertainty": answer.get("uncertainty", ""),
            "evaluations": evaluations,
        }
        if advice.get("mode") == "onboarding":
            public_solution = {"mode": "onboarding", "guide": advice.get("answer"),
                               "evidence_review": advice.get("review")}
        return DecisionResult(task_id=request.task_id, status=status, result_kind="recommendation",
                              solution=public_solution, evidence_refs=tuple(
                                  item.get("id", "") for item in (advice.get("evidence") or {}).get("documents", []) if item.get("id")
                              ), state_ref=request.state_ref, plan_revision=1, tool_calls=(tool,), step_results=(step,))


from __future__ import annotations

from rust_coach.architecture.contracts import (
    CoachTaskRepresentation,
    DecisionRequest,
    IntentSpec,
    ParsedInput,
    StateRef,
    Uncertainty,
)


class UnderstandingLayer:
    """Resolve the structured input into a route and a decision request."""

    def interpret(self, parsed: ParsedInput, *, snapshot: dict | None = None, capabilities=()):
        candidate = parsed.request_candidates[0]
        kind = candidate.kind
        if kind == "state.query":
            route = "state"
            mode = "conversational"
        elif kind == "calculation.request":
            route = "simple_call"
            mode = "exact"
        elif kind == "decision.recommendation":
            route = "plan_execute"
            mode = "advisory"
        else:
            route = "chat"
            mode = "conversational"
        uncertainty = Uncertainty(
            semantic="resolved",
            factual="partial" if parsed.unknowns else "known",
            coverage="partial" if parsed.unknowns else "full",
            freshness="known" if snapshot else "unknown",
            capability="known" if not capabilities or route in capabilities or route in {"chat", "state"} else "unknown",
        )
        state_ref = StateRef(
            scope_id=(snapshot or {}).get("scope", {}).get("id") if snapshot else None,
            revision=(snapshot or {}).get("revision") if snapshot else None,
        )
        task = CoachTaskRepresentation(
            task_id=parsed.task_id,
            input_revision=parsed.input_revision,
            intent=IntentSpec(
                kind=kind,
                target=candidate.target,
                desired_outcome=candidate.desired_outcome or candidate.text,
                mode=mode,
                confidence=1.0 if kind in {"state.query", "conversation.general"} else 0.8,
            ),
            goals=(candidate.desired_outcome or candidate.text,),
            state_ref=state_ref,
            unknowns=parsed.unknowns,
            uncertainty=uncertainty,
            route=route,
            required_evidence=("player_state",) if route == "plan_execute" else (),
            provenance=parsed.provenance,
        )
        if task.route in {"simple_call", "plan_execute"}:
            decision = DecisionRequest(
                request_id=parsed.request_id,
                task_id=parsed.task_id,
                understanding_revision=task.understanding_revision,
                route=task.route,
                intent=task.intent,
                goals=task.goals,
                state_ref=task.state_ref,
                required_evidence=task.required_evidence,
            )
        else:
            decision = None
        return task, decision


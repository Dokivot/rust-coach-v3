from rust_coach.architecture.contracts import AnswerSpec, DecisionResult


def answer_spec_from_decision(result: DecisionResult) -> AnswerSpec:
    kind = {"recommendation": "advice", "calculation": "calculation", "state": "state", "chat": "chat"}[result.result_kind]
    return AnswerSpec(task_id=result.task_id, kind=kind, status=result.status,
                      protected_payload=result.solution, evidence_refs=result.evidence_refs,
                      limitations=result.limitations,
                      provenance={"schema_version": result.schema_version, "plan_revision": result.plan_revision})


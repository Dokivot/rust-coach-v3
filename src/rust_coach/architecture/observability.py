from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from rust_coach.architecture.contracts import SpanEvent, TraceContext


def new_trace(request_id: UUID, task_id: UUID) -> TraceContext:
    return TraceContext(request_id=request_id, task_id=task_id)


def emit_trace(
    trace: dict | None,
    *,
    layer: str,
    component: str,
    event: str,
    status: str = "ok",
    references: dict | None = None,
    error_code: str | None = None,
) -> dict:
    """Append a redacted, serializable event to the task trace."""
    result = trace or {"context": {}, "events": []}
    context = result.get("context") or {}
    item = SpanEvent(
        trace_id=context["trace_id"],
        task_id=context["task_id"],
        request_id=context["request_id"],
        input_revision=context.get("input_revision", 1),
        understanding_revision=context.get("understanding_revision"),
        decision_revision=context.get("decision_revision"),
        layer=layer,
        component=component,
        event=event,
        status=status,
        started_at=datetime.now(timezone.utc),
        ended_at=None,
        duration_ms=None,
        references=references or {},
        error_code=error_code,
    )
    result.setdefault("events", []).append(item.model_dump(mode="json"))
    return result


def update_trace_context(trace: dict, **values) -> dict:
    context = dict(trace.get("context") or {})
    context.update(values)
    trace["context"] = context
    return trace


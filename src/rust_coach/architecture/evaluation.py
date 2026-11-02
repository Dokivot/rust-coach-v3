from __future__ import annotations

from rust_coach.architecture.contracts import EvalCase, EvalReport, EvalRun, MetricPoint


def evaluate_route(case: EvalCase, actual_route: str, *, trace_id=None) -> tuple[MetricPoint, bool]:
    passed = case.expected_route == actual_route
    return MetricPoint(trace_id=trace_id, name="route_accuracy", value=1.0 if passed else 0.0,
                       unit="ratio", labels={"expected": case.expected_route, "actual": actual_route}), passed


def report_run(run: EvalRun, points: list[MetricPoint], passed: int, failed: int) -> EvalReport:
    return EvalReport(run_id=run.run_id, dataset_version=run.dataset_version,
                      total=passed + failed, passed=passed, failed=failed, metrics=tuple(points))


import argparse
import asyncio
import copy
import hashlib
import io
import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw

from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.llm import CompatibleChatModel
from rust_coach.contracts import AppError

try:
    from .inventory_grading import GRADER_VERSION, independently_calculated_materials, score_case
    from .inventory_evidence import apply_reviews, grader_manifest, source_manifest, summarize, validate_dataset
except ImportError:
    from inventory_grading import GRADER_VERSION, independently_calculated_materials, score_case
    from inventory_evidence import apply_reviews, grader_manifest, source_manifest, summarize, validate_dataset


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals/inventory-baseline.json"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def evaluation_configuration(settings):
    vision = settings.for_vision()
    return {
        "schema": "evaluation-configuration-v2",
        "text_model": settings.model,
        "vision_model": vision.model,
        "text_endpoint_sha256": digest(settings.base_url.encode()),
        "vision_endpoint_sha256": digest(vision.base_url.encode()),
        "api_mode": settings.api_mode,
        "vision_api_mode": vision.api_mode,
        "instruction_role": settings.instruction_role,
        "output_token_parameter": settings.output_token_parameter,
        "max_output_tokens": settings.max_output_tokens,
        "vision_max_output_tokens": vision.max_output_tokens,
        "vision_enabled": settings.vision_enabled,
        "vision_detail": settings.vision_detail,
        "history_turns": settings.history_turns,
        "context_char_limit": settings.context_char_limit,
        "memory_context_char_limit": settings.memory_context_char_limit,
        "search_provider": settings.search_provider,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "advice_timeout_seconds": settings.advice_timeout_seconds,
        "memory_timeout_seconds": settings.memory_timeout_seconds,
        "attachment_max_bytes": settings.attachment_max_bytes,
        "attachment_max_pixels": settings.attachment_max_pixels,
        "visual_draft_hours": settings.visual_draft_hours,
    }


def validate_captured_runs(report, dataset):
    runs = report.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("captured_report_requires_runs")
    cases = {case["id"]: case for case in dataset["cases"]}
    keys = []
    for record in runs:
        case_id = record.get("case_id")
        repeat = record.get("repeat")
        if case_id not in cases or not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1:
            raise ValueError("invalid_captured_run_identity")
        if record.get("expected") != cases[case_id]:
            raise ValueError("captured_run_case_mismatch")
        if not isinstance(record.get("steps"), list) or not isinstance(record.get("model_calls"), list):
            raise ValueError("invalid_captured_run_artifacts")
        keys.append((case_id, repeat))
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate_captured_run")
    selection = report.get("selection")
    if selection:
        if not isinstance(selection, dict):
            raise ValueError("captured_run_manifest_mismatch")
        selected = selection.get("case_ids")
        repeat = selection.get("repeat")
        if (not isinstance(selected, list) or not selected or any(case_id not in cases for case_id in selected)
                or not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1):
            raise ValueError("captured_run_manifest_mismatch")
        expected = [(case_id, iteration) for iteration in range(1, repeat + 1) for case_id in selected]
        if selection.get("planned_runs") != len(expected) or keys != expected:
            raise ValueError("captured_run_manifest_mismatch")


def save_report(destination, report):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def fixture_bytes(dataset, cases_path, variant):
    if "images" in dataset:
        return (cases_path.parent / dataset["images"][variant]["path"]).read_bytes()
    raw = (cases_path.parent / dataset["image"]).read_bytes()
    if variant == "original":
        return raw
    if variant != "occluded":
        raise ValueError("unknown_fixture_variant")
    with Image.open(io.BytesIO(raw)) as source:
        canvas = source.convert("RGB")
    ImageDraw.Draw(canvas).rectangle(dataset["occlusion"]["box"], fill=tuple(dataset["occlusion"]["fill"]))
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


def safe_messages(messages):
    result = []
    for message in messages:
        content = message.content
        if isinstance(content, list):
            content = [
                {"type": "image", "data_url_sha256": digest(part.data_url.encode()), "detail": part.detail}
                if getattr(part, "type", None) == "image" else part.model_dump(mode="json")
                for part in content
            ]
        result.append({"role": message.role, "content": content})
    return result


class RunBudget:
    def __init__(self, max_calls, max_seconds):
        if max_calls < 0 or max_seconds <= 0:
            raise ValueError("invalid_evaluation_budget")
        self.max_calls = max_calls
        self.deadline = monotonic() + max_seconds
        self.calls = 0
        self.stop_reason = None

    def remaining(self):
        return max(0, self.deadline - monotonic())

    def reserve(self):
        if self.calls >= self.max_calls or self.remaining() <= 0:
            self.stop_reason = "model_call_budget" if self.calls >= self.max_calls else "wall_time_budget"
            raise AppError("evaluation_budget_exhausted", self.stop_reason, 503)
        self.calls += 1


class RecordingModel:
    def __init__(self, wrapped, role, record, persist, budget=None):
        self.wrapped, self.role, self.record, self.persist = wrapped, role, record, persist
        self.budget = budget
        self.step = -1

    async def complete(self, messages):
        if self.budget:
            self.budget.reserve()
        started = monotonic()
        call = {"role": self.role, "step": self.step, "status": "running", "messages": safe_messages(messages)}
        self.record["model_calls"].append(call)
        self.persist()
        try:
            reply = await self.wrapped.complete(messages)
            call.update(status="completed", response=reply.model_dump(mode="json"))
            return reply
        except BaseException as error:
            call.update(status="failed", error_code=getattr(error, "code", type(error).__name__))
            raise
        finally:
            call["elapsed_seconds"] = round(monotonic() - started, 3)
            self.persist()




def expire_observation(app, conversation_id, minutes):
    store = app.state.runtime.store
    turns = store.get(conversation_id).turns
    visual = turns[-1].visual or {}
    if not visual.get("draft_id"):
        raise ValueError("expiration_fixture_requires_visual_draft")
    draft = app.state.vision.drafts.get(conversation_id, visual["draft_id"])
    observed_at = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    previous = copy.deepcopy(draft["payload"].get("resolved_context", {}))
    for context in draft["payload"]["resolved_context"].values():
        context["observed_at"] = observed_at
    with store.connection() as connection:
        connection.execute("UPDATE visual_drafts SET payload=? WHERE id=?", (json.dumps(draft["payload"]), draft["id"]))
    return {"type": "age_observation", "minutes": minutes, "before": previous,
            "after": draft["payload"]["resolved_context"], "scope": "isolated evaluation database only"}


async def run_case(dataset, case, args, settings, transport_client, record, persist, budget=None):
    with tempfile.TemporaryDirectory(prefix="rust-coach-baseline-") as directory:
        scoped = settings.model_copy(update={"database_path": Path(directory) / "case.sqlite3"})
        adapters = {}
        for role in ("text", "vision", "memory"):
            configured = scoped.for_vision() if role == "vision" else scoped
            adapters[role] = RecordingModel(CompatibleChatModel(configured, transport_client, stream_responses=True), role, record, persist, budget)
        app = create_app(scoped, model=adapters["text"], vision_model=adapters["vision"], memory_model=adapters["memory"])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=None) as client:
                created = await client.post("/api/conversations", json={})
                created.raise_for_status()
                conversation_id = created.json()["id"]
                record["conversation_id"] = conversation_id
                if any(turn.get("image") for turn in case["turns"]):
                    probe = await client.post("/api/vision/probe", json={})
                    record["probe"] = probe.json()
                    if probe.status_code != 200:
                        record["steps"].append({"error": {"phase": "vision_probe", "response": probe.json()}})
                        return
                for step_index, specification in enumerate(case["turns"]):
                    for adapter in adapters.values():
                        adapter.step = step_index
                    step = {"input": specification, "events": [], "status": "running"}
                    record["steps"].append(step)
                    persist()
                    try:
                        if specification.get("age_observation_minutes"):
                            step["fixture_change"] = expire_observation(app, conversation_id, specification["age_observation_minutes"])
                        payload = {"request_id": str(uuid4()), "message": specification["message"]}
                        if specification.get("image"):
                            raw = fixture_bytes(dataset, args.cases, specification["image"])
                            step["fixture_sha256"] = digest(raw)
                            uploaded = await client.post(f"/api/conversations/{conversation_id}/attachments", files={"file": ("inventory.png", raw, "image/png")})
                            uploaded.raise_for_status()
                            step["attachment"] = uploaded.json()
                            payload["attachment_ids"] = [uploaded.json()["id"]]
                        step["request"] = payload
                        started = monotonic()
                        response = await client.post(f"/api/conversations/{conversation_id}/messages", json=payload, headers={"Accept": "text/event-stream"})
                        step["elapsed_seconds"] = round(monotonic() - started, 3)
                        response.raise_for_status()
                        for line in response.text.splitlines():
                            if line.startswith("data: "):
                                event = json.loads(line[6:])
                                if event["type"] == "result":
                                    step["turn"] = event["turn"]
                                elif event["type"] == "error":
                                    step["error"] = event["error"]
                                else:
                                    step["events"].append(event)
                        if "turn" not in step and "error" not in step:
                            step["error"] = {"code": "missing_terminal_event"}
                        step["status"] = "failed" if step.get("error") else "completed"
                    except asyncio.CancelledError:
                        step.update(status="cancelled", error={"code": "evaluation_cancelled"})
                        persist()
                        raise
                    except Exception as error:
                        step.update(status="failed", error={"code": getattr(error, "code", type(error).__name__)})
                    persist()
                    if step.get("error"):
                        break
                record["saved_turn_count"] = len(app.state.runtime.store.get(conversation_id).turns)


def portable_report(report):
    portable = copy.deepcopy(report)
    for record in portable["runs"]:
        for call in record["model_calls"]:
            call.pop("messages", None)
        for step in record["steps"]:
            turn = step.get("turn") or {}
            if turn.get("presentation"):
                architecture = turn["presentation"].get("architecture") or {}
                turn["presentation"] = {"architecture": architecture,
                                        "checks": turn["presentation"].get("checks"),
                                        "fallback_reason": turn["presentation"].get("fallback_reason")}
    return portable


async def live(args, dataset):
    validate_dataset(dataset, args.cases)
    settings = Settings()
    if not settings.configured:
        raise SystemExit("Configured model credentials are required for --live.")
    cases = [case for case in dataset["cases"] if not args.only or case["id"] in args.only]
    if not cases or args.only and set(args.only) - {case["id"] for case in cases}:
        raise SystemExit("Unknown case selection")
    variants = sorted({turn["image"] for case in cases for turn in case["turns"] if turn.get("image")})
    report = {"mode": "live", "started_at": datetime.now(timezone.utc).isoformat(),
              "dataset": dataset, "dataset_sha256": digest(args.cases.read_bytes()),
              "grader_version": GRADER_VERSION, "grader_sha256": grader_manifest(),
              "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "business_source_sha256": digest(b"".join(source.read_bytes() for source in sorted((ROOT / "src/rust_coach").rglob("*.py")))),
              "rules_sha256": digest((ROOT / "src/rust_coach/calculations/rules.json").read_bytes()),
              "source_manifest": source_manifest(ROOT),
              "fixture_sha256": {variant: digest(fixture_bytes(dataset, args.cases, variant)) for variant in variants},
              "configuration": evaluation_configuration(settings),
              "transport": "existing ASGI HTTP upload and SSE chat endpoints, no external browser",
              "budget": {"max_model_calls": args.max_model_calls, "max_seconds": args.max_seconds},
              "selection": {"case_ids": [case["id"] for case in cases], "repeat": args.repeat,
                            "planned_runs": len(cases) * args.repeat},
              "runs": [{"case_id": case["id"], "repeat": repeat + 1, "expected": case, "steps": [], "model_calls": [],
                        "status": "not_run", "execution_status": "not_run"} for repeat in range(args.repeat) for case in cases], "status": "running"}

    def persist():
        save_report(args.output, report)
        if args.portable_output:
            save_report(args.portable_output, portable_report(report))

    persist()
    budget = RunBudget(args.max_model_calls, args.max_seconds)
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout_seconds, connect=10), follow_redirects=False) as transport_client:
        for record in report["runs"]:
            case = record["expected"]
            if budget.stop_reason or budget.remaining() <= 0:
                budget.stop_reason = budget.stop_reason or "wall_time_budget"
                record["not_run_reason"] = budget.stop_reason
                record["assessment"] = score_case(dataset, case, record)
                continue
            record.update(status="running", execution_status="running")
            print(json.dumps({"event": "case_started", "case": case["id"], "repeat": record["repeat"]}), flush=True)
            try:
                await asyncio.wait_for(run_case(dataset, case, args, settings, transport_client, record, persist, budget), timeout=budget.remaining())
            except TimeoutError:
                budget.stop_reason = "wall_time_budget"
                record["steps"].append({"error": {"code": "wall_time_budget"}})
            except Exception as error:
                record["steps"].append({"error": {"phase": "setup", "code": type(error).__name__}})
            if budget.stop_reason:
                record["stop_reason"] = budget.stop_reason
            record["assessment"] = score_case(dataset, case, record)
            record["execution_status"] = "stopped_budget" if budget.stop_reason else record["assessment"]["execution_status"]
            record["assessment"]["execution_status"] = record["execution_status"]
            if budget.stop_reason:
                record["assessment"]["status"] = "fail"
                record["stop_reason"] = budget.stop_reason
            record["status"] = "finished"
            persist()
            print(json.dumps({"event": "case_finished", "case": case["id"], "repeat": record["repeat"],
                              "assessment": record["assessment"]["status"], "first_error": record["assessment"]["first_error_check"]}), flush=True)
    report.update(status="stopped_budget" if budget.stop_reason else "completed", completed_at=datetime.now(timezone.utc).isoformat())
    report["budget"].update(actual_model_calls=budget.calls, stop_reason=budget.stop_reason)
    report["summary"] = summarize(report)
    persist()
    if args.portable_output:
        save_report(args.portable_output, portable_report(report))


def replay(args, dataset):
    report = json.loads(args.replay.read_text(encoding="utf-8"))
    if report["dataset_sha256"] != digest(args.cases.read_bytes()):
        raise SystemExit("Dataset differs from captured run; do not silently rescore against another oracle.")
    if report.get("dataset") != dataset:
        raise SystemExit("Embedded dataset differs from captured dataset hash.")
    try:
        validate_captured_runs(report, dataset)
    except ValueError as error:
        raise SystemExit(f"Captured report is incomplete or inconsistent: {error}.") from error
    cases = {case["id"]: case for case in dataset["cases"]}
    replayed = copy.deepcopy(report)
    replayed.update(mode="saved_output_rescore", source=str(args.replay), source_sha256=digest(args.replay.read_bytes()),
                    model_calls=0, grader_version=GRADER_VERSION, grader_sha256=grader_manifest(),
                    limitation="Rechecks saved outputs only; captured calls are historical, no application or model execution.")
    replayed.pop("review", None)
    if "fixture_sha256" not in replayed:
        replayed["fixture_sha256"] = {"original": report.get("image_sha256"), "occluded": report.get("occluded_sha256")}
        replayed["provenance_limitation"] = "Legacy capture lacks per-file source manifest and endpoint fingerprints; retained as unknown."
    if "selection" not in replayed:
        limitation = "Legacy capture lacks an explicit planned-run manifest; captured runs were validated for identity and uniqueness only."
        replayed["provenance_limitation"] = "; ".join(filter(None, (replayed.get("provenance_limitation"), limitation)))
    for record in replayed["runs"]:
        original = record.get("assessment")
        record["assessment"] = score_case(dataset, cases[record["case_id"]], record)
        record["previous_assessment"] = original
        record["matches_recorded_assessment"] = record["assessment"] == original
    if args.review:
        replayed = apply_reviews(replayed, json.loads(args.review.read_text()), replayed["source_sha256"])
    replayed["summary"] = summarize(replayed)
    replayed["summary"]["new_model_calls"] = 0
    save_report(args.output, replayed)
    print(json.dumps({"mode": replayed["mode"], "runs": len(replayed["runs"]), "all_match": all(row["matches_recorded_assessment"] for row in replayed["runs"])}))


def main():
    parser = argparse.ArgumentParser(description="Eight-case diagnostic baseline. Business code remains unchanged.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Call configured external models; sends the fixture image and incurs usage.")
    mode.add_argument("--replay", type=Path, help="Rescore saved outputs without network or application execution.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--portable-output", type=Path)
    parser.add_argument("--repeat", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--review", type=Path, help="Versioned per-turn review overlay, applied only during rescore.")
    parser.add_argument("--max-model-calls", type=int, default=48)
    parser.add_argument("--max-seconds", type=float, default=900)
    args = parser.parse_args()
    if args.output.exists() or args.portable_output and args.portable_output.exists():
        parser.error("Use a new output path to preserve previous runs.")
    if args.portable_output and args.output.resolve() == args.portable_output.resolve():
        parser.error("Raw and portable reports must use different output paths.")
    if args.replay and (args.portable_output or args.only):
        parser.error("--portable-output and --only apply to live execution only.")
    if args.max_model_calls < 0 or args.max_seconds <= 0:
        parser.error("Evaluation budgets must be nonnegative calls and positive seconds.")
    dataset = json.loads(args.cases.read_text(encoding="utf-8"))
    validate_dataset(dataset, args.cases)
    if args.live and args.review:
        parser.error("Reviews must bind to an existing captured report; use --replay.")
    if args.live:
        asyncio.run(live(args, dataset))
    else:
        replay(args, dataset)


if __name__ == "__main__":
    main()

import argparse
import asyncio
import copy
import hashlib
import io
import json
import re
import subprocess
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw

from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.llm import CompatibleChatModel


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals/inventory-baseline.json"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save_report(destination, report):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def fixture_bytes(dataset, cases_path, variant):
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


class RecordingModel:
    def __init__(self, wrapped, role, record, persist):
        self.wrapped, self.role, self.record, self.persist = wrapped, role, record, persist
        self.step = -1

    async def complete(self, messages):
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


def independently_calculated_materials(dataset, inventory):
    totals = {item: amount * 10 for item, amount in dataset["oracle"]["per_c4"].items()}
    used = {item: min(amount, inventory.get(item, 0)) for item, amount in totals.items() if inventory.get(item, 0)}
    missing = {item: amount - used.get(item, 0) for item, amount in totals.items() if amount > used.get(item, 0)}
    return missing, used


def score_case(dataset, case, record):
    checks = []

    def check(name, stage, passed, expected, actual, provisional=False):
        status = "pending" if provisional else "pass" if passed else "fail"
        checks.append({"name": name, "stage": stage, "status": status,
                       "expected": expected, "actual": actual,
                       "comparison_matches": bool(passed)})

    turns = [step.get("turn") for step in record["steps"]]
    for step_index, step in enumerate(record["steps"]):
        if step.get("error"):
            check("response_received", "external_service", False, "successful response", step["error"])
    if len(turns) != len(case["turns"]) or not all(turns):
        if not checks:
            check("all_turns_executed", "execution", False, len(case["turns"]), len(turns))
        return finalize(checks)
    expected_kind = case["expected"]
    for step_index, turn in enumerate(turns):
        visual = turn.get("visual") or {}
        calculation = turn.get("calculation") or {}
        request = calculation.get("input") or {}
        result = calculation.get("result") or {}
        is_main = step_index == len(turns) - 1
        needs_image = expected_kind in {"craft_image", "craft_occluded", "switch"} and step_index == 0
        if needs_image:
            expected_inventory = {slot["item_id"]: slot["quantity"] for slot in dataset["annotation"]["slots"]}
            if expected_kind == "craft_occluded":
                expected_inventory.pop(dataset["occlusion"]["excluded_item"], None)
            observed = Counter()
            for observation in visual.get("observations", []):
                if observation.get("item_id") and observation.get("quantity") is not None:
                    observed[observation["item_id"]] += observation["quantity"]
            check("identified_items_and_counts", "vision", dict(observed) == expected_inventory,
                  expected_inventory, dict(observed), dataset["annotation"]["status"] != "human_confirmed")
            check("partial_inventory", "state", not request.get("inventory_complete", True), False, request.get("inventory_complete"))
            check("inventory_admitted", "state", request.get("inventory") == expected_inventory,
                  expected_inventory, request.get("inventory"))
        if expected_kind in {"craft_zero", "craft_image", "craft_occluded"} or expected_kind == "switch" and step_index == 0:
            check("craft_route", "intent", calculation.get("operation") == "craft", "craft", calculation.get("operation"))
            check("craft_goal", "parameters", request.get("goals") == {"explosive.timed": 10}, {"explosive.timed": 10}, request.get("goals"))
            inventory = {} if expected_kind == "craft_zero" else expected_inventory
            check("zero_or_image_inventory", "state", request.get("inventory") == inventory, inventory, request.get("inventory"))
            missing, used = independently_calculated_materials(dataset, inventory)
            check("recipe_arithmetic", "calculation", result.get("acquisition") == missing, missing, result.get("acquisition"))
            check("inventory_deduction", "calculation", result.get("inventory_used") == used, used, result.get("inventory_used"))
            check("game_version", "rules", False, "independently verified game build", dataset["oracle"]["version_status"], True)
        if expected_kind in {"no_image_stock", "expired"} and is_main:
            check("no_invalid_inventory", "state", request.get("inventory") is None and not result.get("inventory_used"),
                  "no historical/opponent/expired deductions", {"inventory": request.get("inventory"), "used": result.get("inventory_used")})
        if expected_kind == "needs_hp":
            check("damaged_target_not_defaulted", "parameters", calculation.get("status") == "needs_input",
                  "needs_input, no full-health calculation", calculation.get("status"))
        if expected_kind == "switch" and step_index > 0:
            check("raid_route", "intent", calculation.get("operation") == "raid", "raid", calculation.get("operation"))
            check("raid_target", "parameters", request.get("target") == "building:base-wall:stone" and request.get("count") == 1 and not request.get("goals"),
                  "one stone wall; no craft goal", {key: request.get(key) for key in ("target", "count", "goals")})
            expected_hp = None if step_index == 1 else 250
            check("raid_hp_input", "parameters", request.get("hp") == expected_hp, expected_hp, request.get("hp"))
            options = result.get("options", [])
            c4 = next((option for option in options if option.get("item") == "explosive.timed"), {})
            expected_count = dataset["oracle"]["raid"]["expected_counts"]["500" if step_index == 1 else "250"]
            check("raid_rule_independent_verification", "rules", c4.get("count") == expected_count,
                  expected_count, c4.get("count"), True)
            check("comparison_scope", "parameters", result.get("comparison") == "minimum_additional_sulfur_among_single_method_options",
                  "minimum_additional_sulfur_among_single_method_options", result.get("comparison"))
    check("final_expression_review", "expression", False,
          "Review every answer for correct conditions, unsupported certainty, unnecessary refusal or questions", "not automatically judged", True)
    return finalize(checks)


def finalize(checks):
    failures = [check for check in checks if check["status"] == "fail"]
    order = {stage: index for index, stage in enumerate(("execution", "intent", "parameters", "vision", "state", "rules", "calculation", "expression", "external_service"))}
    first = min(failures, key=lambda check: order[check["stage"]]) if failures else None
    return {"status": "fail" if failures else "pending" if any(check["status"] == "pending" for check in checks) else "pass",
            "first_error_stage": first["stage"] if first else None, "first_error_check": first["name"] if first else None,
            "checks": checks, "review": None}


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


async def run_case(dataset, case, args, settings, transport_client, record, persist):
    with tempfile.TemporaryDirectory(prefix="rust-coach-baseline-") as directory:
        scoped = settings.model_copy(update={"database_path": Path(directory) / "case.sqlite3"})
        adapters = {}
        for role in ("text", "vision", "memory"):
            configured = scoped.for_vision() if role == "vision" else scoped
            adapters[role] = RecordingModel(CompatibleChatModel(configured, transport_client, stream_responses=True), role, record, persist)
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
    settings = Settings()
    if not settings.configured:
        raise SystemExit("Configured model credentials are required for --live.")
    cases = [case for case in dataset["cases"] if not args.only or case["id"] in args.only]
    if not cases or args.only and set(args.only) - {case["id"] for case in cases}:
        raise SystemExit("Unknown case selection")
    report = {"mode": "live", "started_at": datetime.now(timezone.utc).isoformat(),
              "dataset": dataset, "dataset_sha256": digest(args.cases.read_bytes()),
              "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "business_source_sha256": digest(b"".join(source.read_bytes() for source in sorted((ROOT / "src/rust_coach").rglob("*.py")))),
              "rules_sha256": digest((ROOT / "src/rust_coach/calculations/rules.json").read_bytes()),
              "image_sha256": digest(fixture_bytes(dataset, args.cases, "original")),
              "occluded_sha256": digest(fixture_bytes(dataset, args.cases, "occluded")),
              "configuration": {"text_model": settings.model, "vision_model": settings.for_vision().model,
                                "api_mode": settings.api_mode, "vision_api_mode": settings.for_vision().api_mode,
                                "search_provider": settings.search_provider, "request_timeout_seconds": settings.request_timeout_seconds,
                                "advice_timeout_seconds": settings.advice_timeout_seconds, "max_output_tokens": settings.max_output_tokens},
              "transport": "existing ASGI HTTP upload and SSE chat endpoints, no external browser",
              "runs": [], "status": "running"}

    def persist():
        save_report(args.output, report)

    persist()
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout_seconds, connect=10), follow_redirects=False) as transport_client:
        for repeat in range(args.repeat):
            for case in cases:
                record = {"case_id": case["id"], "repeat": repeat + 1, "expected": case, "steps": [], "model_calls": [], "status": "running"}
                report["runs"].append(record)
                print(json.dumps({"event": "case_started", "case": case["id"], "repeat": repeat + 1}), flush=True)
                try:
                    await run_case(dataset, case, args, settings, transport_client, record, persist)
                except Exception as error:
                    record["steps"].append({"error": {"phase": "setup", "code": type(error).__name__}})
                record["assessment"] = score_case(dataset, case, record)
                record["status"] = "finished"
                persist()
                print(json.dumps({"event": "case_finished", "case": case["id"], "repeat": repeat + 1,
                                  "assessment": record["assessment"]["status"], "first_error": record["assessment"]["first_error_check"]}), flush=True)
    report.update(status="completed", completed_at=datetime.now(timezone.utc).isoformat())
    persist()
    if args.portable_output:
        save_report(args.portable_output, portable_report(report))


def replay(args, dataset):
    report = json.loads(args.replay.read_text(encoding="utf-8"))
    if report["dataset_sha256"] != digest(args.cases.read_bytes()):
        raise SystemExit("Dataset differs from captured run; do not silently rescore against another oracle.")
    cases = {case["id"]: case for case in dataset["cases"]}
    replayed = {"mode": "saved_output_rescore", "source": str(args.replay), "source_sha256": digest(args.replay.read_bytes()),
                "model_calls": 0, "limitation": "Rechecks saved outputs only; does not run the application or evaluate model stability.", "runs": []}
    for record in report["runs"]:
        assessment = score_case(dataset, cases[record["case_id"]], record)
        replayed["runs"].append({"case_id": record["case_id"], "repeat": record["repeat"], "assessment": assessment,
                                 "matches_recorded_assessment": assessment == record["assessment"]})
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
    args = parser.parse_args()
    if args.output.exists() or args.portable_output and args.portable_output.exists():
        parser.error("Use a new output path to preserve previous runs.")
    dataset = json.loads(args.cases.read_text(encoding="utf-8"))
    if args.live:
        asyncio.run(live(args, dataset))
    else:
        replay(args, dataset)


if __name__ == "__main__":
    main()

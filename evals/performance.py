import argparse
import asyncio
import hashlib
import io
import json
import math
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.contracts import AppError, ModelReply


class FixtureModel:
    def __init__(self):
        self.fail = False
        self.invalid = False
        self.calls = []

    async def complete(self, messages):
        call = {"usage": None}
        self.calls.append(call)
        await asyncio.sleep(0.005)
        if self.fail:
            raise AppError("model_timeout", "评测夹具：模拟模型超时", 504)
        if self.invalid:
            self.invalid = False
            return ModelReply(content="invalid json")
        multimodal = next((message.content for message in messages if isinstance(message.content, list)), None)
        if multimodal is None:
            call["usage"] = {"input_tokens": 10, "output_tokens": 3}
            return ModelReply(content="你好，请说说这次的目标。", usage=call["usage"])
        payload = json.loads(multimodal[0].text)
        attachment_id = payload["images"][0]["attachment_id"]
        extraction = {"task": "craft", "inventory_coverage": "partial", "observations": [
            {"id": "sulfur", "attachment_id": attachment_id, "kind": "inventory", "region_kind": "backpack",
             "slot": "row_1_col_1", "label": "硫磺", "item_id": "sulfur", "quantity": 100,
             "quantity_status": "readable", "bbox": [0.1, 0.2, 0.4, 0.6], "description": "合成评测观测"}],
            "calculation": {"operation": "craft", "goals": {"gunpowder": 10}}}
        call["usage"] = {"input_tokens": 100, "output_tokens": 40,
                         "total_tokens": 140, "input_tokens_details": {"cached_tokens": 0}}
        return ModelReply(content=json.dumps(extraction), usage=call["usage"])


def delivery_checks(events, identifier, expected_status, before, after):
    terminal = events[-1] if events else {}
    turn = terminal.get("turn") or {}
    expected_type = "result" if expected_status == 200 else "error"
    checks = {
        "terminal_type": terminal.get("type") == expected_type,
        "request_identity": bool(events) and all(event.get("request_id") == identifier for event in events),
        "single_terminal": sum(event.get("type") in {"result", "error"} for event in events) == 1,
        "terminal_status": (expected_status == 200 and terminal.get("type") == "result"
                            or terminal.get("error", {}).get("status") == expected_status),
    }
    previous = next((item for item in before if item["request_id"] == identifier), None)
    if expected_status == 200:
        saved = next((item for item in after if item["request_id"] == identifier), None)
        checks.update(answer_present=isinstance(turn.get("assistant"), str) and bool(turn["assistant"].strip()),
                      turn_identity=turn.get("request_id") == identifier,
                      persisted_answer=saved is not None and saved == turn,
                      turn_count=len(after) == len(before) + (0 if previous else 1),
                      existing_turns_unchanged=all(item in after for item in before))
        if previous is not None:
            checks["replay_unchanged"] = previous == turn and before == after
    else:
        checks.update(no_failed_turn=not turn and before == after,
                      error_code=terminal.get("error", {}).get("code") == "model_timeout")
    return checks


def evaluate(repetitions=3):
    root = Path(__file__).resolve().parents[1]
    paths = [*sorted((root / "src/rust_coach").rglob("*.py")), Path(__file__).resolve(),
             root / "src/rust_coach/calculations/rules.json", root / "src/rust_coach/knowledge/curated.json"]
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    samples = []
    for repeat in range(repetitions):
        with tempfile.TemporaryDirectory(prefix="coach-performance-") as directory:
            settings = Settings(_env_file=None, api_key="fixture-only", search_provider="off", database_path=Path(directory) / "demo.sqlite3")
            model = FixtureModel()
            application = create_app(settings, model)
            with TestClient(application) as client:
                owner = client.post("/api/conversations", json={}).json()["id"]
                vision = application.state.vision
                vision.drafts.metadata("probe:" + vision.config_id, {"status": "passed", "fixture": True})
                output = io.BytesIO()
                Image.new("RGB", (320, 180), "green").save(output, format="PNG")
                attachment = client.post(f"/api/conversations/{owner}/attachments",
                    files={"file": ("fixture.png", output.getvalue(), "image/png")}).json()

                def run(name, message, expected_calls, *, request_id=None, image=False, expected_status=200):
                    identifier = request_id or str(uuid4())
                    payload = {"request_id": identifier, "message": message}
                    if image:
                        payload["attachment_ids"] = [attachment["id"]]
                    before = client.get(f"/api/conversations/{owner}").json()["turns"]
                    call_start = len(model.calls)
                    started = perf_counter()
                    response = client.post(f"/api/conversations/{owner}/messages", json=payload,
                                           headers={"Accept": "text/event-stream"})
                    elapsed = (perf_counter() - started) * 1000
                    try:
                        events = [json.loads(frame[6:]) for frame in response.text.split("\n\n") if frame.startswith("data: ")]
                        valid_events = all(isinstance(event, dict) for event in events)
                    except ValueError:
                        events, valid_events = [], False
                    if not valid_events:
                        events = []
                    terminal = events[-1] if events else {}
                    turn = terminal.get("turn") or {}
                    after = client.get(f"/api/conversations/{owner}").json()["turns"]
                    calls = model.calls[call_start:]
                    report = client.get(f"/api/conversations/{owner}/performance", params={"request_id": identifier}).json()
                    chat_runs = [entry for entry in report["runs"] if entry["kind"] == "chat"]
                    current = chat_runs[-1] if chat_runs else {}
                    summary = current.get("summary", {})
                    calculation = turn.get("calculation") or {}
                    checks = {**delivery_checks(events, identifier, expected_status, before, after),
                              "sse_response": response.status_code == 200 and valid_events and
                                  response.headers.get("content-type", "").startswith("text/event-stream"),
                              "model_calls": summary.get("model_calls") == expected_calls == len(calls),
                              "no_provider_requests": summary.get("dispatched_model_requests") == 0,
                              "recording_complete": summary.get("recording_status") == "complete",
                              "run_identity": current.get("request_id") == identifier and current.get("conversation_id") == owner,
                              "run_status": current.get("status") == ("completed" if expected_status == 200 else "failed"),
                              "replay_flag": (current.get("outcome") or {}).get("replayed") is (name == "request_replay")
                                  if expected_status == 200 else current.get("error_code") == "model_timeout",
                              "no_shared_budget": report["shared_request_budget"] is False}
                    for field in ("input_tokens", "output_tokens", "total_tokens"):
                        values = [call["usage"][field] for call in calls if call["usage"] and field in call["usage"]]
                        checks[field] = summary.get("tokens", {}).get(field) == {
                            "known_sum": sum(values) if values else None, "reported_calls": len(values), "total_calls": len(calls)}
                    if name in {"image_cold", "image_warm", "image_repair"}:
                        checks["calculation"] = calculation.get("status") == "calculated" and calculation["input"]["goals"] == {"gunpowder": 10}
                        checks["inventory"] = calculation.get("input", {}).get("inventory") == {"sulfur": 100}
                        result = calculation.get("result", {})
                        checks["resource_result"] = result.get("acquisition") == {"charcoal": 30} and result.get("inventory_used") == {"sulfur": 20}
                        checks["partial_inventory"] = result.get("inventory_complete") is False
                    if name in {"raid_switch", "hp_followup", "local_calculation", "request_replay"}:
                        checks["calculation"] = calculation.get("status") == "calculated" and calculation["input"]["operation"] == "raid"
                        options = calculation.get("result", {}).get("options", [])
                        c4 = next((option for option in options if option["item"] == "explosive.timed"), {})
                        checks["raid_result"] = c4.get("count") == (2 if name == "raid_switch" else 1)
                    if name == "hp_followup":
                        checks["hp"] = calculation.get("input", {}).get("hp") == 250
                    if name in {"knowledge_cold", "knowledge_warm"}:
                        checks["knowledge"] = (turn.get("presentation") or {}).get("knowledge", {}).get("status") == "supported_snapshot"
                    samples.append({"scenario": name, "repeat": repeat + 1, "input": message,
                                    "http_elapsed_ms": round(elapsed, 3), "checks": checks, "passed": all(checks.values()),
                                    "outcome": current.get("outcome"), "summary": summary,
                                    "server_elapsed_ms": current.get("duration_ms"), "observations": current.get("observations", []),
                                    "spans": current.get("spans", []),
                                    "delivery": {"type": terminal.get("type"), "request_id": terminal.get("request_id"),
                                                 "error": terminal.get("error"), "assistant": turn.get("assistant"),
                                                 "calculation_input": calculation.get("input"),
                                                 "calculation_result": calculation.get("result")},
                                    "independent_model_calls": calls, "saved_turn_count": len(after)})
                    return identifier

                run("image_cold", "制作十份火药", 1, image=True)
                run("image_warm", "制作十份火药", 0, image=True)
                run("raid_switch", "改成拆一面石墙最省资源", 0)
                run("hp_followup", "它还剩250血", 0)
                run("knowledge_cold", "/knowledge C4配方是什么", 0)
                run("knowledge_warm", "/knowledge C4配方是什么", 0)
                local = '/calc {"operation":"raid","target":"building:base-wall:stone","hp":250}'
                identifier = run("local_calculation", local, 0)
                run("request_replay", local, 0, request_id=identifier)
                model.fail = True
                identifier = run("model_failure", "你好", 1, expected_status=504)
                model.fail = False
                run("retry_after_failure", "你好", 1, request_id=identifier)
                model.invalid = True
                run("image_repair", "请制作十份火药", 2, image=True)
    unchanged = all(hashlib.sha256((root / name).read_bytes()).hexdigest() == value for name, value in hashes.items())
    assert unchanged
    groups = {}
    for name in dict.fromkeys(sample["scenario"] for sample in samples):
        selected = [sample for sample in samples if sample["scenario"] == name]
        timings = sorted(sample["http_elapsed_ms"] for sample in selected)
        groups[name] = {"samples": len(selected), "passed": sum(sample["passed"] for sample in selected),
                        "p50_ms": statistics.median(timings), "max_ms": max(timings),
                        "p95_ms": timings[math.ceil(len(timings) * .95) - 1] if len(timings) >= 20 else None,
                        "model_calls": [sample["summary"].get("model_calls") for sample in selected]}
    return {"version": "performance-fixture-v2", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "mode": "synthetic_model_real_application_http_sse", "source_hashes": hashes,
            "sources_unchanged": unchanged, "repetitions": repetitions, "summary": groups,
            "passed": sum(sample["passed"] for sample in samples), "total": len(samples), "samples": samples,
            "limitations": ["合成图片和模型返回，模型延迟为5毫秒夹具；不是视觉准确率或真实模型性能",
                "缓存复用是已有能力，本报告验证计量与数据链路，不声称本轮优化收益",
                "冷/热指应用缓存，不代表供应商缓存；费用未知；无整次请求共享预算",
                "HTTP计时不含上传和探测，非真实浏览器首事件时间；少于20次不报告p95"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 20:
        parser.error("repetitions must be between 1 and 20")
    report = evaluate(args.repetitions)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["passed"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

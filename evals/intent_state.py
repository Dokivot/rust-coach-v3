import argparse
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.contracts import ModelReply
from rust_coach.player_state.schemas import ScopeCreate, StateUpdate
from rust_coach import __file__ as package_file
from rust_coach.calculations.catalog import RuleCatalog


class FixtureModel:
    def __init__(self):
        self.step = {}
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        parts = next((message.content for message in messages if isinstance(message.content, list)), None)
        if parts:
            context = json.loads(parts[0].text)
            identifier = context["images"][0]["attachment_id"]
            fixture = self.step["image"]
            observation = {"id": "material", "attachment_id": identifier, "kind": "inventory", "region_kind": "backpack",
                "slot": "row_1_col_1", "label": "硫磺", "item_id": fixture.get("item_id", "sulfur"),
                "item_status": fixture.get("item_status", "identified"), "quantity": fixture.get("quantity", 100),
                "quantity_status": "unknown" if fixture.get("quantity", 100) is None else "readable",
                "bbox": [0.1, 0.1, 0.5, 0.5]}
            output = {"task": "craft", "observations": [observation],
                "inventory_coverage": fixture.get("coverage", "full_view"),
                "calculation": {"operation": "craft", "goals": {"explosive.timed": 1}}}
        elif "计算请求解析器" in messages[0].content:
            output = self.step.get("model_response", {"chat": True})
        else:
            output = "可以继续提问；精确数值以计算结果为准。"
        return ModelReply(content=output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))


def observe(turn, snapshot, previous_task, calls, error=None):
    architecture = (turn.get("presentation") or {}).get("architecture") or {}
    task = architecture.get("task") or {}
    calculation = turn.get("calculation") or {}
    inputs = calculation.get("input") or {}
    receipt = architecture.get("state_receipt") or {}
    working = architecture.get("working_state") or {}
    return {"route": task.get("route"), "node": task.get("execution_node"), "action": task.get("action"),
        "status": calculation.get("status"), "operation": inputs.get("operation"), "hp": inputs.get("hp"),
        "inventory": inputs.get("inventory"), "inventory_complete": inputs.get("inventory_complete"),
        "source": (calculation.get("input_usage") or {}).get("inventory_source"),
        "event": receipt.get("event"), "profile_revision": snapshot["revision"],
        "backpack": {key: record["value"] for key, record in snapshot["containers"].get("backpack", {}).get("items", {}).items()},
        "storage": {key: record["value"] for key, record in snapshot["containers"].get("storage", {}).get("items", {}).items()},
        "continues_task": bool(previous_task and task.get("task_id") == previous_task),
        "working_task": bool(working), "calls": calls, "error": error or calculation.get("error_code"),
        "result_kind": (architecture.get("decision_result") or {}).get("result_kind"),
        "temporary_fields": sorted((architecture.get("state_view") or {}).get("temporary_facts", {})),
        "task_id": task.get("task_id"), "working_task_id": working.get("task_id")}


def run_case(case, root):
    model = FixtureModel()
    settings = Settings(_env_file=None, api_key="fixture", search_provider="off", database_path=root / (case["id"] + ".sqlite3"))
    app = create_app(settings, model)
    observations = []
    with TestClient(app) as client:
        owner = client.post("/api/conversations", json={}).json()["id"]
        states = app.state.runtime.player_states
        scope = states.create(ScopeCreate(server_id="fixture", wipe_id="wipe", label="固定评测状态"))["scope"]["id"]
        states.bind(owner, scope)
        initial = case.get("initial", {})
        states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=0,
            observed_at=datetime.now(timezone.utc) - timedelta(minutes=initial.get("age_minutes", 0)),
            changes={"facts": {"technology.workbench": 3, "base.has_base": True},
                     "inventory": [{"container_id": "backpack", "mode": "replace", "complete": initial.get("complete", True),
                                    "items": initial.get("inventory", {"sulfur": 1000})},
                                   {"container_id": "storage", "mode": "replace", "complete": True, "items": {"sulfur": 5000}}]}))
        vision = app.state.vision
        vision.drafts.metadata("probe:" + vision.config_id, {"status": "passed"})
        previous_task = None
        for index, step in enumerate(case["steps"]):
            if step.get("new_conversation"):
                owner = client.post("/api/conversations", json={}).json()["id"]
                previous_task = None
            if step.get("update"):
                current = states.get(scope)
                states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=current["revision"],
                    observed_at=datetime.now(timezone.utc), changes=step["update"]))
            model.step = step
            before_calls = model.calls
            payload = {"request_id": str(uuid4()), "message": step["message"]}
            if step.get("container"):
                payload["player_inventory_container"] = step["container"]
            if step.get("image") is not None:
                buffer = io.BytesIO()
                Image.new("RGB", (320, 180), "green").save(buffer, format="PNG")
                upload = client.post(f"/api/conversations/{owner}/attachments", files={"file": ("fixture.png", buffer.getvalue(), "image/png")})
                upload.raise_for_status()
                identifier = upload.json()["id"]
                payload["attachment_ids"] = [identifier]
                payload["image_context"] = {identifier: {"subject": step["image"].get("subject", "self"),
                    "captured_at": (datetime.now(timezone.utc) - timedelta(minutes=step["image"].get("age_minutes", 0))).isoformat()}}
            started = monotonic()
            response = client.post(f"/api/conversations/{owner}/messages", headers={"accept": "text/event-stream"}, json=payload)
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            terminal = events[-1] if events else {"type": "error", "error": {"code": "missing_terminal_event"}}
            turn = terminal.get("turn", {})
            actual = observe(turn, states.get(scope), previous_task, model.calls - before_calls,
                             terminal.get("error", {}).get("code"))
            checks = [{"field": key, "expected": value, "actual": actual.get(key), "passed": actual.get(key) == value}
                      for key, value in step["expected"].items()]
            checks.append({"field": "terminal_type", "expected": "result", "actual": terminal["type"],
                           "passed": terminal["type"] == "result"})
            observations.append({"turn": index, "message": step["message"], "checks": checks,
                "elapsed_ms": round((monotonic() - started) * 1000), "model_calls": model.calls - before_calls,
                "terminal_type": terminal["type"], "actual": actual})
            previous_task = actual["working_task_id"]
    failed = next((item["turn"] for item in observations if not all(check["passed"] for check in item["checks"])), None)
    return {"case_id": case["id"], "category": case["category"], "passed": failed is None,
            "first_failed_turn": failed, "turns": observations}


def evaluate(path):
    raw = path.read_bytes()
    dataset = json.loads(raw)
    with TemporaryDirectory(prefix="rust-coach-intent-eval-") as directory:
        results = [run_case(case, Path(directory)) for case in dataset["cases"]]
    source = Path(package_file).resolve().parent
    source_hash = hashlib.sha256()
    for filename in sorted(source.rglob("*.py")):
        source_hash.update(str(filename.relative_to(source)).encode())
        source_hash.update(filename.read_bytes())
    return {"dataset": dataset["version"], "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "source_sha256": source_hash.hexdigest(), "mode": "scripted_http_sse",
        "grader_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "rule_provenance": RuleCatalog().provenance(),
        "rules": "既定PC原版规则快照；本评测不认证游戏build", "case_count": len(results),
        "passed": sum(item["passed"] for item in results), "failed": sum(not item["passed"] for item in results),
        "turn_count": sum(len(item["turns"]) for item in results), "results": results,
        "limitations": ["模型抽取与视觉输出使用固定夹具；不衡量真实模型语言理解或识图准确率。",
                        "HTTP/SSE使用ASGI测试客户端，不等同浏览器交互验收。",
                        "样例用于工程回归，不能作为未见真实玩家分布的准确率。"]}


def compare_reports(before, after):
    for key in ("dataset_sha256", "mode", "grader_sha256", "rule_provenance"):
        if before[key] != after[key]:
            raise ValueError("incompatible_evaluation:" + key)
    old = {case["case_id"]: case for case in before["results"]}
    if set(old) != {case["case_id"] for case in after["results"]}:
        raise ValueError("incompatible_case_coverage")
    return {"baseline_source_sha256": before["source_sha256"], "before_passed": before["passed"],
        "after_passed": after["passed"],
        "newly_passed": [case["case_id"] for case in after["results"] if case["passed"] and not old[case["case_id"]]["passed"]],
        "regressions": [case["case_id"] for case in after["results"] if not case["passed"] and old[case["case_id"]]["passed"]],
        "interpretation": "新契约与工程行为的案例通过数；包含新元数据要求，不是模型准确率对比。"}


def main():
    parser = argparse.ArgumentParser(description="意图与状态链路的固定夹具HTTP/SSE评测，不调用付费模型")
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("intent-state-cases.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出文件已存在，请选择新路径保留历史证据")
    report = evaluate(args.cases)
    if args.baseline:
        report["comparison"] = compare_reports(json.loads(args.baseline.read_text()), report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("mode", "case_count", "turn_count", "passed", "failed")}, ensure_ascii=False))
    raise SystemExit(bool(report["failed"]))


if __name__ == "__main__":
    main()

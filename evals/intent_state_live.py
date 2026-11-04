import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from rust_coach import __file__ as package_file
from rust_coach.app import create_app
from rust_coach.calculations.catalog import RuleCatalog
from rust_coach.config import Settings
from rust_coach.contracts import AppError
from rust_coach.llm import CompatibleChatModel
from rust_coach.player_state.schemas import ScopeCreate, StateUpdate


class BudgetModel:
    def __init__(self, settings, maximum):
        self.settings = settings
        self.maximum = maximum
        self.calls = []

    async def complete(self, messages):
        if len(self.calls) >= self.maximum:
            raise AppError("evaluation_budget_exceeded", "真实模型调用达到本次评测预算。", 422)
        record = {"number": len(self.calls) + 1, "status": "started"}
        self.calls.append(record)
        started = monotonic()
        try:
            async with asyncio.timeout(45), httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
                result = await CompatibleChatModel(self.settings, client, stream_responses=True).complete(messages)
            record.update(status="completed", usage=result.usage)
            return result
        except Exception as error:
            record.update(status="failed", error_code=getattr(error, "code", type(error).__name__))
            raise
        finally:
            record["elapsed_ms"] = round((monotonic() - started) * 1000)


def check_turn(terminal, actual, expected):
    checks = [{"field": key, "expected": value, "actual": actual.get(key), "passed": actual.get(key) == value}
              for key, value in expected.items()]
    checks.append({"field": "terminal_type", "expected": "result", "actual": terminal.get("type"),
                   "passed": terminal.get("type") == "result"})
    if "operation" in expected:
        calculation = (terminal.get("turn") or {}).get("calculation") or {}
        checks.extend([
            {"field": "calculation_status", "expected": "calculated", "actual": calculation.get("status"),
             "passed": calculation.get("status") == "calculated"},
            {"field": "calculation_result", "expected": "nonempty_result", "actual": bool(calculation.get("result")),
             "passed": isinstance(calculation.get("result"), dict) and bool(calculation["result"])},
        ])
    return checks


def run(maximum):
    settings = Settings()
    if not settings.configured:
        raise ValueError("模型尚未配置；未执行真实调用")
    model = BudgetModel(settings, maximum)
    turns = []
    with TemporaryDirectory(prefix="rust-coach-live-state-") as directory:
        isolated = settings.model_copy(update={"database_path": Path(directory) / "coach.sqlite3", "search_provider": "off"})
        app = create_app(isolated, model)
        with TestClient(app) as client:
            owner = client.post("/api/conversations", json={}).json()["id"]
            states = app.state.runtime.player_states
            scope = states.create(ScopeCreate(server_id="live-eval", wipe_id="fixed-demo", label="隔离冒烟测试"))["scope"]["id"]
            states.bind(owner, scope)
            states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=0, observed_at=datetime.now(timezone.utc),
                changes={"facts": {"technology.workbench": 3}, "inventory": [
                    {"container_id": "backpack", "mode": "replace", "complete": True, "items": {"sulfur": 1000}}]}))
            cases = [
                ("按档案背包，制作两个C4还缺多少材料？", {"operation": "craft", "inventory": {"sulfur": 1000},
                    "goals": {"explosive.timed": 2}, "sulfur_needed": 3400}, False),
                ("改成拆一面石墙最省资源", {"operation": "raid", "action": "switch"}, False),
                ("它还剩250血", {"operation": "raid", "hp": 250}, False),
                ("我刚死了，背包丢了", {"event": "death", "backpack": {}, "revision": 2}, False),
                ("按档案背包，制作两个C4还缺多少材料？", {"operation": "craft", "inventory": {}, "revision": 2,
                    "goals": {"explosive.timed": 2}, "sulfur_needed": 4400}, True),
                ("我有1级工作台，继续计算", {"operation": "craft", "workbench": 1, "profile_workbench": 3,
                    "workbench_status": "blocked"}, False),
            ]
            for message, expected, new_conversation in cases:
                if new_conversation:
                    owner = client.post("/api/conversations", json={}).json()["id"]
                started = monotonic()
                response = client.post(f"/api/conversations/{owner}/messages", headers={"accept": "text/event-stream"},
                    json={"request_id": str(uuid4()), "message": message})
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                terminal = events[-1] if events else {"type": "error", "error": {"code": "missing_terminal_event"}}
                turn = terminal.get("turn") or {}
                architecture = (turn.get("presentation") or {}).get("architecture") or {}
                inputs = (turn.get("calculation") or {}).get("input") or {}
                calculation_result = (turn.get("calculation") or {}).get("result") or {}
                current = states.get(scope)
                actual = {"operation": inputs.get("operation"), "inventory": inputs.get("inventory"), "hp": inputs.get("hp"),
                    "workbench": inputs.get("workbench"), "action": (architecture.get("task") or {}).get("action"),
                    "event": (architecture.get("state_receipt") or {}).get("event"), "revision": current["revision"],
                    "backpack": {key: record["value"] for key, record in current["containers"]["backpack"]["items"].items()},
                    "profile_workbench": current["facts"]["technology.workbench"]["value"],
                    "goals": inputs.get("goals"),
                    "sulfur_needed": calculation_result.get("acquisition", {}).get("sulfur"),
                    "workbench_status": calculation_result.get("requirements", {}).get("workbench_status")}
                checks = check_turn(terminal, actual, expected)
                passed = all(check["passed"] for check in checks)
                turns.append({"message": message, "new_conversation": new_conversation, "expected": expected, "actual": actual,
                    "checks": checks, "calculation": turn.get("calculation"),
                    "passed": passed, "elapsed_ms": round((monotonic() - started) * 1000), "terminal_type": terminal["type"],
                    "error": terminal.get("error"), "answer": turn.get("assistant"), "architecture": architecture})
    source = Path(package_file).resolve().parent
    digest = hashlib.sha256()
    for filename in sorted(source.rglob("*.py")):
        digest.update(str(filename.relative_to(source)).encode())
        digest.update(filename.read_bytes())
    return {"mode": "live_model_http_sse", "model": settings.model, "api_mode": settings.api_mode,
        "grader_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "recorded_at": datetime.now(timezone.utc).isoformat(), "source_sha256": digest.hexdigest(),
        "rule_provenance": RuleCatalog().provenance(), "call_budget": maximum, "calls": model.calls,
        "turn_count": len(turns), "passed": sum(turn["passed"] for turn in turns), "turns": turns,
        "limitations": ["单次六轮文本冒烟，不证明模型泛化准确率。", "本次未调用视觉模型，不验证识图质量。",
                        "使用临时数据库和合成玩家状态，不读写用户现有档案。", "费用没有账单核对，调用数不是金额。"]}


def main():
    parser = argparse.ArgumentParser(description="隔离数据库的真实模型冒烟，会产生模型调用费用")
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--max-model-calls", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出文件已存在，请选择新路径保留历史证据")
    report = run(args.max_model_calls)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"turns": report["turn_count"], "passed": report["passed"], "model_calls": len(report["calls"])}, ensure_ascii=False))
    raise SystemExit(report["passed"] != report["turn_count"])


if __name__ == "__main__":
    main()

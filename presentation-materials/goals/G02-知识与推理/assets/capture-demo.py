import json
import hashlib
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.knowledge.bundle import default_release


class NoExternalModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        raise AssertionError("This material capture must not call a model")


def capture():
    root = Path(__file__).resolve().parents[4]
    sources = [*sorted((root / "src/rust_coach").rglob("*.py")), root / "src/rust_coach/calculations/rules.json",
               root / "src/rust_coach/knowledge/curated.json", Path(__file__).resolve()]
    fingerprints = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    samples = []
    model = NoExternalModel()
    with tempfile.TemporaryDirectory(prefix="knowledge-material-") as directory:
        settings = Settings(_env_file=None, database_path=Path(directory) / "demo.sqlite3", api_key="", search_provider="off")
        application = create_app(settings, model)
        with TestClient(application) as client:
            owner = client.post("/api/conversations", json={}).json()["id"]
            path = f"/api/conversations/{owner}/messages"

            def send(identifier, message, *, stream=False, request_id=None):
                payload = {"request_id": request_id or str(uuid4()), "message": message}
                response = client.post(path, json=payload, headers={"Accept": "text/event-stream"} if stream else {})
                if stream:
                    events = [json.loads(frame[6:]) for frame in response.text.split("\n\n") if frame.startswith("data: ")]
                    assert events[-1]["type"] == "result", events[-1]
                    turn = events[-1]["turn"]
                else:
                    turn = response.json()
                evidence = (turn.get("presentation") or {}).get("knowledge") or {}
                calculation = turn.get("calculation") or {}
                sample = {"id": identifier, "message": message, "transport": "SSE" if stream else "HTTP JSON",
                    "http_status": response.status_code, "assistant": turn.get("assistant"), "error": turn.get("error"),
                    "calculation_status": calculation.get("status"), "error_code": calculation.get("error_code"),
                    "calculation_input": calculation.get("input"), "knowledge_status": evidence.get("status"),
                    "release_id": evidence.get("release_id"), "registry_revision": evidence.get("registry_revision"),
                    "need": evidence.get("need"), "selected": [document["id"] for document in evidence.get("documents", [])],
                    "missing": evidence.get("missing"), "excluded": evidence.get("excluded"),
                    "rule_dependencies": evidence.get("rule_dependencies"),
                    "derived_input": evidence.get("derivation", {}).get("input"),
                    "derived_formulas": [option["formula"] for option in evidence.get("derivation", {}).get("result", {}).get("options", [])],
                    "raid_options": [{key: option[key] for key in ("item", "damage", "count", "formula")}
                                     for option in calculation.get("result", {}).get("options", [])]}
                samples.append(sample)
                return turn, payload

            send("recipe", "/knowledge C4配方是什么", stream=True)
            initial, initial_request = send("raid", '/calc {"operation":"raid","target":"building:base-wall:stone","hp":250,"methods":["explosive.timed"]}')
            explanation, _ = send("explanation", "为什么250血还需要一颗C4", stream=True)
            send("false_premise", "为什么250血还需要两颗C4", stream=True)
            send("latest", "/knowledge 最新版本C4配方是什么")
            send("other_scope", "/knowledge 主机版C4配方是什么")
            knowledge = application.state.runtime.knowledge
            initial_reference = knowledge.reference()
            feedback = client.post(f"/api/conversations/{owner}/knowledge-feedback", json={
                "request_id": "material-report", "turn_request_id": explanation["request_id"],
                "document_ids": ["raid:building:base-wall:stone"], "reason": "演示反馈：其他资料数值不同，待核实"})
            assert feedback.status_code == 200
            samples.append({"id": "feedback", "http_status": feedback.status_code,
                "status": feedback.json()["status"], "verification": feedback.json()["verification"],
                "knowledge_reference_unchanged": knowledge.reference() == initial_reference})
            registry = knowledge.registry
            registry.revoke(initial_reference["release_id"], "raid:building:base-wall:stone", "材料采集夹具：待核实目标记录")
            send("after_revocation", "为什么250血还需要一颗C4")
            send("calculation_blocked", initial_request["message"])
            send("replay_blocked", initial_request["message"], request_id=initial_request["request_id"])
            package = default_release()
            package["id"] = "material-demo-v2"
            package["documents"] = [document for document in package["documents"] if document["id"] != "raid:building:base-wall:stone"]
            installed = registry.install(package, knowledge.catalog.sha256)
            candidate_did_not_activate = knowledge.reference()["release_id"] == initial_reference["release_id"]
            registry.activate(package["id"], installed["release_sha256"], "材料采集：移除待核实目标；不认证游戏build")
            send("candidate_missing_target", "为什么250血还需要一颗C4")
            registry.activate(initial_reference["release_id"], initial_reference["release_sha256"], "材料采集：回退原包")
            send("rollback_still_revoked", "为什么250血还需要一颗C4")
            samples.append({"id": "publication", "candidate_did_not_activate": candidate_did_not_activate,
                "final_release_id": knowledge.reference()["release_id"],
                "rollback_preserves_original_revocation": "raid:building:base-wall:stone" in registry.snapshot()["revoked"]})
    assert model.calls == 0
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == value for name, value in fingerprints.items())
    return {"captured_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "mode": "actual_application_HTTP_SSE_with_structured_requests_and_model_forbidden",
        "scope": "材料整理的本地应用记录；合成任务和临时数据库，无图像、真人玩家数据、浏览器或游戏内验证",
        "source_hashes": fingerprints, "sources_unchanged_during_capture": True,
        "model_calls": model.calls, "samples": samples}


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    print(json.dumps(capture(), ensure_ascii=False, indent=2))

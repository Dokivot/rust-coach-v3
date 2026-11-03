import argparse
import asyncio
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx

from rust_coach.advice.service import AdviceService
from rust_coach.config import Settings
from rust_coach.contracts import ChatRequest
from rust_coach.knowledge.store import EvidenceStore
from rust_coach.knowledge.web import WebResearch
from rust_coach.llm import CompatibleChatModel
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime


class CountedModel:
    def __init__(self, model, diagnostic_path):
        self.model = model
        self.diagnostic_path = diagnostic_path
        self.calls = 0
        self.responses = []

    async def complete(self, messages):
        self.calls += 1
        started = monotonic()
        print(json.dumps({"event": "model_start", "call": self.calls,
            "input_chars": sum(len(message.content) for message in messages)}), flush=True)
        result = await self.model.complete(messages)
        self.responses.append(result.content)
        self.diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        self.diagnostic_path.write_text(json.dumps(self.responses, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"event": "model_complete", "call": self.calls,
            "elapsed_seconds": round(monotonic() - started, 2), "output_chars": len(result.content)}), flush=True)
        return result


async def run(output, repeat=1):
    configured = Settings()
    if not configured.configured:
        raise RuntimeError("Configure COACH_API_KEY locally before running the live smoke test.")
    reports = []
    with tempfile.TemporaryDirectory(prefix="rust-coach-onboarding-") as directory:
        settings = configured.model_copy(update={"database_path": Path(directory) / "smoke.sqlite3",
            "max_output_tokens": max(4096, configured.max_output_tokens)})
        store = ConversationStore(settings.database_path)
        archive = EvidenceStore(store)
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout_seconds, connect=10), follow_redirects=False) as client:
            model = CountedModel(CompatibleChatModel(settings, client, stream_responses=True), output.with_suffix(".models.json"))
            service = AdviceService(model, WebResearch(settings, client, archive), settings.instruction_role)
            runtime = ChatRuntime(settings, model, store, advice_service=service)
            owner = store.create().id
            for name, message in [
                *[(f"new_player_{index}", "我是新手，什么都不懂，应该优先做什么？") for index in range(1, repeat + 1)],
                ("existing_base", "我已经有上锁的小家、睡袋、石斧和石镐了，接下来优先做什么？"),
                ("exact_budget", '/calc {"operation":"craft","goals":{"explosive.timed":2}}'),
            ]:
                if name.startswith("new_player_"):
                    owner = store.create().id
                before = model.calls
                turn = await runtime.chat(owner, ChatRequest(request_id=uuid4(), message=message))
                advice = turn.advice or {}
                report = {"case": name, "question": message, "answer": turn.assistant,
                    "model_calls": model.calls - before, "mode": advice.get("mode"), "status": advice.get("status"),
                    "queries": (advice.get("evidence") or {}).get("queries"),
                    "sources": (advice.get("evidence") or {}).get("documents"),
                    "events": (advice.get("evidence") or {}).get("events"),
                    "warnings": (advice.get("evidence") or {}).get("warnings"),
                    "review": advice.get("review"), "repairs": advice.get("repairs"),
                    "answer_source": advice.get("answer_source"), "diagnostics": advice.get("diagnostics"),
                    "guide": advice.get("answer"), "calculation": turn.calculation,
                    "model_responses": model.responses[before:]}
                reports.append(report)
                print(json.dumps({key: report[key] for key in ("case", "answer", "model_calls", "status", "answer_source", "diagnostics")}, ensure_ascii=False), flush=True)
                output.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "results": reports},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                if name != "exact_budget" and not advice.get("answer"):
                    break
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "results": reports},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    assert len(reports) == repeat + 2
    assert all(report["mode"] == "onboarding" and report["status"] == "conditional" and report["guide"] and
        (report["answer_source"] == "curated_baseline" or report["review"] and not report["review"]["issues"]) for report in reports[:-1])
    assert all("档案待确认" not in report["answer"] for report in reports[:-1])
    assert reports[-1]["model_calls"] == 0 and reports[-1]["calculation"]["status"] == "calculated"


if __name__ == "__main__":
    logger = logging.getLogger("rust_coach")
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler())
    parser = argparse.ArgumentParser(description="Exercise live web retrieval and the configured model with synthetic beginner questions.")
    parser.add_argument("--live", action="store_true", help="Allow real web and model requests, which may incur API charges.")
    parser.add_argument("--output", type=Path, default=Path("data/onboarding-smoke-results.json"))
    parser.add_argument("--repeat", type=int, choices=range(1, 6), default=1, help="Repeat the beginner question in separate conversations before testing follow-ups.")
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error("Pass --live to call the configured model and search services.")
    asyncio.run(run(arguments.output, arguments.repeat))

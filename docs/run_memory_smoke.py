import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx

from rust_coach.config import Settings
from rust_coach.contracts import ChatRequest
from rust_coach.llm import CompatibleChatModel
from rust_coach.memory.service import MemoryService
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime


async def main():
    settings = Settings()
    if not settings.configured:
        raise SystemExit("Configure the local model key before running this smoke test.")
    results = []
    semaphore = asyncio.Semaphore(2)
    with tempfile.TemporaryDirectory(prefix="rust-coach-memory-") as directory:
        settings = settings.model_copy(update={"database_path": Path(directory) / "smoke.sqlite3", "search_provider": "off"})
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=False) as client:
            model = CompatibleChatModel(settings, client, stream_responses=True)
            store = ConversationStore(settings.database_path)
            memory = MemoryService(store, settings, model)
            cases = [
                ("event", "昨晚我的基地被偷家了", "event", None),
                ("hypothetical", "假如我的基地被偷家，应该怎么处理？", None, None),
                ("correct_explanation", "我理解了：因为截图只显示部分背包，所以没看到的材料不能直接当作零。", "learning", "demonstrated"),
                ("incorrect_explanation", "我理解了：未知库存就是零，所以缺口是准确的。", None, None),
                ("self_report", "合成批次我明白了", "learning", "self_reported"),
            ]

            async def check(case):
                identifier, message, kind, signal = case
                started = monotonic()
                try:
                    async with semaphore:
                        async with asyncio.timeout(60):
                            candidates = await memory.extract(message, {"concepts": ["inventory_scope", "craft_batches"]})
                    passed = not candidates if kind is None else any(item.kind == kind and (signal is None or item.signal == signal) for item in candidates)
                    result = {"id": identifier, "passed": passed, "elapsed_seconds": round(monotonic() - started, 2),
                              "candidates": [item.model_dump() for item in candidates]}
                except Exception as error:
                    result = {"id": identifier, "passed": False, "error_type": type(error).__name__}
                results.append(result)
                print(json.dumps({key: result[key] for key in ("id", "passed")}), flush=True)

            await asyncio.gather(*(check(case) for case in cases))
            runtime = ChatRuntime(settings, model, store, memory_model=model)
            conversation = store.create().id
            await runtime.chat(conversation, ChatRequest(request_id=uuid4(), message="/memory set verbosity concise"))
            receipt = await runtime.chat(conversation, ChatRequest(request_id=uuid4(), message="/memory remember 昨晚我的基地被偷家了"))
            new_conversation = store.create().id
            reply = await runtime.chat(new_conversation, ChatRequest(request_id=uuid4(), message="你记得我之前报告过什么经历吗？"))
            results.append({"id": "cross_conversation_recall", "passed": bool(reply.memory["used_event_ids"]) and
                "被偷家" in reply.assistant and reply.presentation["context"]["preferences"]["verbosity"] == "concise",
                "receipt": receipt.assistant, "answer": reply.assistant})
            await runtime.chat(new_conversation, ChatRequest(request_id=uuid4(), message="/memory forget all"))
            forgotten = await runtime.chat(store.create().id, ChatRequest(request_id=uuid4(), message="你记得我之前报告过什么经历吗？"))
            results.append({"id": "forget_abstention", "passed": not forgotten.memory["used_event_ids"] and
                "被偷家" not in forgotten.assistant, "answer": forgotten.assistant})
            teaching_conversation = store.create().id
            await runtime.chat(teaching_conversation, ChatRequest(request_id=uuid4(), message="/memory set guidance guided"))
            guided = await runtime.chat(teaching_conversation, ChatRequest(request_id=uuid4(), message="教我理解合成批次"))
            await runtime.chat(teaching_conversation, ChatRequest(request_id=uuid4(), message="/memory set guidance independent"))
            independent = await runtime.chat(store.create().id, ChatRequest(request_id=uuid4(), message="解释一下合成批次"))
            results.append({"id": "teaching_modes", "passed": guided.memory["teaching"]["mode"] == "guided" and
                independent.memory["teaching"]["mode"] == "independent" and
                guided.presentation["fallback_reason"] is None and independent.presentation["fallback_reason"] is None and
                "批次" in guided.assistant and "批次" in independent.assistant and all(
                    any(term in answer for term in ("待制作", "净需求", "扣除", "抵扣")) for answer in (guided.assistant, independent.assistant)),
                "guided_answer": guided.assistant, "independent_answer": independent.assistant,
                "note": "Modes and output checked; teaching quality requires human review."})
            await runtime.memory.close()
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "model": settings.model,
              "synthetic_data_only": True, "results": results, "passed": all(item["passed"] for item in results),
              "limitations": "Small synthetic smoke test, not a general accuracy or teaching-effectiveness estimate."}
    destination = Path(__file__).with_name("memory-live-results.json")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "cases": len(results), "report": str(destination)}), flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

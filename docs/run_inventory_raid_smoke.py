import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx

from rust_coach.attachments.store import AttachmentStore, normalize_image
from rust_coach.config import Settings
from rust_coach.contracts import ChatRequest
from rust_coach.llm import CompatibleChatModel
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime
from rust_coach.vision.service import VisionService


async def run(image_path, output):
    settings = Settings()
    reports = []
    with tempfile.TemporaryDirectory(prefix="rust-inventory-raid-") as directory:
        settings = settings.model_copy(update={"database_path": Path(directory) / "smoke.sqlite3"})
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout_seconds, connect=10)) as client:
            store = ConversationStore(settings.database_path)
            attachments = AttachmentStore(store, settings)
            model = CompatibleChatModel(settings, client, stream_responses=True)
            vision = VisionService(settings, CompatibleChatModel(settings.for_vision(), client, stream_responses=True), attachments, store)
            await vision.probe()
            runtime = ChatRuntime(settings, model, store, vision_service=vision)
            owner = store.create().id
            attachment = attachments.put(owner, *normalize_image(image_path.read_bytes(),
                settings.attachment_max_bytes, settings.attachment_max_pixels))
            for message, images in [
                ("我还差多少资源才能造10个C4？", [attachment["id"]]),
                ("拆一面石墙最省资源的方案是什么？", []),
                ("它还剩250血", []),
            ]:
                turn = await runtime.chat(owner, ChatRequest(request_id=uuid4(), message=message, attachment_ids=images))
                reports.append(turn.model_dump(mode="json"))
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"question": message, "answer": turn.assistant,
                    "status": (turn.calculation or {}).get("status")}, ensure_ascii=False), flush=True)
    first, second, third = [report["calculation"] for report in reports]
    assert first["status"] == "calculated" and first["operation"] == "craft"
    assert first["input"]["goals"] == {"explosive.timed": 10}
    assert first["result"]["inventory_used"] and not first["input"]["inventory_complete"]
    assert "库存未知" not in reports[0]["assistant"]
    assert second["operation"] == third["operation"] == "raid"
    assert second["result"]["hp_each"] == 500 and third["result"]["hp_each"] == 250


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay inventory and raid questions using a real image and the configured model, with a temporary database.")
    parser.add_argument("image", type=Path)
    parser.add_argument("--live", action="store_true", help="Send the image to the configured model; API charges may apply.")
    parser.add_argument("--output", type=Path, default=Path("data/inventory-raid-smoke.json"))
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error("Pass --live to use the configured model.")
    asyncio.run(run(arguments.image, arguments.output))

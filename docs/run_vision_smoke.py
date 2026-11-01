import argparse
import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx

from rust_coach.attachments.store import AttachmentStore, normalize_image
from rust_coach.config import Settings
from rust_coach.contracts import ChatRequest
from rust_coach.llm import CompatibleChatModel
from rust_coach.persistence import ConversationStore
from rust_coach.vision.service import VisionService
from rust_coach.runtime import ChatRuntime
from rust_coach.advice.service import AdviceService
from rust_coach.knowledge.web import WebResearch
from rust_coach.knowledge.store import EvidenceStore


class RecordingModel:
    def __init__(self, model):
        self.model = model
        self.responses = []

    async def complete(self, messages):
        started = monotonic()
        reply = await self.model.complete(messages)
        self.responses.append({"content":reply.content,"truncated":reply.truncated,
            "usage":{key:value for key,value in (reply.usage or {}).items() if key in {"input_tokens","output_tokens","total_tokens"}},
            "elapsed_seconds":round(monotonic()-started,2)})
        return reply


async def run(args):
    settings = Settings()
    with tempfile.TemporaryDirectory(prefix="rust-vision-eval-") as directory:
        settings.database_path = Path(directory) / "eval.sqlite3"
        async with httpx.AsyncClient(timeout=180) as client:
            model = RecordingModel(CompatibleChatModel(settings.for_vision(),client,stream_responses=True))
            store = ConversationStore(settings.database_path)
            attachments = AttachmentStore(store,settings)
            vision = VisionService(settings,model,attachments,store)
            probe = await vision.probe()
            owner = str(store.create().id)
            image = normalize_image(Path(args.image).read_bytes(),settings.attachment_max_bytes,settings.attachment_max_pixels)
            attachment = attachments.put(owner,*image)
            request = ChatRequest(request_id=uuid4(),message=args.question,attachment_ids=[attachment["id"]],
                image_context={attachment["id"]:{"subject":"unknown"}})
            result = {"tested_at":datetime.now(timezone.utc).isoformat(),"fixture":args.image,"question":args.question,
                "model":settings.for_vision().model,"probe_passed":probe["status"] == "passed"}
            try:
                if args.workflow:
                    settings.search_provider = "off"
                    advice = AdviceService(model,WebResearch(settings,client,EvidenceStore(store)),settings.instruction_role)
                    runtime = ChatRuntime(settings,model,store,advice_service=advice,vision_service=vision)
                    turn = await runtime.chat(owner,request)
                    result.update(status="passed",turn=turn.model_dump(mode="json"))
                else:
                    draft = await vision.analyze(owner,request,None)
                    result.update(status="passed",extraction=draft["payload"]["extraction"],repairs=draft["payload"]["repairs"])
            except Exception as error:
                result.update(status="failed",error=getattr(error,"code",type(error).__name__))
            result["responses"] = model.responses
            Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
            print(json.dumps({key:value for key,value in result.items() if key not in {"responses","extraction","turn"}},ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Opt-in live visual smoke test; sends the specified image to the configured model.")
    parser.add_argument("image")
    parser.add_argument("--question",default="只识别图中可见内容，不做数值推断。")
    parser.add_argument("--output",default="data/vision-eval/result.json")
    parser.add_argument("--workflow",action="store_true",help="Run the complete chat/advice graph, with web search disabled.")
    asyncio.run(run(parser.parse_args()))

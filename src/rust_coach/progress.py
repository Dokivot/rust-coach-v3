import asyncio
import json
import logging
from contextvars import ContextVar
from typing import Callable

from rust_coach.contracts import AppError
from rust_coach.performance import progress


logger = logging.getLogger("rust_coach")
STAGE_LABELS = {
    "context": "正在读取上下文",
    "image_context": "正在读取截图信息",
    "recognize_image": "正在识图",
    "check_image": "正在核对识图结果",
    "refine_image": "正在复核模糊读数",
    "understand": "正在理解问题",
    "plan": "正在规划方案",
    "retrieve": "正在查找资料",
    "evaluate": "正在比较方案",
    "calculate_craft": "正在计算资源缺口",
    "calculate_raid": "正在计算拆除成本",
    "compose": "正在组织语言",
    "check_answer": "正在核对回答",
    "player_state": "正在读取玩家状态",
    "memory": "正在处理个人记忆",
    "save": "正在保存结果",
    "replay": "正在恢复已有结果",
}
_progress_sink: ContextVar[Callable[[str], None] | None] = ContextVar("progress_sink", default=None)


def report_stage(stage: str):
    if stage in STAGE_LABELS:
        progress(stage)
    sink = _progress_sink.get()
    if sink is not None and stage in STAGE_LABELS:
        sink(stage)


async def chat_events(runtime, conversation_id, request):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    last_stage = None
    sequence = 0
    closed = False

    def publish(stage):
        nonlocal last_stage, sequence
        if closed or stage == last_stage:
            return
        last_stage = stage
        sequence += 1
        queue.put_nowait({"type": "stage", "stage": stage, "label": STAGE_LABELS[stage], "sequence": sequence})

    def sink(stage):
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            publish(stage)
        else:
            loop.call_soon_threadsafe(publish, stage)

    async def run():
        token = _progress_sink.set(sink)
        try:
            turn = await runtime.chat(conversation_id, request)
            queue.put_nowait({"type": "result", "turn": turn.model_dump(mode="json")})
        except AppError as error:
            logger.warning("chat_stream_error code=%s", error.code)
            queue.put_nowait({"type": "error", "error": {
                "code": error.code, "message": error.message, "status": error.status}})
        except Exception as error:
            logger.error("chat_stream_error type=%s", type(error).__name__)
            queue.put_nowait({"type": "error", "error": {
                "code": "internal_error", "message": "服务暂时出错，请重试。", "status": 500}})
        finally:
            _progress_sink.reset(token)

    task = asyncio.create_task(run())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            event["request_id"] = str(request.request_id)
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            if event["type"] in {"result", "error"}:
                break
    finally:
        closed = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

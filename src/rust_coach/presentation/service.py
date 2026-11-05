import asyncio
import logging

from rust_coach.contracts import AppError, ModelMessage, ModelReply
from rust_coach.progress import report_stage
from rust_coach.presentation.rendering import fallback_text
from rust_coach.presentation.validation import narrative_issues
from rust_coach.performance import observe


logger = logging.getLogger("rust_coach")


async def generate_chat(model, messages, context):
    checks = []
    for attempt in range(2):
        report_stage("compose")
        try:
            if attempt:
                async with asyncio.timeout(20):
                    reply = await model.complete(messages)
            else:
                reply = await model.complete(messages)
        except (AppError, TimeoutError):
            if attempt == 0:
                raise
            break
        report_stage("check_answer")
        issues = narrative_issues(reply.content)
        if not issues:
            return {"reply": reply, "response_repairs": attempt, "response_checks": checks,
                    "calculation": None}
        checks.extend({"code": issue, "status": "blocked", "attempt": attempt} for issue in issues)
        observe("narrative_rejected", reasons=issues, attempt=attempt + 1)
        if attempt == 0:
            instructions = messages[0].model_copy(update={"content": messages[0].content +
                "\n上次候选未通过表达校验。重新回答当前问题，保持事实和身份边界，不执行新工具或编造结果。错误码：" + ", ".join(issues)})
            messages = [instructions, *messages[1:]]
            logger.info("persona_repair codes=%s", issues)
    observe("narrative_fallback", reason="chat_validation_failed")
    return {"reply": ModelReply(content=fallback_text(context)), "calculation": None,
            "response_checks": checks, "response_repairs": 1, "response_fallback": "chat_validation_failed"}

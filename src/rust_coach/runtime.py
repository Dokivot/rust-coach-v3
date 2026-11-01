import asyncio

from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelMessage, Turn
from rust_coach.llm import ChatModel
from rust_coach.persistence import ConversationStore
from rust_coach.workflows import build_chat_graph


SYSTEM_PROMPT = """你是 Rust Coach，使用清楚、友好的中文回应，也可以跟随用户的语言。
应用已接入 PC 原版拆墙/门爆炸物数量、合成路径与库存缺口的本地确定性计算器。
计算通过单独链路执行，界面有无需模型密钥的本地计算器，也支持 /calc JSON。
你当前处于普通对话节点，没有计算结果授权；不得自己心算、猜测游戏数值、改写历史计算或编造来源。
遇到需要计算的新问题请让用户在本地计算器填写，或明确物品、数量、血量、库存后重新提出计算请求。
尚未接入联网知识检索、战斗伤害、玩家状态决策或图像识别，不得假装拥有这些能力。
可以正常闲聊、解释一般概念、帮助整理问题。一般问候不用反复重复能力说明。"""


class ChatRuntime:
    def __init__(self, settings: Settings, model: ChatModel, store: ConversationStore,
                 calculation_model: ChatModel | None = None):
        self.settings = settings
        self.store = store
        self.graph = build_chat_graph(model, settings.instruction_role, calculation_model)
        self.lock = asyncio.Lock()

    async def chat(self, conversation_id: str, request: ChatRequest) -> Turn:
        if self.lock.locked():
            raise AppError("chat_busy", "已有对话正在生成，请等待完成后再发送。", 409)
        async with self.lock:
            conversation = self.store.get(conversation_id)
            for turn in conversation.turns:
                if turn.request_id == str(request.request_id):
                    if turn.user != request.message:
                        raise AppError("request_conflict", "请求标识已用于另一条消息，请重新发送。", 409)
                    return turn
            if len(conversation.turns) >= 100:
                raise AppError("conversation_full", "当前会话已达到 100 轮，请创建新对话。", 409)
            history: list[ModelMessage] = []
            used_chars = len(request.message)
            for turn in reversed(conversation.turns[-self.settings.history_turns:]):
                turn_chars = len(turn.user) + len(turn.assistant)
                if used_chars + turn_chars > self.settings.context_char_limit:
                    break
                history[0:0] = [ModelMessage(role="user", content=turn.user),
                                ModelMessage(role="assistant", content=turn.assistant)]
                used_chars += turn_chars
            messages = [ModelMessage(role=self.settings.instruction_role, content=SYSTEM_PROMPT),
                        *history, ModelMessage(role="user", content=request.message)]
            try:
                async with asyncio.timeout(self.settings.request_timeout_seconds):
                    previous = conversation.turns[-1].calculation if conversation.turns else None
                    result = await self.graph.ainvoke({"messages": messages,
                        "previous_calculation": previous.get("input") if previous else None,
                        "pending_message": previous.get("pending_message") if previous else None}, {"recursion_limit": 8})
            except TimeoutError as error:
                raise AppError("model_timeout", "模型响应超时，请稍后重试。", 504) from error
            reply = result["reply"]
            return self.store.append(conversation_id, str(request.request_id), request.message,
                                     reply.content, reply.truncated, result.get("calculation"))

    async def delete(self, conversation_id: str) -> None:
        if self.lock.locked():
            raise AppError("chat_busy", "请等待回复结束后再删除会话。", 409)
        async with self.lock:
            self.store.delete(conversation_id)

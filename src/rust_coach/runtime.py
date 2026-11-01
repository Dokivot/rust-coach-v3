import asyncio

from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelMessage, Turn
from rust_coach.llm import ChatModel
from rust_coach.persistence import ConversationStore
from rust_coach.workflows import build_chat_graph


SYSTEM_PROMPT = """你是 Rust Coach 的初版聊天助手，使用清楚、友好的中文回应，也可以跟随用户的语言。
当前只有普通 LLM 对话功能，没有游戏知识库、联网搜索、计算工具、玩家状态解析或决策引擎。
可以正常闲聊、解释一般概念、帮助整理问题。不得声称已经查资料、运行工具或读取玩家状态。
涉及 Rust 的精确爆炸物数量、合成缺口、装备伤害或个性化行动决策时，说明对应能力尚未接入，
可以帮助用户整理待核实的问题，但不要编造数字、引用或假装给出经过验证的决策建议。
一般问候和普通话题不用反复重复能力说明。"""


class ChatRuntime:
    def __init__(self, settings: Settings, model: ChatModel, store: ConversationStore):
        self.settings = settings
        self.store = store
        self.graph = build_chat_graph(model)
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
                    result = await self.graph.ainvoke({"messages": messages}, {"recursion_limit": 4})
            except TimeoutError as error:
                raise AppError("model_timeout", "模型响应超时，请稍后重试。", 504) from error
            reply = result["reply"]
            return self.store.append(conversation_id, str(request.request_id), request.message,
                                     reply.content, reply.truncated)

    async def delete(self, conversation_id: str) -> None:
        if self.lock.locked():
            raise AppError("chat_busy", "请等待回复结束后再删除会话。", 409)
        async with self.lock:
            self.store.delete(conversation_id)

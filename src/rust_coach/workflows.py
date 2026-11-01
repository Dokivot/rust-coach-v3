from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from rust_coach.contracts import ModelMessage, ModelReply
from rust_coach.llm import ChatModel


class ChatState(TypedDict):
    messages: list[ModelMessage]
    reply: ModelReply


def build_chat_graph(model: ChatModel):
    async def chat(state: ChatState):
        return {"reply": await model.complete(state["messages"])}

    builder = StateGraph(ChatState)
    builder.add_node("chat", chat)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    return builder.compile()

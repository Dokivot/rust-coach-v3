from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from rust_coach.contracts import ModelMessage, ModelReply
from rust_coach.llm import ChatModel
from rust_coach.calculations.service import CalculationService


class ChatState(TypedDict):
    messages: list[ModelMessage]
    reply: ModelReply
    previous_calculation: dict | None
    pending_message: str | None
    calculation: dict | None
    route: str


def build_chat_graph(model: ChatModel, instruction_role="system", calculation_model: ChatModel | None = None):
    service = CalculationService(calculation_model or model, instruction_role)

    def route(state: ChatState):
        previous = state.get("previous_calculation")
        return {"route": "calculate" if service.matches(state["messages"][-1].content, previous,
                                                         state.get("pending_message")) else "chat"}

    async def calculate(state: ChatState):
        result = await service.run(state["messages"], state.get("previous_calculation"), state.get("pending_message"))
        return {"route": "done", **result}

    async def chat(state: ChatState):
        return {"reply": await model.complete(state["messages"]), "calculation": None}

    builder = StateGraph(ChatState)
    builder.add_node("chat", chat)
    builder.add_node("route", route)
    builder.add_node("calculate", calculate)
    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", lambda state: state["route"], {"chat": "chat", "calculate": "calculate"})
    builder.add_conditional_edges("calculate", lambda state: state["route"], {"chat": "chat", "done": END})
    builder.add_edge("chat", END)
    return builder.compile()

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from rust_coach.contracts import ModelMessage, ModelReply
from rust_coach.llm import ChatModel
from rust_coach.calculations.service import CalculationService
from rust_coach.player_state.service import render_snapshot
from rust_coach.advice.service import is_advice


class ChatState(TypedDict):
    messages: list[ModelMessage]
    reply: ModelReply
    previous_calculation: dict | None
    pending_message: str | None
    calculation: dict | None
    route: str
    player_snapshot: dict | None
    inventory_container: str | None
    previous_advice: dict | None
    advice: dict | None


def build_chat_graph(model: ChatModel, instruction_role="system", calculation_model: ChatModel | None = None, advice_service=None):
    service = CalculationService(calculation_model or model, instruction_role)

    def route(state: ChatState):
        if state["messages"][-1].content.strip() in {"/state", "查看我的状态", "查看玩家状态"}:
            return {"route": "player_state"}
        if advice_service and is_advice(state["messages"][-1].content, state.get("previous_advice")):
            return {"route": "advice"}
        previous = state.get("previous_calculation")
        return {"route": "calculate" if service.matches(state["messages"][-1].content, previous,
                                                         state.get("pending_message")) else "chat"}

    async def calculate(state: ChatState):
        result = await service.run(state["messages"], state.get("previous_calculation"), state.get("pending_message"),
                                   state.get("player_snapshot"), state.get("inventory_container"))
        return {"route": "done", **result}

    async def chat(state: ChatState):
        return {"reply": await model.complete(state["messages"]), "calculation": None}

    def player_state(state: ChatState):
        return {"reply": ModelReply(content=render_snapshot(state.get("player_snapshot"))), "calculation": None}

    async def advice(state: ChatState):
        previous = state.get("previous_advice") or {}
        return await advice_service.run(state["messages"][-1].content, state.get("player_snapshot"),
                                        state.get("inventory_container"), previous.get("request"),
                                        previous.get("answer"))

    builder = StateGraph(ChatState)
    builder.add_node("chat", chat)
    builder.add_node("route", route)
    builder.add_node("calculate", calculate)
    builder.add_node("player_state", player_state)
    builder.add_node("advice", advice)
    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", lambda state: state["route"], {"chat": "chat", "calculate": "calculate", "player_state": "player_state", "advice": "advice"})
    builder.add_conditional_edges("calculate", lambda state: state["route"], {"chat": "chat", "done": END})
    builder.add_edge("chat", END)
    builder.add_edge("player_state", END)
    builder.add_edge("advice", END)
    return builder.compile()

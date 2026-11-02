import json
from uuid import uuid4

from rust_coach.advice.service import AdviceService
from rust_coach.contracts import ModelReply
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime
from rust_coach.config import Settings


PLAN = {
    "goal": "比较当前行动",
    "topics": [],
    "evidence_reason": "先核对当前状态",
    "candidates": [{"id": "check", "title": "核对当前状态", "kind": "observe",
                    "goals": {}, "required_fields": []}],
}
ANSWER = {
    "recommendation": {"candidate_id": "check", "reason": "先确认当前条件",
                        "tradeoff": "暂缓其他行动", "prerequisites": ["能查看当前状态"],
                        "steps": ["打开玩家状态并记录变化"], "completion": "已记录当前状态",
                        "switch_when": "若条件变化则重新评估", "citations": [], "visual_citations": []},
    "alternative": None, "missing": [], "uncertainty": "当前建议只基于已提供的条件",
}


class ScriptedModel:
    def __init__(self, outputs=()):
        self.outputs = list(outputs)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        if self.outputs:
            value = self.outputs.pop(0)
            return ModelReply(content=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        return ModelReply(content="普通回复")


async def test_calculation_vertical_slice_has_all_layer_artifacts(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "calc.sqlite3", api_key="test")
    store = ConversationStore(settings.database_path)
    conversation = store.create().id
    model = ScriptedModel()
    runtime = ChatRuntime(settings, model, store)
    request = {"operation": "craft", "goals": {"explosive.timed": 1}}
    turn = await runtime.chat(conversation, __import__("rust_coach.contracts", fromlist=["ChatRequest"]).ChatRequest(
        request_id=uuid4(), message="/calc " + json.dumps(request)))
    architecture = turn.presentation["architecture"]
    assert architecture["task"]["route"] == "simple_call"
    assert architecture["decision_result"]["result_kind"] == "calculation"
    assert architecture["answer_spec"]["kind"] == "calculation"
    layers = {event["layer"] for event in architecture["trace"]["events"]}
    assert {"input", "understanding", "decision", "generation"} <= layers


async def test_advice_vertical_slice_uses_plan_execute_and_hides_plan(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "advice.sqlite3", api_key="test")
    store = ConversationStore(settings.database_path)
    conversation = store.create().id
    model = ScriptedModel([PLAN, ANSWER])
    class Research:
        async def retrieve(self, topics, force=False):
            return _empty_evidence()

    advice = AdviceService(model, research=Research())
    runtime = ChatRuntime(settings, model, store, advice_service=advice)
    from rust_coach.contracts import ChatRequest
    turn = await runtime.chat(conversation, ChatRequest(request_id=uuid4(), message="/coach 现在该先做什么"))
    architecture = turn.presentation["architecture"]
    assert architecture["task"]["route"] == "plan_execute"
    assert architecture["decision_result"]["result_kind"] == "recommendation"
    assert architecture["answer_spec"]["kind"] == "advice"
    assert "execution_plan" not in json.dumps(architecture["answer_spec"], ensure_ascii=False)
    assert any(event["layer"] == "decision" for event in architecture["trace"]["events"])


def _empty_evidence():
    return {"documents": [], "events": [{"type": "skipped"}], "warnings": [], "queries": [], "cache_hit": False}

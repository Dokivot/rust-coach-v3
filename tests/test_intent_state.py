import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelReply
from rust_coach.persistence import ConversationStore
from rust_coach.player_state.domain import calculation_inventory
from rust_coach.player_state.schemas import ScopeCreate, StateUpdate
from rust_coach.player_state.working import explicit_event
from rust_coach.runtime import ChatRuntime
from rust_coach.advice.service import AdviceService


class Model:
    def __init__(self, outputs=()):
        self.outputs = list(outputs)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        if self.outputs:
            value = self.outputs.pop(0)
            if isinstance(value, Exception):
                raise value
            return ModelReply(content=json.dumps(value) if isinstance(value, dict) else value)
        return ModelReply(content="可以继续提问。")


def setup_runtime(tmp_path, model=None):
    settings = Settings(_env_file=None, api_key="test", search_provider="off", database_path=tmp_path / "intent.sqlite3")
    store = ConversationStore(settings.database_path)
    runtime = ChatRuntime(settings, model or Model(), store)
    owner = store.create().id
    scope = runtime.player_states.create(ScopeCreate(server_id="server-a", wipe_id="wipe-a", label="当前服务器"))["scope"]["id"]
    runtime.player_states.bind(owner, scope)
    runtime.player_states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=0,
        observed_at=datetime.now(timezone.utc), changes={"facts": {"technology.workbench": 3, "base.has_base": True},
        "inventory": [{"container_id": container, "mode": "replace", "complete": True, "items": {"sulfur": amount}}
                      for container, amount in [("backpack", 1000), ("storage", 5000)]]}))
    return runtime, owner, scope


async def send(runtime, owner, message, **kwargs):
    return await runtime.chat(owner, ChatRequest(request_id=uuid4(), message=message, **kwargs))


@pytest.mark.parametrize("message,event", [
    ("我刚死了，背包丢了", "death"), ("我死了，按背包算还能造多少？", "death"),
    ("我的背包丢了", "backpack_lost"), ("我家刚被抄了", "raided"),
    ("如果我死了，背包会丢吗？", None), ("昨天我死了", None),
    ("队友说我死了", None), ("我没死", None), ("我刚死了吗？", None),
    ("解释‘我死了’这句话", None), ("我死了才怪", None),
])
def test_only_explicit_current_self_events_write(message, event):
    assert explicit_event(message) == event


async def test_death_is_durable_empty_not_unknown_and_history_survives(tmp_path):
    runtime, owner, scope = setup_runtime(tmp_path)
    request = ChatRequest(request_id=uuid4(), message="我刚死了，背包丢了")
    turn = await runtime.chat(owner, request)
    current = runtime.player_states.get(scope)
    assert current["revision"] == 2
    assert calculation_inventory(current, "backpack") == {}
    assert calculation_inventory(current, "storage") == {"sulfur": 5000}
    assert current["retired_containers"]["backpack"]["invalidated_by"] == "death"
    assert current["retired_containers"]["backpack"]["items"]["sulfur"]["value"] == 1000
    assert runtime.player_states.get(scope, revision=1)["containers"]["backpack"]["items"]["sulfur"]["value"] == 1000
    assert "清零" in turn.assistant
    assert turn.presentation["architecture"]["state_receipt"]["revision"] == 2
    assert runtime.graph and runtime.player_states.get(scope)["facts"]["base.has_base"]["value"] is True
    assert await runtime.chat(owner, request) == turn
    assert runtime.player_states.get(scope)["revision"] == 2
    restored = ChatRuntime(runtime.settings, Model(), ConversationStore(runtime.settings.database_path))
    assert calculation_inventory(restored.player_states.get(scope), "backpack") == {}


async def test_event_survives_generation_failure_and_retry_does_not_clear_new_stock(tmp_path):
    model = Model([AppError("model_unavailable", "测试模型不可用", 503), {"operation": "craft", "goals": {"explosive.timed": 1}}])
    runtime, owner, scope = setup_runtime(tmp_path, model)
    request = ChatRequest(request_id=uuid4(), message="我刚死了，按背包算制作一个C4")
    with pytest.raises(AppError, match="测试模型不可用") as failed:
        await runtime.chat(owner, request)
    assert "状态事件已保存" in failed.value.message
    assert calculation_inventory(runtime.player_states.get(scope), "backpack") == {}
    runtime.player_states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=2,
        observed_at=datetime.now(timezone.utc), changes={"inventory": [
            {"container_id": "backpack", "mode": "replace", "complete": True, "items": {"sulfur": 200}}]}))
    result = await runtime.chat(owner, request)
    assert result.calculation["input"]["inventory"] == {"sulfur": 200}
    assert runtime.player_states.get(scope)["revision"] == 3
    assert result.presentation["architecture"]["state_receipt"]["revision"] == 2


async def test_failed_event_request_id_cannot_be_reused_for_other_text(tmp_path):
    runtime, owner, scope = setup_runtime(tmp_path, Model([AppError("model_unavailable", "失败", 503)]))
    identifier = uuid4()
    with pytest.raises(AppError):
        await runtime.chat(owner, ChatRequest(request_id=identifier, message="我刚死了，制作一个C4"))
    with pytest.raises(AppError) as conflict:
        await runtime.chat(owner, ChatRequest(request_id=identifier, message="你好"))
    assert conflict.value.code == "request_conflict"
    assert runtime.player_states.get(scope)["revision"] == 2


async def test_task_switch_followup_survives_chat_and_restart(tmp_path):
    runtime, owner, scope = setup_runtime(tmp_path)
    craft = await send(runtime, owner, '/calc {"operation":"craft","goals":{"explosive.timed":10}}', player_inventory_container="backpack")
    raid = await send(runtime, owner, "改成拆一面石墙最省资源")
    assert raid.calculation["input"]["goals"] == {}
    assert raid.calculation["input"]["inventory"] == {"sulfur": 1000}
    assert raid.presentation["architecture"]["task"]["action"] == "switch"
    assert raid.presentation["architecture"]["task"]["task_id"] != craft.presentation["architecture"]["task"]["task_id"]
    greeting = await send(runtime, owner, "你好")
    assert greeting.presentation["architecture"]["task"]["route"] == "chat"
    restored = ChatRuntime(runtime.settings, Model(), ConversationStore(runtime.settings.database_path))
    hp = await send(restored, owner, "它还剩250血")
    architecture = hp.presentation["architecture"]
    assert hp.calculation["input"]["hp"] == 250
    assert architecture["task"]["task_id"] == raid.presentation["architecture"]["task"]["task_id"]
    assert architecture["task"]["understanding_revision"] == 2
    assert {event["task_id"] for event in architecture["trace"]["events"]} == {architecture["task"]["task_id"]}
    assert runtime.player_states.get(scope)["revision"] == 1


async def test_cancel_discards_task_and_orphan_health_requests_target(tmp_path):
    runtime, owner, _ = setup_runtime(tmp_path)
    await send(runtime, owner, "拆一面石墙最省资源")
    cancelled = await send(runtime, owner, "不算了")
    assert cancelled.presentation["architecture"]["working_state"] is None
    result = await send(runtime, owner, "它还剩250血")
    assert result.calculation["status"] == "needs_input"
    assert "明确" in result.assistant


async def test_state_query_has_one_authoritative_route_and_no_model_call(tmp_path):
    model = Model()
    runtime, owner, _ = setup_runtime(tmp_path, model)
    result = await send(runtime, owner, "查看我的状态")
    architecture = result.presentation["architecture"]
    assert architecture["task"]["route"] == "state"
    assert architecture["task"]["execution_node"] == "player_state"
    assert architecture["decision_request"] is None
    assert architecture["decision_result"]["result_kind"] == "state"
    assert model.calls == []


async def test_explicit_craft_question_does_not_enter_advice(tmp_path):
    model = Model([{"operation": "craft", "goals": {"explosive.timed": 1}}])
    runtime, owner, _ = setup_runtime(tmp_path, model)
    runtime = ChatRuntime(runtime.settings, model, runtime.store, advice_service=AdviceService(model, None))
    result = await send(runtime, owner, "如何制作一个C4")
    assert result.calculation["status"] == "calculated"
    assert result.advice is None
    assert len(model.calls) == 1


async def test_model_cannot_invent_inventory_from_goal_quantity(tmp_path):
    fabricated = {"operation": "craft", "goals": {"explosive.timed": 10}, "inventory": {"sulfur": 10}}
    model = Model([fabricated, fabricated])
    runtime, owner, scope = setup_runtime(tmp_path, model)
    result = await send(runtime, owner, "制作10个C4需要多少硫磺")
    assert result.calculation["status"] == "needs_input"
    assert "result" not in result.calculation
    assert len(model.calls) == 2
    assert runtime.player_states.get(scope)["revision"] == 1


async def test_partial_self_report_is_not_promoted_to_complete_inventory(tmp_path):
    model = Model([{"operation": "craft", "goals": {"explosive.timed": 1}, "inventory": {"sulfur": 100}, "inventory_complete": True}])
    runtime, owner, _ = setup_runtime(tmp_path, model)
    result = await send(runtime, owner, "我有100硫磺，制作一个C4还差多少")
    assert result.calculation["input"]["inventory"] == {"sulfur": 100}
    assert result.calculation["input"]["inventory_complete"] is False
    assert result.presentation["architecture"]["decision_result"]["status"] == "conditional"


def test_http_new_conversation_reuses_scope_and_death_across_sessions(tmp_path):
    settings = Settings(_env_file=None, api_key="test", search_provider="off", database_path=tmp_path / "http.sqlite3")
    app = create_app(settings, Model())
    with TestClient(app) as client:
        first = client.post("/api/conversations", json={}).json()["id"]
        scope = app.state.runtime.player_states.create(ScopeCreate(server_id="a", wipe_id="w", label="演示"))["scope"]["id"]
        app.state.runtime.player_states.bind(first, scope)
        app.state.runtime.player_states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=0,
            observed_at=datetime.now(timezone.utc), changes={"inventory": [
                {"container_id": "backpack", "mode": "replace", "complete": True, "items": {"sulfur": 1000}}]}))
        second = client.post("/api/conversations", json={}).json()["id"]
        assert app.state.runtime.player_states.binding(second) == scope
        response = client.post(f"/api/conversations/{first}/messages", headers={"accept": "text/event-stream"},
            json={"request_id": str(uuid4()), "message": "我刚死了，背包丢了"})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert events[-1]["type"] == "result"
        assert "清零" in events[-1]["turn"]["assistant"]
        calculation = client.post(f"/api/conversations/{second}/messages", json={"request_id": str(uuid4()),
            "message": '/calc {"operation":"craft","goals":{"explosive.timed":1}}', "player_inventory_container": "backpack"})
        assert calculation.status_code == 200, calculation.text
        assert calculation.json()["calculation"]["input"]["inventory"] == {}
        assert calculation.json()["player_state_ref"]["revision"] == 2


async def test_no_scope_event_has_explicit_error_and_no_fabricated_server(tmp_path):
    runtime, owner, _ = setup_runtime(tmp_path)
    await runtime.bind_player_state(owner, None)
    with pytest.raises(AppError) as error:
        await send(runtime, owner, "我刚死了，背包丢了")
    assert error.value.code == "player_scope_required"
    assert len(runtime.player_states.list_scopes()) == 1


async def test_natural_server_change_detaches_scope_without_destroying_profile(tmp_path):
    runtime, owner, scope = setup_runtime(tmp_path)
    await send(runtime, owner, "拆一面石墙最省资源")
    request = ChatRequest(request_id=uuid4(), message="我换服了，继续算C4")
    result = await runtime.chat(owner, request)
    assert result.calculation is None
    assert "选择新服务器" in result.assistant
    assert runtime.player_states.binding(owner) is None
    assert calculation_inventory(runtime.player_states.get(scope), "backpack") == {"sulfur": 1000}
    assert await runtime.chat(owner, request) == result
    followup = await send(runtime, owner, "它还剩250血")
    assert followup.calculation["status"] == "needs_input"


async def test_current_facts_are_used_without_persistent_overwrite(tmp_path):
    runtime, owner, scope = setup_runtime(tmp_path)
    result = await send(runtime, owner, "我有1级工作台，先说明本轮条件")
    view = result.presentation["architecture"]["state_view"]
    assert view["facts"]["technology.workbench"]["value"] == 1
    assert view["conflicts"][0]["profile_value"] == 3
    assert runtime.player_states.get(scope)["facts"]["technology.workbench"]["value"] == 3
    assert "档案未覆盖" in result.assistant


async def test_concurrent_state_change_rejects_stale_calculation(tmp_path):
    runtime, owner, scope = setup_runtime(tmp_path)

    class UpdatingModel:
        async def complete(self, messages):
            runtime.player_states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=1,
                observed_at=datetime.now(timezone.utc), changes={"event": "death"}))
            return ModelReply(content='{"operation":"craft","goals":{"explosive.timed":1}}')

    runtime = ChatRuntime(runtime.settings, UpdatingModel(), runtime.store)
    with pytest.raises(AppError) as error:
        await send(runtime, owner, "用当前材料制作一个C4", player_inventory_container="backpack")
    assert error.value.code == "player_state_changed"
    assert runtime.store.get(owner).turns == []
    assert calculation_inventory(runtime.player_states.get(scope), "backpack") == {}


async def test_new_scope_does_not_resume_old_task_or_temporary_facts(tmp_path):
    runtime, owner, _ = setup_runtime(tmp_path)
    await send(runtime, owner, "我有1级工作台")
    await send(runtime, owner, "拆一面石墙最省资源")
    second = runtime.player_states.create(ScopeCreate(server_id="other", wipe_id="wipe-b", label="另一个服"))["scope"]["id"]
    await runtime.bind_player_state(owner, second)
    result = await send(runtime, owner, "它还剩250血")
    assert result.calculation["status"] == "needs_input"
    assert result.presentation["architecture"]["state_view"]["temporary_facts"] == {}
    assert result.presentation["architecture"]["task"]["previous_task_id"] is None


async def test_advice_evaluation_uses_current_self_report_and_preserves_profile(tmp_path):
    plan = {"goal": "核对当前行动", "topics": [], "evidence_reason": "本轮状态检查", "candidates": [
        {"id": "check", "title": "核对基地", "kind": "observe", "goals": {},
         "required_fields": ["base.has_base", "session.online_minutes"]}]}
    answer = {"recommendation": {"candidate_id": "check", "reason": "先核对当前基地情况", "tradeoff": "暂缓外出",
        "prerequisites": ["可以查看基地"], "steps": ["检查入口"], "completion": "已确认入口情况",
        "switch_when": "条件改变时重新评估", "citations": []}, "alternative": None, "missing": [], "uncertainty": "依据玩家自报"}
    model = Model([plan, answer])
    runtime, owner, scope = setup_runtime(tmp_path, model)
    runtime = ChatRuntime(runtime.settings, model, runtime.store, advice_service=AdviceService(model, None))
    result = await send(runtime, owner, "/coach 我已经有基地，我还能在线20分钟，建议下一步")
    assert result.advice["evaluations"]["check"]["missing_fields"] == []
    assert "session.online_minutes" not in runtime.player_states.get(scope)["facts"]
    view = result.presentation["architecture"]["state_view"]
    assert view["temporary_facts"]["session.online_minutes"]["value"] == 20


@pytest.mark.parametrize("message", ["队友有3级工作台", "如果我有3级工作台", "昨天我有3级工作台", "我又捡了100硫磺"])
async def test_questions_history_and_deltas_do_not_become_current_facts(tmp_path, message):
    runtime, owner, scope = setup_runtime(tmp_path)
    result = await send(runtime, owner, message)
    assert result.presentation["architecture"]["state_view"]["temporary_facts"] == {}
    assert runtime.player_states.get(scope)["revision"] == 1

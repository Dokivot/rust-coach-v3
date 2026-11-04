import json
from uuid import uuid4

import pytest

from rust_coach.contracts import AppError, ModelReply
from rust_coach.player_state.schemas import ScopeCreate
from rust_coach.player_state.schemas import StateUpdate
from datetime import datetime, timezone
from rust_coach.player_state.working import explicit_event, text_facts
from test_intent_state import Model, send, setup_runtime
from test_vision import setup, upload, analyze


@pytest.mark.parametrize("message", ["我有3级工作台就好了", "我有3级工作台吗", "我已经有基地了吗", "我还能在线20分钟就好了"])
def test_non_assertions_never_become_temporary_facts(message):
    assert text_facts(message) == {}


@pytest.mark.parametrize("message", ["我死了，不对，我没死", "我的背包丢了，刚才说错了", "我死了，背包没丢"])
def test_contested_loss_never_clears_inventory(message):
    assert explicit_event(message) is None


async def test_negated_cancellation_keeps_task_and_health_followup(tmp_path):
    runtime, owner, _ = setup_runtime(tmp_path)
    first = await send(runtime, owner, "拆一面石墙最省资源")
    await send(runtime, owner, "不要取消计算")
    result = await send(runtime, owner, "它还剩250血")
    assert result.calculation["status"] == "calculated"
    assert result.calculation["input"]["hp"] == 250
    assert result.presentation["architecture"]["task"]["task_id"] == first.presentation["architecture"]["task"]["task_id"]


@pytest.mark.parametrize("message", [
    "对手有100硫磺，我制作一个C4还差多少", "我没有100硫磺，制作一个C4还差多少",
    "如果我有100硫磺，制作一个C4还差多少", "昨天我有100硫磺，制作一个C4还差多少",
])
async def test_model_cannot_turn_non_current_counts_into_inventory(tmp_path, message):
    fabricated = {"operation": "craft", "goals": {"explosive.timed": 1}, "inventory": {"sulfur": 100}}
    runtime, owner, _ = setup_runtime(tmp_path, Model([fabricated, fabricated]))
    result = await send(runtime, owner, message)
    assert result.calculation["status"] == "needs_input"
    assert not result.calculation.get("result")


async def test_partial_report_cannot_refresh_complete_inventory_by_equal_value(tmp_path):
    parsed = {"operation": "craft", "goals": {"explosive.timed": 1}, "inventory": {"sulfur": 100}, "inventory_complete": True}
    runtime, owner, _ = setup_runtime(tmp_path, Model([parsed]))
    await send(runtime, owner, '/calc {"operation":"craft","goals":{"explosive.timed":1},"inventory":{"sulfur":100}}')
    result = await send(runtime, owner, "我有100硫磺，继续算")
    assert result.calculation["input"]["inventory_complete"] is False


async def test_total_only_policy_discards_parser_inventory(tmp_path):
    parsed = {"operation": "craft", "goals": {"explosive.timed": 1}, "inventory": {"sulfur": 100}}
    runtime, owner, _ = setup_runtime(tmp_path, Model([parsed]))
    result = await send(runtime, owner, "我有100硫磺，但只算总需求，制作一个C4")
    assert result.calculation["input"]["inventory"] is None
    assert result.calculation["result"]["inventory_used"] == {}


async def test_scope_rebinding_during_generation_prevents_old_scope_answer(tmp_path):
    runtime, owner, _ = setup_runtime(tmp_path)
    other = runtime.player_states.create(ScopeCreate(server_id="b", wipe_id="b", label="新档案"))["scope"]["id"]

    class RebindingModel:
        async def complete(self, messages):
            runtime.player_states.bind(owner, other)
            return ModelReply(content=json.dumps({"operation": "craft", "goals": {"explosive.timed": 1}}))

    runtime.graph = type(runtime)(runtime.settings, RebindingModel(), runtime.store).graph
    with pytest.raises(AppError) as error:
        await send(runtime, owner, "制作一个C4", player_inventory_container="backpack")
    assert error.value.code == "player_scope_changed"
    assert runtime.store.get(owner).turns == []


@pytest.mark.parametrize("message", ["如果硫磺数量改为150会怎样？", "不要把硫磺数量改为150"])
def test_hypothetical_visual_correction_is_not_applied(setup, message):
    client, application, model, owner = setup
    model.task = "craft"
    first = analyze(client, owner, upload(client, owner))
    response = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()), "message": message})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "visual_correction_unclear"
    original = application.state.vision.drafts.get(owner, first["visual"]["draft_id"])
    assert original["payload"]["automatic"]["inventory"] == {"sulfur": 100}


def test_multiple_visual_edits_are_not_silently_partially_applied(setup):
    client, _, model, owner = setup
    model.task = "craft"
    analyze(client, owner, upload(client, owner))
    response = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()),
        "message": "硫磺数量改为150，硫磺数量改为200"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "visual_correction_ambiguous"


async def test_parser_cannot_invent_technology_to_override_profile(tmp_path):
    invented = {"operation": "craft", "goals": {"explosive.timed": 1}, "workbench": 3,
                "blueprints": ["explosive.timed", "explosives"]}
    runtime, owner, scope = setup_runtime(tmp_path, Model([invented, invented]))
    runtime.player_states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=1,
        observed_at=datetime.now(timezone.utc), changes={"facts": {"technology.workbench": 1}}))
    result = await send(runtime, owner, "制作一个C4")
    assert result.calculation["status"] == "needs_input"
    assert not result.calculation.get("result")


def test_explicit_profile_selection_with_new_image_resolves_sources(setup):
    client, app, model, owner = setup
    model.task = "craft"
    scope = app.state.runtime.player_states.create(ScopeCreate(server_id="a", wipe_id="w", label="档案"))["scope"]["id"]
    app.state.runtime.player_states.bind(owner, scope)
    app.state.runtime.player_states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=0,
        observed_at=datetime.now(timezone.utc), changes={"inventory": [
            {"container_id": "backpack", "mode": "replace", "complete": True, "items": {"sulfur": 200}}]}))
    result = analyze(client, owner, upload(client, owner), message="只用档案背包，截图材料制作火药")
    assert result["calculation"]["status"] == "calculated"
    assert result["calculation"]["input"]["inventory"] == {"sulfur": 200}
    assert result["calculation"]["input_usage"]["inventory_source"] == "profile"
    followup = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()),
        "message": "改成拆一面石墙最省资源"}).json()
    assert followup["calculation"]["input"]["inventory"] == {"sulfur": 200}


async def test_arithmetic_with_unknown_inventory_is_conditional(tmp_path):
    runtime, owner, _ = setup_runtime(tmp_path)
    result = await send(runtime, owner, '/calc {"operation":"craft","goals":{"explosive.timed":1}}')
    assert result.presentation["architecture"]["decision_result"]["status"] == "conditional"

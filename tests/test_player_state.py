import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rust_coach.app import create_app
from rust_coach.calculations.service import CalculationService
from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelMessage, ModelReply
from rust_coach.persistence import ConversationStore
from rust_coach.player_state import PlayerStateStore
from rust_coach.player_state.domain import calculation_inventory, model_context
from rust_coach.player_state.schemas import ScopeCreate, StateChanges, StateUpdate
from rust_coach.runtime import ChatRuntime


NOW = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    return PlayerStateStore(ConversationStore(tmp_path / "state.sqlite3"))


def new_scope(store, server="server-a", wipe="wipe-1"):
    return store.create(ScopeCreate(server_id=server, wipe_id=wipe, label=server + wipe))["scope"]["id"]


def observation(revision, changes, at=NOW, identifier=None):
    return StateUpdate(observation_id=identifier or uuid4(), expected_revision=revision,
                       observed_at=at, changes=changes)


def inventory(items, complete=False, container="backpack"):
    return {"inventory": [{"container_id": container, "mode": "replace" if complete else "patch",
                           "complete": complete, "items": items}]}


def test_unknown_zero_partial_complete_and_owned_containers(store):
    scope = new_scope(store)
    empty = store.get(scope, now=NOW)
    assert empty["facts"] == empty["containers"] == {}
    assert "technology.workbench" in empty["unknown_fields"]
    partial = store.update(scope, observation(0, inventory({"sulfur": 1000})), now=NOW)
    assert partial["containers"]["backpack"]["coverage"] == "partial"
    with pytest.raises(AppError) as error:
        calculation_inventory(partial, "backpack")
    assert error.value.code == "player_inventory_incomplete"
    complete = store.update(scope, observation(1, inventory({"sulfur": 0}, True)), now=NOW)
    assert calculation_inventory(complete, "backpack") == {"sulfur": 0}
    stored = store.update(scope, observation(2, inventory({"sulfur": 999}, True, "storage")), now=NOW)
    assert calculation_inventory(stored, "backpack") == {"sulfur": 0}
    assert calculation_inventory(stored, "storage") == {"sulfur": 999}
    cleared = store.update(scope, observation(3, inventory({}, True)), now=NOW)
    assert calculation_inventory(cleared, "backpack") == {}
    assert calculation_inventory(cleared, "storage") == {"sulfur": 999}


def test_patch_does_not_refresh_unmentioned_items_and_downgrades_coverage(store):
    scope = new_scope(store)
    store.update(scope, observation(0, inventory({"sulfur": 1000, "cloth": 50}, True)), now=NOW)
    later = NOW + timedelta(minutes=35)
    state = store.update(scope, observation(1, inventory({"sulfur": 800}), at=later), now=later)
    container = state["containers"]["backpack"]
    assert container["items"]["cloth"]["value"] == 50
    assert container["items"]["cloth"]["status"] == "stale"
    assert container["items"]["sulfur"]["status"] == "fresh"
    assert container["coverage"] == "partial"
    assert container["usable_for_exact_calculation"] is False
    context = model_context(state)
    assert "cloth" not in context["containers"]["backpack"]["items"]


def test_fact_patch_expiry_clear_and_provenance(store):
    scope = new_scope(store)
    first = store.update(scope, observation(0, {"facts": {"technology.workbench": 0, "base.has_base": False,
                                                           "session.online_minutes": 20}}), now=NOW)
    assert first["facts"]["technology.workbench"]["value"] == 0
    assert first["facts"]["base.has_base"]["value"] is False
    later = NOW + timedelta(minutes=31)
    second = store.update(scope, observation(1, {"facts": {"goals.primary": "先发展科技"}}, at=later), now=later)
    assert second["facts"]["session.online_minutes"]["status"] == "stale"
    assert second["facts"]["technology.workbench"]["observed_at"] == first["facts"]["technology.workbench"]["observed_at"]
    assert second["facts"]["goals.primary"]["verification"] == "user_reported"
    cleared = store.update(scope, observation(2, {"clear_fields": ["base.has_base"]}, at=later), now=later)
    assert cleared["facts"]["base.has_base"]["status"] == "invalidated"
    assert "base.has_base" not in model_context(cleared)["facts"]
    assert "base.has_base" in model_context(cleared)["unavailable_fields"]


@pytest.mark.parametrize("event", ["death", "raided", "blueprint_reset", "invalidate_all"])
def test_domain_events_only_invalidate_relevant_state(store, event):
    scope = new_scope(store)
    changes = {"facts": {"technology.workbench": 2, "technology.unlocked_blueprints": ["explosive.timed"],
                          "base.has_base": True, "session.location": "D4", "preferences.risk": "cautious"},
               "inventory": [inventory({"sulfur": 100}, True, container)["inventory"][0]
                             for container in ["backpack", "equipped", "storage"]]}
    store.update(scope, observation(0, changes), now=NOW)
    state = store.update(scope, observation(1, {"event": event}), now=NOW)
    assert state["facts"]["preferences.risk"]["status"] == ("invalidated" if event == "invalidate_all" else "fresh")
    for container in ["backpack", "equipped", "storage"]:
        if event == "death" and container == "backpack":
            assert calculation_inventory(state, container) == {}
            assert state["retired_containers"][container]["invalidated_by"] == "death"
            assert state["retired_containers"][container]["items"]["sulfur"]["value"] == 100
            continue
        invalid = event in {"raided", "invalidate_all"} or event == "death" and container != "storage"
        assert state["containers"][container]["status"] == ("invalidated" if invalid else "fresh")
        assert state["containers"][container]["items"]["sulfur"]["value"] == 100
    assert state["facts"]["technology.unlocked_blueprints"]["status"] == (
        "invalidated" if event in {"blueprint_reset", "invalidate_all"} else "fresh")


def test_replay_conflict_atomicity_and_history(store):
    scope = new_scope(store)
    first_request = observation(0, {"facts": {"session.team_size": 2}})
    first = store.update(scope, first_request, now=NOW)
    assert store.update(scope, first_request, now=NOW) == first
    with pytest.raises(AppError) as conflict:
        store.update(scope, observation(0, {"facts": {"session.team_size": 3}}, identifier=first_request.observation_id), now=NOW)
    assert conflict.value.code == "observation_conflict"
    with pytest.raises(AppError) as conflict:
        store.update(scope, observation(0, {"facts": {"base.has_base": True}}), now=NOW)
    assert conflict.value.code == "state_revision_conflict"
    with pytest.raises(AppError) as conflict:
        store.update(scope, observation(1, {"facts": {"session.teammates_online": 2, "base.has_base": True}}), now=NOW)
    assert conflict.value.code == "state_conflict"
    assert store.get(scope, now=NOW) == first
    second = store.update(scope, observation(1, {"facts": {"session.teammates_online": 1}}), now=NOW)
    restored = PlayerStateStore(ConversationStore(store.store.path))
    assert restored.get(scope, now=NOW) == second
    assert restored.get(scope, revision=1, now=NOW) == first
    assert restored.get(scope, revision=0, now=NOW)["facts"] == {}
    assert store.update(scope, first_request, now=NOW)["revision"] == 1
    assert store.get(scope, now=NOW)["revision"] == 2


def test_backdated_and_future_observations_cannot_overwrite(store):
    scope = new_scope(store)
    store.update(scope, observation(0, {"facts": {"technology.workbench": 2}}), now=NOW)
    for at, expected in [(NOW - timedelta(minutes=1), "older_observation"), (NOW + timedelta(hours=1), "future_observation")]:
        with pytest.raises(AppError) as error:
            store.update(scope, observation(1, {"facts": {"technology.workbench": 1}}, at=at), now=NOW)
        assert error.value.code == expected
    assert store.get(scope, now=NOW)["facts"]["technology.workbench"]["value"] == 2


def test_events_block_older_unseen_observations_and_allow_fresh_reconfirmation(store):
    scope = new_scope(store)
    store.update(scope, observation(0, {"event": "death"}), now=NOW)
    with pytest.raises(AppError) as error:
        store.update(scope, observation(1, inventory({"sulfur": 500}, True), at=NOW - timedelta(minutes=1)), now=NOW)
    assert error.value.code == "older_observation"
    fresh = store.update(scope, observation(1, inventory({"sulfur": 20}, True), at=NOW + timedelta(seconds=1)), now=NOW)
    assert calculation_inventory(fresh, "backpack") == {"sulfur": 20}


def test_scope_isolation_binding_and_conversation_delete(store):
    first = new_scope(store)
    other_wipe = new_scope(store, wipe="wipe-2")
    other_server = new_scope(store, server="server-b")
    store.update(first, observation(0, {"facts": {"technology.workbench": 3}}), now=NOW)
    assert store.get(other_wipe, now=NOW)["facts"] == {}
    assert store.get(other_server, now=NOW)["facts"] == {}
    conversation = store.store.create().id
    store.bind(conversation, first)
    assert store.binding(conversation) == first
    store.bind(conversation, other_wipe)
    assert store.binding(conversation) == other_wipe
    store.bind(conversation, None)
    assert store.binding(conversation) is None
    store.bind(conversation, first)
    store.store.delete(conversation)
    assert store.get(first, now=NOW)["facts"]["technology.workbench"]["value"] == 3
    with store.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_player_scope").fetchone()[0] == 0
    with pytest.raises(AppError) as error:
        new_scope(store)
    assert error.value.code == "scope_exists"


@pytest.mark.parametrize("changes", [
    {"facts": {"arbitrary": 1}}, {"facts": {"technology.workbench": True}},
    {"facts": {"technology.workbench": "2"}}, {"facts": {"technology.workbench": 4}},
    {"facts": {"base.has_base": "false"}}, {"facts": {"goals.primary": "  "}},
    {"facts": {"technology.unlocked_blueprints": ["rifle.ak", "rifle.ak"]}},
    inventory({"sulfur": -1}), inventory({"sulfur": 1.5}), inventory({"sulfur": True}),
    inventory({"not an id": 2}), inventory({}),
    {"inventory": [{"mode": "patch", "complete": True, "items": {"sulfur": 1}}]},
    {"facts": {"base.has_base": True}, "clear_fields": ["base.has_base"]},
    {"event": "death", "facts": {"base.has_base": True}},
    {"facts": {"goals.primary": None}}, {},
])
def test_strict_state_schema(changes):
    with pytest.raises(ValidationError):
        StateChanges.model_validate(changes)


def test_timezone_required_and_expiration_does_not_modify_revision(store):
    with pytest.raises(ValidationError):
        observation(0, {"facts": {"technology.workbench": 1}}, at=datetime(2026, 11, 1))
    scope = new_scope(store)
    store.update(scope, observation(0, inventory({}, True)), now=NOW)
    assert store.get(scope, now=NOW + timedelta(minutes=29))["containers"]["backpack"]["usable_for_exact_calculation"]
    expired = store.get(scope, now=NOW + timedelta(minutes=30))
    assert expired["revision"] == 1
    assert not expired["containers"]["backpack"]["usable_for_exact_calculation"]


def test_context_has_budget_and_never_calls_truncated_inventory_complete(store):
    scope = new_scope(store)
    state = store.update(scope, observation(0, {"inventory": [
        inventory({f"item{index}": index for index in range(150)}, True, f"box{container}")["inventory"][0]
        for container in range(10)]}), now=NOW)
    context = model_context(state)
    assert len(json.dumps(context, ensure_ascii=False)) <= 12000
    assert context["context_truncated"] is True
    assert all(container["coverage"] == "partial" for container in context["containers"].values() if container["context_truncated"])


class EchoModel:
    def __init__(self):
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        return ModelReply(content="收到")


async def test_chat_reads_frozen_state_without_auto_updates_and_scope_history_leak(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    model = EchoModel()
    runtime = ChatRuntime(settings, model, store)
    first, second = new_scope(runtime.player_states), new_scope(runtime.player_states, wipe="wipe-2")
    now = datetime.now(timezone.utc)
    runtime.player_states.update(first, observation(0, {"facts": {"technology.workbench": 2}}, at=now))
    identifier = store.create().id
    await runtime.bind_player_state(identifier, first)
    request = ChatRequest(request_id=uuid4(), message="我现在只有一级台")
    reply = await runtime.chat(identifier, request)
    context = json.loads(model.calls[-1][-2].content.split("\n", 1)[1])["player_state"]
    assert context["facts"]["technology.workbench"]["value"] == 2
    assert reply.player_state_ref["revision"] == 1
    assert runtime.player_states.get(first)["revision"] == 1
    assert await runtime.chat(identifier, request) == reply
    await runtime.bind_player_state(identifier, second)
    await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="你好"))
    assert all(message.content != request.message for message in model.calls[-1])
    assert model.calls[-1][-1].content == "你好"
    assert store.get(identifier).turns[0].player_state_ref["scope_id"] == first


async def test_state_read_needs_no_model_and_calculation_inventory_is_explicit(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    model = EchoModel()
    runtime = ChatRuntime(settings, model, store)
    scope = new_scope(runtime.player_states)
    now = datetime.now(timezone.utc)
    snapshot = runtime.player_states.update(scope, observation(0, {
        "facts": {"technology.workbench": 3},
        **inventory({"explosives": 10, "sulfur": 1000, "scrap": 100}, True)}, at=now))
    identifier = store.create().id
    await runtime.bind_player_state(identifier, scope)
    state_turn = await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="/state"))
    assert "1000" in state_turn.assistant
    command = '/calc {"operation":"craft","goals":{"explosive.timed":2}}'
    bare = await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message=command))
    assert bare.calculation["result"]["inventory_known"] is False
    request = ChatRequest(request_id=uuid4(), message=command, player_inventory_container="backpack")
    result = await runtime.chat(identifier, request)
    assert result.calculation["result"]["acquisition"]["sulfur"] == 2300
    assert result.calculation["result"]["requirements"]["workbench_status"] == "satisfied"
    assert result.calculation["player_state_usage"]["excluded_items"] == ["scrap"]
    assert runtime.player_states.get(scope)["revision"] == snapshot["revision"]
    assert not model.calls
    with pytest.raises(AppError) as error:
        await runtime.chat(identifier, request.model_copy(update={"player_inventory_container": None}))
    assert error.value.code == "request_conflict"


async def test_natural_language_calculation_receives_selected_container_context(tmp_path):
    class InventoryAwareModel:
        async def complete(self, messages):
            context = json.loads(messages[-1].content)
            if context.get("selected_inventory_container") != "backpack":
                return ModelReply(content='{"unsupported":"请补充背包库存"}')
            return ModelReply(content='{"operation":"craft","goals":{"explosive.timed":2}}')

    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    runtime = ChatRuntime(settings, InventoryAwareModel(), store)
    scope = new_scope(runtime.player_states)
    runtime.player_states.update(scope, observation(0, inventory({"explosives": 10, "sulfur": 1000}, True),
                                                   at=datetime.now(timezone.utc)))
    identifier = store.create().id
    await runtime.bind_player_state(identifier, scope)
    result = await runtime.chat(identifier, ChatRequest(request_id=uuid4(),
        message="按已选择的背包库存，合成 2 个 C4 还缺什么？", player_inventory_container="backpack"))
    assert result.calculation["result"]["acquisition"]["sulfur"] == 2300
    assert result.calculation["player_state_usage"]["container_id"] == "backpack"
    assert runtime.player_states.get(scope)["revision"] == 1


@pytest.mark.parametrize("case", ["partial", "stale", "invalidated", "explicit_inventory"])
async def test_calculation_cannot_silently_use_unsafe_or_ambiguous_inventory(tmp_path, case):
    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    runtime = ChatRuntime(settings, EchoModel(), store)
    scope = new_scope(runtime.player_states)
    now = datetime.now(timezone.utc)
    runtime.player_states.update(scope, observation(0, inventory({"sulfur": 1000}, case != "partial"),
        at=now - timedelta(hours=1) if case == "stale" else now))
    if case == "invalidated":
        runtime.player_states.update(scope, observation(1, {"event": "raided"}, at=now))
    identifier = store.create().id
    await runtime.bind_player_state(identifier, scope)
    payload = {"operation": "craft", "goals": {"explosive.timed": 2}}
    if case == "explicit_inventory":
        payload["inventory"] = {"sulfur": 50}
    result = await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="/calc " + json.dumps(payload),
        player_inventory_container="backpack"))
    assert result.calculation["status"] == "needs_input"
    assert "result" not in result.calculation


async def test_followup_reloads_inventory_instead_of_reusing_previous_stock(tmp_path):
    class FollowupModel:
        async def complete(self, messages):
            context = json.loads(messages[-1].content)
            previous = context["previous_request"]
            assert previous["inventory"] is None
            assert previous["goals"] == {"explosive.timed": 2}
            return ModelReply(content=json.dumps({**previous, "goals": {"explosive.timed": 3}}))

    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    runtime = ChatRuntime(settings, FollowupModel(), store)
    scope = new_scope(runtime.player_states)
    now = datetime.now(timezone.utc)
    runtime.player_states.update(scope, observation(0, inventory({"sulfur": 1000}, True), at=now))
    identifier = store.create().id
    await runtime.bind_player_state(identifier, scope)
    await runtime.chat(identifier, ChatRequest(request_id=uuid4(),
        message='/calc {"operation":"craft","goals":{"explosive.timed":2}}', player_inventory_container="backpack"))
    runtime.player_states.update(scope, observation(1, inventory({"sulfur": 500}, True), at=now))
    result = await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="改成三个", player_inventory_container="backpack"))
    assert result.calculation["result"]["acquisition"]["sulfur"] == 6100
    assert result.player_state_ref["revision"] == 2


def test_api_structured_and_text_preview_never_write_until_confirmed(tmp_path):
    class PreviewModel:
        async def complete(self, messages):
            return ModelReply(content=json.dumps({"facts": {"technology.workbench": 2}, **inventory({"sulfur": 1000})}))

    settings = Settings(_env_file=None, api_key="", database_path=tmp_path / "api.sqlite3")
    with TestClient(create_app(settings, PreviewModel())) as client:
        assert client.get("/player-state").status_code == 200
        assert client.get("/static/player-state.js").status_code == 200
        assert client.get("/api/player-state/fields").json()["items"]["scrap"] == "废料"
        assert client.get("/api/config").json()["capabilities"]["player_state"] is True
        scope = client.post("/api/player-state/scopes", json={"server_id": "a", "wipe_id": "b", "label": "我的档案"}).json()["scope"]["id"]
        identifier = client.post("/api/conversations", json={}).json()["id"]
        assert client.post(f"/api/conversations/{identifier}/player-state", json={"scope_id": scope}).status_code == 200
        preview = client.post(f"/api/player-state/scopes/{scope}/preview", json={"message": "有1000硫磺，二级台"}).json()
        assert preview["status"] == "draft"
        assert client.get(f"/api/player-state/scopes/{scope}").json()["revision"] == 0
        saved = client.post(f"/api/player-state/scopes/{scope}/observations", json=preview["update"])
        assert saved.status_code == 200
        assert saved.json()["facts"]["technology.workbench"]["source"] == "text_confirmed"
        assert saved.json()["containers"]["backpack"]["coverage"] == "partial"
        assert client.get(f"/api/conversations/{identifier}/player-state").json()["snapshot"]["revision"] == 1
        assert client.get(f"/api/player-state/scopes/{scope}?revision=0").json()["facts"] == {}
        assert client.post(f"/api/player-state/scopes/{scope}/observations", json=preview["update"],
                           headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post(f"/api/player-state/scopes/{scope}/observations", json={"changes": {}}).status_code == 422
    with TestClient(create_app(settings, PreviewModel())) as client:
        assert client.get(f"/api/player-state/scopes/{scope}").json()["revision"] == 1


@pytest.mark.parametrize("reply", ["not json", '{"facts":{"technology.workbench":99}}',
    '{"facts":{"arbitrary":"x"}}', '{"needs_input":"请提供当前绝对数量"}'])
def test_bad_state_preview_does_not_mutate_state(tmp_path, reply):
    class BadModel:
        async def complete(self, messages):
            return ModelReply(content=reply)

    settings = Settings(_env_file=None, database_path=tmp_path / "bad.sqlite3")
    with TestClient(create_app(settings, BadModel())) as client:
        scope = client.post("/api/player-state/scopes", json={"server_id": "a", "wipe_id": "b", "label": "test"}).json()["scope"]["id"]
        result = client.post(f"/api/player-state/scopes/{scope}/preview", json={"message": "我又捡了100硫磺"})
        assert result.status_code == (200 if "needs_input" in reply else 422)
        assert client.get(f"/api/player-state/scopes/{scope}").json()["revision"] == 0


async def test_binding_cannot_change_during_inflight_chat(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class SlowModel:
        async def complete(self, messages):
            started.set()
            await release.wait()
            return ModelReply(content="收到")

    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    runtime = ChatRuntime(settings, SlowModel(), store)
    identifier = store.create().id
    scope = new_scope(runtime.player_states)
    task = asyncio.create_task(runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="你好")))
    await started.wait()
    with pytest.raises(AppError) as error:
        await runtime.bind_player_state(identifier, scope)
    assert error.value.code == "chat_busy"
    release.set()
    await task


def test_competing_writers_cannot_both_commit_same_revision(store):
    scope = new_scope(store)
    requests = [observation(0, {"facts": {"technology.workbench": level}}) for level in [1, 2]]

    def submit(request):
        try:
            return store.update(scope, request, now=NOW)["revision"]
        except AppError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, requests))
    assert outcomes.count(1) == 1
    assert outcomes.count("state_revision_conflict") == 1
    assert store.get(scope, now=NOW)["revision"] == 1


async def test_state_expiring_during_extraction_cannot_be_used_for_calculation(tmp_path):
    class ExtractModel:
        async def complete(self, messages):
            return ModelReply(content='{"operation":"craft","goals":{"explosive.timed":2}}')

    state_store = PlayerStateStore(ConversationStore(tmp_path / "expires.sqlite3"))
    scope = new_scope(state_store)
    earlier = datetime.now(timezone.utc) - timedelta(hours=2)
    fresh_in_past = state_store.update(scope, observation(0, inventory({"sulfur": 1000}, True), at=earlier), now=earlier)
    assert fresh_in_past["containers"]["backpack"]["usable_for_exact_calculation"]
    result = await CalculationService(ExtractModel()).run([ModelMessage(role="user", content="合成两个C4")],
        player_snapshot=fresh_in_past, inventory_container="backpack")
    assert result["calculation"]["status"] == "needs_input"
    assert "过期" in result["reply"].content


def test_historical_reference_is_reproducible(store):
    scope = new_scope(store)
    first = store.update(scope, observation(0, inventory({"sulfur": 1000}, True)), now=NOW)
    reference = store.reference(first)
    store.update(scope, observation(1, {"event": "death"}, at=NOW + timedelta(minutes=1)), now=NOW + timedelta(minutes=1))
    historical = store.get(scope, revision=reference["revision"], now=datetime.fromisoformat(reference["evaluated_at"]))
    assert store.reference(historical) == reference

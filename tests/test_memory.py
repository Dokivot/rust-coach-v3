import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelReply
from rust_coach.memory.policy import deterministic_candidates, select_teaching, validate_candidate
from rust_coach.memory.schemas import MemoryCandidate
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime


class Model:
    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        output = self.outputs.pop(0) if self.outputs else "先看当前条件，再选择下一步。"
        if isinstance(output, Exception):
            raise output
        return ModelReply(content=output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(_env_file=None, api_key="test", search_provider="off", database_path=tmp_path / "test.sqlite3")
    return ChatRuntime(settings, Model(), ConversationStore(settings.database_path))


async def send(runtime, conversation, message, **kwargs):
    return await runtime.chat(conversation, ChatRequest(request_id=uuid4(), message=message, **kwargs))


def candidate(kind="event", key="raided", value="玩家报告被偷家", quote="昨晚我的基地被偷家了", signal="explicit"):
    return MemoryCandidate(kind=kind, key=key, value=value, quote=quote, signal=signal)


def save(runtime, item, *, scope=None, conversation="history", request=None, created_at=None, lesson_id=None, concepts=None):
    source = {"conversation_id": conversation, "request_id": request or str(uuid4()),
              "created_at": created_at or datetime.now(timezone.utc).isoformat(), "scope_id": scope,
              "concepts": concepts or [], "lesson_id": lesson_id or str(uuid4())}
    with runtime.store.connection() as connection:
        return runtime.memory.store.apply(connection, [item], source, expected_epoch=runtime.memory.store.profile(connection)["epoch"])


async def test_explicit_preferences_cross_conversation_restart_and_current_override(runtime):
    first = runtime.store.create().id
    await send(runtime, first, "以后回答简短些")
    assert runtime.memory.store.profile()["items"]["verbosity"]["value"] == "concise"
    model = Model()
    restarted = ChatRuntime(runtime.settings, model, ConversationStore(runtime.store.path))
    second = restarted.store.create().id
    turn = await send(restarted, second, "你好")
    assert turn.presentation["context"]["preferences"]["verbosity"] == "concise"
    assert turn.memory["used_profile_refs"]["verbosity"]
    turn = await send(restarted, second, "这次详细解释")
    assert turn.presentation["context"]["preferences"]["verbosity"] == "detailed"
    assert restarted.memory.store.profile()["items"]["verbosity"]["value"] == "concise"
    assert any("个人记忆" in message.content for message in model.calls[-1])


@pytest.mark.parametrize("message", ["这次说简短点", "队友说以后回答简短些", "假如我通常独狼", "攻略说我通常独狼", "我不喜欢简短回答"])
def test_temporary_quoted_third_party_and_negated_preferences_not_saved(message):
    assert not [item for item in deterministic_candidates(message) if validate_candidate(item, message)]


@pytest.mark.parametrize("item,message", [
    (candidate(), "我还没被偷家"),
    (candidate(quote="假如我的基地被偷家了"), "假如我的基地被偷家了"),
    (candidate(quote="我准备按你说的加固基地", key="outcome"), "我准备按你说的加固基地"),
    (candidate(quote="队友说我的基地被偷家了"), "队友说我的基地被偷家了"),
    (candidate(kind="learning", key="inventory_scope", value="已掌握", quote="谢谢，懂了", signal="demonstrated"), "谢谢，懂了"),
])
def test_extraction_evidence_gate(item, message):
    assert not validate_candidate(item, message)


async def test_commands_without_model_and_replay_are_atomic(runtime):
    model = Model()
    runtime = ChatRuntime(runtime.settings, model, runtime.store)
    conversation = runtime.store.create().id
    request = ChatRequest(request_id=uuid4(), message="/memory set verbosity concise")
    first = await runtime.chat(conversation, request)
    revision = runtime.memory.store.profile()["revision"]
    assert first == await runtime.chat(conversation, request)
    assert runtime.memory.store.profile()["revision"] == revision
    assert first.presentation["blocks"][0]["placement"] == "main"
    assert "简短" in (await send(runtime, conversation, "/memory")).assistant
    await send(runtime, conversation, "/memory set verbosity detailed")
    assert runtime.memory.store.profile()["items"]["verbosity"]["value"] == "detailed"
    assert model.calls == []


async def test_failure_does_not_write_preferences(runtime):
    failing = ChatRuntime(runtime.settings, Model([AppError("failed", "失败", 502)]), runtime.store)
    with pytest.raises(AppError):
        await send(failing, failing.store.create().id, "以后回答简短些")
    assert failing.memory.store.profile()["items"] == {}
    with runtime.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


async def test_forgetting_filters_original_and_derived_history(runtime):
    model = Model()
    runtime = ChatRuntime(runtime.settings, model, runtime.store)
    conversation = runtime.store.create().id
    original = await send(runtime, conversation, "我通常独狼")
    derived = await send(runtime, conversation, "聊聊游戏")
    await send(runtime, conversation, "/memory")
    await send(runtime, conversation, "/memory forget play_style")
    await send(runtime, conversation, "你好")
    contents = " ".join(message.content for message in model.calls[-1])
    assert "我通常独狼" not in contents
    assert "solo" not in contents
    assert original.request_id in runtime.memory.store.history_exclusions(conversation)
    assert derived.request_id in runtime.memory.store.history_exclusions(conversation)
    assert "play_style" not in runtime.memory.store.profile()["items"]
    assert original.user in [turn.user for turn in runtime.store.get(conversation).turns]


async def test_disabled_memory_not_read_written_or_backfilled(runtime):
    conversation = runtime.store.create().id
    await send(runtime, conversation, "/memory set verbosity concise")
    await send(runtime, conversation, "/memory off")
    new = runtime.store.create().id
    turn = await send(runtime, new, "以后回答详细些")
    assert turn.memory["used_profile_keys"] == []
    assert runtime.memory.store.profile()["items"]["verbosity"]["value"] == "concise"
    await send(runtime, new, "/memory on")
    fresh = await send(runtime, runtime.store.create().id, "你好")
    assert fresh.presentation["context"]["preferences"]["verbosity"] == "concise"


def test_history_scope_unbound_cross_conversation_and_explicit_cross_scope(runtime):
    save(runtime, candidate(), scope="server-a", concepts=["base_entry"])
    save(runtime, candidate(quote="昨晚我的另一个基地被抄家了"), scope="server-b", concepts=["base_entry"])
    save(runtime, candidate(quote="昨天我被偷家了"), concepts=["base_entry"])
    memory = runtime.memory.store
    assert {item["scope"] for item in memory.context("上次被偷家", "new", "server-a", ["base_entry"])["events"]} == {"server-a"}
    assert {item["scope"] for item in memory.context("上次被偷家", "new", None, ["base_entry"])["events"]} == {"unscoped"}
    assert len(memory.context("跨服复盘上次被偷家", "new", "server-a", ["base_entry"])["events"]) == 3
    assert memory.context("合成批次", "new", "server-a", ["craft_batches"])["events"] == []


def test_profile_expiry_and_context_budget(runtime):
    old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    save(runtime, candidate(kind="habit", key="play_style", value="solo", quote="我通常独狼"), created_at=old)
    assert "play_style" not in runtime.memory.store.active_profile()["items"]
    for index in range(8):
        save(runtime, candidate(quote="昨晚我的基地被偷家了" + "详细经过" * 90 + str(index)), concepts=["base_entry"])
    context = runtime.memory.store.context("之前被偷家", "new", None, ["base_entry"], limit_chars=1000)
    assert len(json.dumps(context, ensure_ascii=False)) <= 1000
    assert "play_style" not in context["habits"]


def test_learning_is_topic_specific_and_duplicate_explanations_do_not_promote(runtime):
    quote = "因为背包截图只是局部，所以没有显示的东西不能直接按零处理"
    item = candidate(kind="learning", key="inventory_scope", value="解释了未知和零的区别", quote=quote, signal="demonstrated")
    save(runtime, item, lesson_id="same-task")
    save(runtime, item, lesson_id="different-task")
    save(runtime, item.model_copy(update={"quote": "所以只看见局部库存时，不能断言未显示的材料就是没有"}), lesson_id="same-task")
    assert select_teaching("背包库存", runtime.memory.store.active_profile())["mode"] == "practice"
    save(runtime, item.model_copy(update={"quote": "因为储物箱没有完整盘点，所以缺口只能按已知材料给出条件结果"}), lesson_id="new-task")
    profile = runtime.memory.store.active_profile()
    assert select_teaching("背包库存", profile)["mode"] == "independent"
    assert select_teaching("合成批次", profile)["mode"] == "guided"
    assert select_teaching("背包库存我还是不懂", profile)["mode"] == "guided"
    assert select_teaching("正在被打，解释库存", profile)["mode"] == "urgent"


async def test_background_extraction_restart_and_immutable_replay(runtime):
    model = Model([{"candidates": [candidate().model_dump()]}])
    runtime.memory.model = model
    runtime.memory.schedule = lambda: None
    conversation = runtime.store.create().id
    request = ChatRequest(request_id=uuid4(), message="昨晚我的基地被偷家了")
    first = await runtime.chat(conversation, request)
    assert runtime.memory.store.inspect()["pending"] == 1
    restarted = ChatRuntime(runtime.settings, Model(), ConversationStore(runtime.store.path))
    restarted.memory.model = model
    await restarted.memory.drain()
    assert restarted.memory.store.inspect()["pending"] == 0
    assert len(model.calls) == 1
    assert await restarted.chat(conversation, request) == first
    events = restarted.memory.store.context("上次被偷家", restarted.store.create().id, None, ["base_entry"])["events"]
    assert events[0]["quote"] == request.message


async def test_inflight_extraction_cannot_resurrect_deleted_memory(runtime):
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowModel:
        async def complete(self, messages):
            entered.set()
            await release.wait()
            return ModelReply(content=json.dumps({"candidates": [candidate().model_dump()]}))

    runtime.memory.model = SlowModel()
    conversation = runtime.store.create().id
    await send(runtime, conversation, "昨晚我的基地被偷家了")
    await asyncio.wait_for(entered.wait(), 1)
    runtime.memory.store.control("forget", "all")
    release.set()
    await runtime.memory.task
    assert runtime.memory.store.inspect()["events"] == []
    assert runtime.memory.store.inspect()["pending"] == 0


async def test_background_failure_is_bounded_and_chat_survives(runtime):
    runtime.memory.model = Model(["not json"])
    await send(runtime, runtime.store.create().id, "昨晚我的基地被偷家了")
    await runtime.memory.task
    assert runtime.memory.store.inspect()["events"] == []
    assert runtime.memory.store.inspect()["pending"] == 0
    assert len(runtime.memory.model.calls) == 1


async def test_deleted_conversation_removes_derived_memory(runtime):
    conversation = runtime.store.create().id
    await send(runtime, conversation, "我通常独狼")
    await runtime.delete(conversation)
    assert runtime.memory.store.profile()["items"] == {}
    assert runtime.memory.store.inspect()["events"] == []


async def test_calculator_never_receives_memory_events_as_inventory(runtime):
    save(runtime, candidate(key="outcome", quote="昨晚我背包有很多硫磺"), concepts=["inventory_scope"])
    calculation_model = Model([{"operation": "craft", "goals": {"gunpowder": 10}}])
    runtime = ChatRuntime(runtime.settings, Model(), runtime.store, calculation_model=calculation_model)
    result = await send(runtime, runtime.store.create().id, '/calc {"operation":"craft","goals":{"gunpowder":10}}')
    assert result.calculation["result"]["inventory_known"] is False
    assert calculation_model.calls == []
    assert "昨晚我背包" not in result.assistant


async def test_advice_receives_memory_explicitly(runtime):
    class Advice:
        def __init__(self):
            self.kwargs = None

        async def run(self, *args, **kwargs):
            self.kwargs = kwargs
            return {"reply": ModelReply(content="先核对入口。"), "calculation": None}

    advice = Advice()
    runtime = ChatRuntime(runtime.settings, Model(), runtime.store, advice_service=advice)
    save(runtime, candidate(kind="habit", key="play_style", value="solo", quote="我通常独狼"))
    await send(runtime, runtime.store.create().id, "/coach 先冲科技还是加固基地")
    assert advice.kwargs["memory_context"]["habits"]["play_style"]["value"] == "solo"


def test_memory_api_controls_validation_and_origin(runtime):
    with TestClient(create_app(runtime.settings, Model())) as client:
        assert client.get("/api/memory").json()["items"] == {}
        assert client.post("/api/memory/preferences", json={"key": "verbosity", "value": "concise"}).status_code == 200
        assert client.get("/api/memory").json()["items"]["verbosity"]["value"] == "concise"
        assert client.post("/api/memory/preferences", json={"key": "inventory", "value": "1000"}).status_code == 422
        assert client.post("/api/memory/settings", json={"enabled": False}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.delete("/api/memory/verbosity").status_code == 200
        assert client.get("/api/memory").json()["items"] == {}
        assert client.post("/api/memory/settings", json={"enabled": False}).json()["enabled"] is False


async def test_explicit_remember_waits_for_validated_write(runtime):
    runtime.memory.model = Model([{"candidates": [candidate().model_dump()]}])
    turn = await send(runtime, runtime.store.create().id, "/memory remember 昨晚我的基地被偷家了")
    assert "已记下" in turn.assistant
    assert len(runtime.memory.store.inspect()["events"]) == 1
    assert runtime.memory.store.inspect()["pending"] == 0


async def test_learning_self_report_does_not_count_as_demonstration(runtime):
    save(runtime, candidate(kind="learning", key="craft_batches", value="自报理解", quote="合成批次我明白了", signal="self_reported"))
    progress = runtime.memory.store.profile()["items"]["learning.craft_batches"]["value"]
    assert progress["demonstrations"] == []
    assert select_teaching("合成批次", runtime.memory.store.active_profile())["mode"] == "practice"


async def test_natural_forget_and_unknown_memory_do_not_fabricate(runtime):
    conversation = runtime.store.create().id
    await send(runtime, conversation, "我通常独狼")
    await send(runtime, conversation, "忘掉我的独狼习惯")
    assert "play_style" not in runtime.memory.store.profile()["items"]
    with pytest.raises(AppError, match="没有找到"):
        await send(runtime, conversation, "/memory forget missing")


async def test_two_topic_learning_cycle_with_background_evidence(runtime):
    conversation = runtime.store.create().id
    await send(runtime, conversation, "教我理解背包库存的范围")
    first_lesson = runtime.store.get(conversation).turns[-1].memory["teaching"]["lesson_id"]
    quote = "因为截图只有部分背包，所以未显示的库存不能当作零"
    runtime.memory.model = Model([{"candidates": [candidate(kind="learning", key="inventory_scope", value="能区分未知与零",
        quote=quote, signal="demonstrated").model_dump()]}])
    await send(runtime, conversation, quote)
    await runtime.memory.task
    stored = runtime.memory.store.profile()["items"]["learning.inventory_scope"]["value"]
    assert stored["demonstrations"][0]["lesson_id"] == first_lesson
    new = runtime.store.create().id
    assert (await send(runtime, new, "背包库存怎么理解")).memory["teaching"]["mode"] == "practice"
    assert (await send(runtime, new, "合成批次怎么理解")).memory["teaching"]["mode"] == "guided"
    await runtime.memory.close()


async def test_deleting_old_conversation_preserves_new_explicit_correction(runtime):
    old, new = runtime.store.create().id, runtime.store.create().id
    await send(runtime, old, "以后回答简短些")
    await send(runtime, new, "以后回答详细些")
    await runtime.delete(old)
    assert runtime.memory.store.profile()["items"]["verbosity"]["value"] == "detailed"


async def test_newer_preference_wins_over_old_background_result(runtime):
    source = {"conversation_id": "old", "request_id": str(uuid4()), "created_at": "2026-01-01T00:00:00+00:00",
              "scope_id": None, "concepts": []}
    runtime.memory.set_preference("verbosity", "detailed")
    item = candidate(kind="preference", key="verbosity", value="concise", quote="以后简短些")
    with runtime.store.connection() as connection:
        assert runtime.memory.store.apply(connection, [item], source, expected_epoch=0) == []
    assert runtime.memory.store.profile()["items"]["verbosity"]["value"] == "detailed"


async def test_compound_message_records_habit_and_event(runtime):
    quote = "我通常独狼，昨晚我的基地被偷家了"
    runtime.memory.model = Model([{"candidates": [candidate(quote=quote).model_dump()]}])
    await send(runtime, runtime.store.create().id, quote)
    await runtime.memory.task
    snapshot = runtime.memory.store.inspect()
    assert snapshot["items"]["play_style"]["value"] == "solo"
    assert any(event["kind"] == "event" for event in snapshot["events"])


async def test_background_timeout_and_cancellation_resume_are_bounded(runtime):
    entered = asyncio.Event()

    class WaitingModel:
        async def complete(self, messages):
            entered.set()
            await asyncio.Event().wait()

    runtime.memory.model = WaitingModel()
    await send(runtime, runtime.store.create().id, "昨晚我的基地被偷家了")
    await asyncio.wait_for(entered.wait(), 1)
    await runtime.memory.close()
    assert runtime.memory.store.inspect()["pending"] == 1
    runtime.memory.settings = runtime.settings.model_copy(update={"memory_timeout_seconds": 0.01})
    await runtime.memory.drain()
    assert runtime.memory.store.inspect()["pending"] == 0
    assert runtime.memory.store.inspect()["events"] == []


async def test_all_forget_does_not_expose_old_history(runtime):
    model = Model()
    runtime = ChatRuntime(runtime.settings, model, runtime.store)
    conversation = runtime.store.create().id
    await send(runtime, conversation, "我通常独狼")
    await send(runtime, conversation, "/memory forget all")
    await send(runtime, conversation, "你好")
    assert [message.role for message in model.calls[-1]] == ["system", "user"]
    assert runtime.memory.store.inspect()["items"] == {}


def test_mismatched_normalized_preference_is_rejected():
    assert not validate_candidate(candidate(kind="preference", key="verbosity", value="detailed", quote="以后回答简短些"), "以后回答简短些")


async def test_discussed_is_not_mastered_and_resume_survives_new_conversation(runtime):
    await send(runtime, runtime.store.create().id, "教我理解合成批次")
    progress = runtime.memory.store.profile()["items"]["learning.craft_batches"]["value"]
    assert progress["last_discussed"]
    assert progress["demonstrations"] == []
    turn = await send(runtime, runtime.store.create().id, "继续上次的讲解")
    assert turn.memory["teaching"]["concepts"] == ["craft_batches"]
    assert turn.memory["teaching"]["mode"] == "guided"
    assert turn.memory["teaching"]["previously_discussed"] == ["craft_batches"]


def test_delayed_learning_evidence_merges_with_newer_discussion(runtime):
    timestamp = datetime.now(timezone.utc)
    newer = (timestamp + timedelta(seconds=1)).isoformat()
    item = candidate(kind="learning", key="inventory_scope", value="讨论过", quote="我理解了库存范围", signal="discussed")
    save(runtime, item, created_at=newer, request="same-request")
    evidence = item.model_copy(update={"value": "能区分未知", "signal": "demonstrated"})
    save(runtime, evidence, created_at=timestamp.isoformat(), request="same-request", lesson_id="lesson")
    progress = runtime.memory.store.profile()["items"]["learning.inventory_scope"]["value"]
    assert len(progress["demonstrations"]) == 1
    assert progress["last_discussed"]["created_at"] == newer
    assert progress["last_signal"] == "demonstrated"


async def test_concept_routing_does_not_bypass_explicit_calculation(runtime):
    await send(runtime, runtime.store.create().id, "解释一下合成批次")
    turn = await send(runtime, runtime.store.create().id, '/calc {"operation":"craft","goals":{"gunpowder":10}}')
    assert turn.calculation["status"] == "calculated"


async def test_self_reported_routine_is_preserved_without_inferred_state(runtime):
    quote = "我通常下线前检查维护和入口"
    runtime.memory.model = Model([{"candidates": [candidate(kind="habit", key="routine", value="自报检查习惯", quote=quote).model_dump()]}])
    await send(runtime, runtime.store.create().id, quote)
    await runtime.memory.task
    context = runtime.memory.store.context("下线之前做什么", "new", None, ["upkeep"])
    assert context["habits"]["routine"]["value"] == quote
    assert "base.entrance_secured" not in json.dumps(context)

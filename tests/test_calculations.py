import json
import random
import sqlite3
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rust_coach.app import create_app
from rust_coach.calculations import Calculator, CalculationRequest
from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelReply
from rust_coach.persistence import ConversationStore
from rust_coach.runtime import ChatRuntime


@pytest.fixture
def calculator():
    return Calculator()


def craft(calculator, goals, **kwargs):
    return calculator.calculate(CalculationRequest(operation="craft", goals=goals, **kwargs))["result"]


def test_c4_known_resource_example_and_input_immutability(calculator):
    inventory = {"explosives": 10, "sulfur": 1000}
    result = craft(calculator, {"explosive.timed": 2}, inventory=inventory)
    assert result["acquisition"] == {"techparts": 4, "cloth": 10, "metal.fragments": 300,
                                     "lowgradefuel": 90, "sulfur": 2300, "charcoal": 4500}
    assert result["inventory_used"] == inventory
    assert inventory == {"explosives": 10, "sulfur": 1000}
    assert result["requirements"]["workbench_status"] == "unknown"
    assert result["requirements"]["blueprints_status"] == "not_checked"


def test_batch_shared_demand_and_finished_stock(calculator):
    result = craft(calculator, {"torch": 1, "lowgradefuel": 3}, inventory={})
    assert result["acquisition"] == {"fat.animal": 3, "cloth": 2, "wood": 30}
    assert result["batch_leftovers"] == {}
    fuel = next(step for step in result["steps"] if step["item"] == "lowgradefuel")
    assert fuel["batches"] == 1
    result = craft(calculator, {"ammo.rifle.explosive": 3}, inventory={"ammo.rifle.explosive": 1})
    assert result["acquisition"] == {"metal.fragments": 10, "sulfur": 50, "charcoal": 60}
    assert result["batch_leftovers"] == {}
    result = craft(calculator, {"ammo.rifle.explosive": 3})
    assert result["batch_leftovers"] == {"ammo.rifle.explosive": 1}


def test_reserves_mix_and_fuel_boundary(calculator):
    result = craft(calculator, {"explosive.timed": 1}, inventory={"sulfur": 2200}, reserve={"sulfur": 200})
    assert result["acquisition"]["sulfur"] == 200
    assert result["inventory_used"]["sulfur"] == 2000
    assert result["reserved"] == {"sulfur": 200}
    standard = craft(calculator, {"explosive.timed": 1})
    mixing = craft(calculator, {"explosive.timed": 1}, gunpowder_recipe="mixing_table")
    assert standard["acquisition"]["charcoal"] == 3000
    assert mixing["acquisition"]["charcoal"] == 2000
    assert mixing["acquisition"]["sulfur"] == standard["acquisition"]["sulfur"] == 2200
    assert mixing["requirements"]["mixing_table_to_check"] is True
    expanded = craft(calculator, {"explosive.timed": 1}, expand_fuel=True)
    assert "lowgradefuel" not in expanded["acquisition"]
    assert expanded["acquisition"]["fat.animal"] == 45
    assert expanded["acquisition"]["cloth"] == 20


def test_owned_all_tech_and_gear(calculator):
    result = craft(calculator, {"explosive.timed": 2}, inventory={"explosive.timed": 2}, workbench=0)
    assert result["steps"] == []
    assert result["acquisition"] == {}
    assert result["requirements"]["blueprints_status"] == "not_needed"
    result = craft(calculator, {"metal.facemask": 1, "metal.plate.torso": 1}, inventory={"leather": 60}, workbench=2)
    assert result["acquisition"] == {"leather": 40, "metal.refined": 33, "sewingkit": 14}
    assert result["requirements"]["workbench_status"] == "blocked"
    result = craft(calculator, {"stonehatchet": 1}, blueprints=["stonehatchet"], workbench=0)
    assert result["requirements"]["blueprints_status"] == "declared_available"


def test_unknown_inventory_is_not_zero_claim(calculator):
    unknown = craft(calculator, {"stonehatchet": 1})
    zero = craft(calculator, {"stonehatchet": 1}, inventory={})
    assert unknown["acquisition"] == zero["acquisition"]
    assert unknown["inventory_known"] is False
    assert zero["inventory_known"] is True


def test_multiple_targets_do_not_pool_hp_and_exact_threshold(calculator):
    for hp, expected in [(275, 1), (276, 2), (500, 2)]:
        result = calculator.calculate(CalculationRequest(operation="raid", target="building:base-wall:stone",
            hp=hp, count=2, methods=["explosive.timed"]))["result"]
        assert result["options"][0]["per_target"] == expected
        assert result["options"][0]["count"] == expected * 2
    result = calculator.calculate(CalculationRequest(operation="raid", target="building:base-wall:stone"))["result"]
    assert [(option["item"], option["count"], option["new_sulfur"]) for option in result["options"]][:2] == [
        ("explosive.timed", 2, 4400), ("explosive.satchel", 10, 4800)]


def test_all_raid_reference_counts_and_minimum_damage(calculator):
    for target_id, target in calculator.catalog.targets.items():
        for method_id, method in target["methods"].items():
            for side, reference in method["sides"].items():
                result = calculator.calculate(CalculationRequest(operation="raid", target=target_id, side=side,
                                                                  methods=[method_id]))["result"]["options"][0]
                assert result["count"] == reference["reference_count"]
                damage = Decimal(result["damage"])
                assert damage * result["count"] >= target["hp"]
                assert damage * (result["count"] - 1) < target["hp"]


def test_inventory_changes_sulfur_ranking(calculator):
    result = calculator.calculate(CalculationRequest(operation="raid", target="building:base-wall:stone",
        inventory={"ammo.rocket.basic": 4}))["result"]
    assert result["options"][0]["item"] == "ammo.rocket.basic"
    assert result["options"][0]["new_sulfur"] == 0
    assert result["options"][0]["craft"]["steps"] == []


def test_craft_plan_can_be_executed_without_double_spending(calculator):
    randomizer = random.Random(42)
    candidates = ["explosive.timed", "ammo.rocket.basic", "explosive.satchel", "gunpowder", "torch", "lowgradefuel", "spear.stone", "spear.wooden"]
    for _ in range(80):
        goals = {item: randomizer.randint(1, 7) for item in randomizer.sample(candidates, 4)}
        inventory = {item: randomizer.randint(0, 5) for item in candidates}
        result = craft(calculator, goals, inventory=inventory, expand_fuel=True)
        ledger = dict(inventory)
        for item, amount in result["acquisition"].items():
            ledger[item] = ledger.get(item, 0) + amount
        for step in result["steps"]:
            for item, amount in step["ingredients"].items():
                assert ledger.get(item, 0) >= amount
                ledger[item] -= amount
            ledger[step["item"]] = ledger.get(step["item"], 0) + step["produced"]
        for item, amount in goals.items():
            assert ledger.get(item, 0) >= amount
            ledger[item] -= amount
        expected = dict(result["inventory_remaining"])
        for item, amount in result["batch_leftovers"].items():
            expected[item] = expected.get(item, 0) + amount
        assert {item: amount for item, amount in ledger.items() if amount} == expected


@pytest.mark.parametrize("payload", [
    {"operation": "craft", "goals": {"gunpowder": -1}},
    {"operation": "craft", "goals": {"gunpowder": True}},
    {"operation": "craft", "goals": {"gunpowder": 1.5}},
    {"operation": "craft", "goals": {"gunpowder": "1"}},
    {"operation": "craft", "goals": {"gunpowder": 10001}},
    {"operation": "raid", "damage": 999999},
    {"operation": "raid", "ruleset": "modded"},
    {"operation": "craft", "target": "building:base-wall:stone"},
    {"operation": "raid", "methods": ["explosive.timed", "explosive.timed"]},
])
def test_strict_inputs(payload):
    with pytest.raises(ValidationError):
        CalculationRequest.model_validate(payload)


@pytest.mark.parametrize("payload,code", [
    ({"operation": "raid", "target": "unknown"}, "target_required"),
    ({"operation": "raid", "target": "building:base-wall:stone", "hp": 501}, "invalid_hp"),
    ({"operation": "raid", "target": "building:base-wall:stone", "methods": ["ammo.rifle.explosive"]}, "unsupported_method"),
    ({"operation": "craft", "goals": {"unknown": 1}}, "unsupported_item"),
    ({"operation": "craft", "goals": {"sulfur": 1}}, "recipe_unavailable"),
    ({"operation": "craft", "goals": {"gunpowder": 1}, "reserve": {"sulfur": 1}}, "inventory_required"),
    ({"operation": "craft", "goals": {"gunpowder": 1}, "inventory": {}, "reserve": {"sulfur": 1}}, "invalid_reserve"),
])
def test_unsupported_never_silently_substituted(calculator, payload, code):
    with pytest.raises(AppError) as error:
        calculator.calculate(CalculationRequest.model_validate(payload))
    assert error.value.code == code


def test_api_without_model_and_audit_stability(tmp_path):
    settings = Settings(_env_file=None, api_key="", database_path=tmp_path / "chat.sqlite3")
    with TestClient(create_app(settings)) as client:
        catalog = client.get("/api/calculations/catalog").json()
        assert len(catalog["targets"]) == 8
        assert len(catalog["craftable"]) == 60
        assert client.get("/calculator").status_code == 200
        payload = {"operation": "raid", "target": "building:base-wall:stone", "methods": ["explosive.timed"]}
        response = client.post("/api/calculations", json=payload)
        assert response.status_code == 200
        first = response.json()
        assert first == client.post("/api/calculations", json=payload).json()
        assert "ceil(500 / 275)" in first["text"]
        assert "未核验对应游戏 build" in first["text"]
        payload["hp"] = 250
        assert first["calculation_id"] != client.post("/api/calculations", json=payload).json()["calculation_id"]
        assert client.post("/api/calculations", json={"operation": "raid", "damage": 1000}).status_code == 422
        assert client.post("/api/calculations", json=payload, headers={"Origin": "https://evil.example"}).status_code == 403
        identifier = client.post("/api/conversations", json={}).json()["id"]
        response = client.post(f"/api/conversations/{identifier}/messages", json={
            "request_id": str(uuid4()), "message": "/calc " + json.dumps(payload)})
        assert response.status_code == 200
        assert response.json()["calculation"]["result"]["options"][0]["count"] == 1


async def test_natural_language_clarification_restart_and_replay(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    replies = [{"operation": "craft", "goals": {}},
               {"operation": "craft", "goals": {"explosive.timed": 2}},
               {"operation": "craft", "goals": {"explosive.timed": 2}, "inventory": {"explosives": 10}}]

    class ExtractionModel:
        def __init__(self):
            self.inputs = []

        async def complete(self, messages):
            self.inputs.append(json.loads(messages[-1].content))
            return ModelReply(content=json.dumps(replies.pop(0)))

    model = ExtractionModel()
    store = ConversationStore(settings.database_path)
    identifier = store.create().id
    runtime = ChatRuntime(settings, model, store)
    first = await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="合成这套装备"))
    assert first.calculation["status"] == "needs_input"
    runtime = ChatRuntime(settings, model, ConversationStore(settings.database_path))
    second = await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="2 个 C4"))
    assert second.calculation["result"]["acquisition"]["sulfur"] == 4400
    request = ChatRequest(request_id=uuid4(), message="已有 10 个炸药材料")
    third = await runtime.chat(identifier, request)
    assert third.calculation["result"]["acquisition"]["sulfur"] == 3300
    assert await runtime.chat(identifier, request) == third
    assert len(model.inputs) == 3
    assert model.inputs[-1]["previous_request"]["goals"] == {"explosive.timed": 2}
    assert ConversationStore(settings.database_path).get(identifier).turns[-1] == third


@pytest.mark.parametrize("response", ["not json", '{"operation":"raid","damage":999999}',
    '{"operation":"craft","goals":{"fake":1}}', '{"unsupported":"修改服伤害不受支持"}'])
async def test_bad_extraction_cannot_generate_numeric_answers(tmp_path, response):
    class BadModel:
        async def complete(self, messages):
            return ModelReply(content=response)

    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    runtime = ChatRuntime(settings, BadModel(), store)
    result = await runtime.chat(store.create().id, ChatRequest(request_id=uuid4(), message="算一下拆石墙的炸药"))
    assert result.calculation["status"] == "needs_input"
    assert "999999" not in result.assistant


def test_existing_database_migration(tmp_path):
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript("CREATE TABLE conversations (id TEXT PRIMARY KEY);"
        "CREATE TABLE turns (sequence INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, request_id TEXT,"
        "user TEXT, assistant TEXT, created_at TEXT, truncated INTEGER);"
        "INSERT INTO conversations VALUES ('old');"
        "INSERT INTO turns VALUES (1, 'old', 'request', 'hi', 'hello', 'date', 0);")
    connection.close()
    store = ConversationStore(path)
    assert store.get("old").turns[0].calculation is None
    store.append("old", "new", "question", "answer", False, {"status": "needs_input"})
    assert ConversationStore(path).get("old").turns[-1].calculation == {"status": "needs_input"}


@pytest.mark.parametrize("failure", ["bad_json", "bad_schema", "empty_response"])
async def test_extraction_retries_once_then_uses_validated_result(tmp_path, failure):
    class RecoveringModel:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages):
            self.calls += 1
            if self.calls == 1:
                if failure == "empty_response":
                    raise AppError("model_invalid_response", "empty", 502)
                return ModelReply(content="invalid" if failure == "bad_json" else '{"operation":"raid","damage":99}')
            return ModelReply(content='{"operation":"raid","target":"building:base-wall:stone","methods":["explosive.timed"]}')

    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    model = RecoveringModel()
    result = await ChatRuntime(settings, model, store).chat(store.create().id,
        ChatRequest(request_id=uuid4(), message="一面石墙需要多少？"))
    assert result.calculation["result"]["options"][0]["count"] == 2
    assert model.calls == 2


async def test_provider_failure_does_not_save_or_erase_last_calculation(tmp_path):
    class FailedModel:
        async def complete(self, messages):
            raise AppError("model_invalid_response", "empty", 502)

    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    identifier = store.create().id
    runtime = ChatRuntime(settings, FailedModel(), store)
    first = await runtime.chat(identifier, ChatRequest(request_id=uuid4(),
        message='/calc {"operation":"craft","goals":{"explosive.timed":2}}'))
    with pytest.raises(AppError):
        await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="改成三个"))
    assert store.get(identifier).turns == [first]


async def test_unsupported_request_keeps_pending_context_across_restart(tmp_path):
    class ClarifyingModel:
        async def complete(self, messages):
            context = json.loads(messages[-1].content)
            if context["message"] == "原版":
                assert "拆石墙" in context["pending_message"]
                return ModelReply(content='{"operation":"raid","target":"building:base-wall:stone","methods":["explosive.timed"]}')
            return ModelReply(content='{"unsupported":"请确认是否为原版伤害"}')

    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    identifier = store.create().id
    model = ClarifyingModel()
    first = await ChatRuntime(settings, model, store).chat(identifier,
        ChatRequest(request_id=uuid4(), message="拆石墙，但不确定是什么服"))
    assert first.calculation["input"] is None
    restored = ConversationStore(settings.database_path)
    result = await ChatRuntime(settings, model, restored).chat(identifier,
        ChatRequest(request_id=uuid4(), message="原版"))
    assert result.calculation["result"]["options"][0]["count"] == 2


@pytest.mark.parametrize("question,target,count", [
    ("拆一面石墙最省资源的方案是什么？", "building:base-wall:stone", 1),
    ("请问拆两面石墙最省硫磺的方法是什么？", "building:base-wall:stone", 2),
    ("石墙怎么炸最省？", "building:base-wall:stone", 1),
    ("帮我算一下拆3扇金属门最省材料的方案", "item:door.hinged.metal", 3),
    ("车库门用什么拆最省资源？", "item:wall.frame.garagedoor", 1),
])
async def test_standard_raid_uses_catalog_defaults_without_model_refusal(tmp_path, question, target, count):
    class RefusingModel:
        async def complete(self, messages):
            raise AssertionError("Standard raid comparison should not need model extraction")

    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    owner = store.create().id
    runtime = ChatRuntime(settings, RefusingModel(), store)
    await runtime.chat(owner, ChatRequest(request_id=uuid4(), message='/calc {"operation":"craft","goals":{"explosive.timed":10}}'))
    turn = await runtime.chat(owner, ChatRequest(request_id=uuid4(), message=question))
    assert turn.calculation["status"] == "calculated"
    assert turn.calculation["input"]["target"] == target
    assert turn.calculation["input"]["count"] == count
    assert turn.calculation["input"]["hp"] is None
    assert turn.calculation["input"]["goals"] == {}
    assert turn.calculation["result"]["hp_each"] == Calculator().catalog.targets[target]["hp"]
    assert "暂按满血计算" in turn.assistant


@pytest.mark.parametrize("condition", ["受损但不知道血量", "修改服", "混搭最优", "软面用镐子", "不同血量的两面墙"])
async def test_special_raid_conditions_are_not_replaced_by_defaults(tmp_path, condition):
    class ConditionalModel:
        async def complete(self, messages):
            context = json.loads(messages[-1].content)
            assert context["previous_request"] is None
            assert condition in context["message"]
            return ModelReply(content=json.dumps({"unsupported": condition}))

    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    owner = store.create().id
    runtime = ChatRuntime(settings, ConditionalModel(), store)
    await runtime.chat(owner, ChatRequest(request_id=uuid4(), message='/calc {"operation":"craft","goals":{"explosive.timed":10}}'))
    turn = await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="拆一面石墙最省资源的方案是什么？" + condition))
    assert turn.calculation["status"] == "needs_input"
    assert turn.calculation["input"] is None
    assert condition in turn.calculation["pending_message"]


async def test_explicit_hp_followup_keeps_raid_target(tmp_path):
    class HpModel:
        async def complete(self, messages):
            context = json.loads(messages[-1].content)
            assert context["previous_request"]["operation"] == "raid"
            return ModelReply(content=json.dumps({**context["previous_request"], "hp": 250}))

    settings = Settings(_env_file=None, database_path=tmp_path / "chat.sqlite3")
    store = ConversationStore(settings.database_path)
    owner = store.create().id
    runtime = ChatRuntime(settings, HpModel(), store)
    await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="拆一面石墙最省资源的方案是什么？"))
    turn = await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="它还剩250血"))
    assert turn.calculation["result"]["hp_each"] == 250
    assert turn.calculation["result"]["options"][0]["count"] == 1
    assert "暂按满血计算" not in turn.assistant

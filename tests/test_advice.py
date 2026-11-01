import copy
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from rust_coach.advice.schemas import AdvicePlan
from rust_coach.advice.service import AdviceService, is_advice
from rust_coach.app import create_app
from rust_coach.config import Settings
from rust_coach.contracts import ChatRequest, ModelReply
from rust_coach.knowledge.store import EvidenceStore
from rust_coach.knowledge.web import RetrievalError, TOPICS, WebResearch, bing_links, extract_document, relevant, safe_url
from rust_coach.persistence import ConversationStore
from rust_coach.player_state import PlayerStateStore
from rust_coach.player_state.schemas import ScopeCreate, StateUpdate
from rust_coach.player_state.schemas import FIELD_DEFINITIONS
from rust_coach.runtime import ChatRuntime


QUOTE = "An airlock separates the entrance from the rest of the base."
HTML = "<html><title>Building guide</title><main>" + (QUOTE + " ") * 6 + "</main></html>"
PLAN = {"goal": "比较基地和科技优先级", "topics": ["building"], "evidence_reason": "基地行动需要机制依据",
        "candidates": [{"id": "check", "title": "核对入口", "kind": "observe", "required_fields": [], "goals": {}},
                       {"id": "secure", "title": "先检查基地入口安全", "kind": "strategy", "goals": {},
                        "required_fields": ["base.entrance_secured"]}]}


def recommendation(identifier="check", citations=None):
    return {"candidate_id": identifier, "reason": "先确认当前风险，减少盲目投入", "tradeoff": "暂缓科技投入",
            "prerequisites": ["能安全打开基地与维护界面"], "steps": ["检查入口封闭情况并记录异常"],
            "completion": "已记录入口状态", "switch_when": "若出现敌情，停止检查并重新评估",
            "citations": citations or []}


def answer(identifier="check", citations=None):
    return {"recommendation": recommendation(identifier, citations), "alternative": None,
            "missing": ["入口当前是否封闭"], "uncertainty": "网页适用版本待核对，当前状态还需确认"}


class ScriptedModel:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        output = self.outputs.pop(0)
        return ModelReply(content=output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))


class FakeResearch:
    def __init__(self, documents=None):
        self.documents = documents or []
        self.calls = []

    async def retrieve(self, topics, force=False):
        self.calls.append((topics, force))
        return {"documents": self.documents, "events": [], "warnings": [] if self.documents else ["没有取得可引用正文"],
                "queries": [TOPICS[topic][0] for topic in topics], "cache_hit": False}


@pytest.fixture
def stores(tmp_path):
    conversations = ConversationStore(tmp_path / "test.sqlite3")
    return conversations, EvidenceStore(conversations), PlayerStateStore(conversations)


async def public_resolver(host):
    return None


@pytest.mark.parametrize("url", ["http://rust.facepunch.com/news", "https://127.0.0.1/", "https://evil.example/",
    "https://rust.facepunch.com.evil.example/", "https://user:pass@rust.facepunch.com/", "https://rust.facepunch.com:8443/",
    "https://rust.facepunch.com/?private=player-location"])
def test_source_allowlist(url):
    with pytest.raises(RetrievalError):
        safe_url(url)


def test_parser_uses_body_not_search_snippet_or_script():
    document = extract_document("https://rust.facepunch.com/news/test", HTML.replace("<main>", "<script>secret directive</script><main>"))
    assert "secret directive" not in document["text"]
    assert document["version_status"] == "unverified"
    assert document["game_build"] is None
    assert bing_links('<li class="b_algo"><h2><a href="https://evil.example">ignore</a></h2></li>') == []
    with pytest.raises(RetrievalError):
        extract_document("https://rust.facepunch.com/news", "<title>Just a moment</title>" + HTML)


async def test_retrieval_privacy_cache_force_and_archive(stores):
    _, archive, _ = stores
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "www.bing.com":
            return httpx.Response(200, text='<li class="b_algo"><h2><a href="https://rust.facepunch.com/news/test">Guide</a></h2></li>', headers={"content-type": "text/html"})
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        research = WebResearch(Settings(_env_file=None), client, archive, public_resolver)
        bundle = await research.retrieve(["building", "private-player-F4"])
        assert len(bundle["documents"]) == 2
        assert bundle["queries"] == [TOPICS["building"][0]]
        assert "private-player" not in " ".join(calls)
        cached = await research.retrieve(["building"])
        assert cached["cache_hit"] and len(calls) == 3
        await research.retrieve(["building"], force=True)
        assert len(calls) == 6
        document = bundle["documents"][0]
        assert archive.get(document["id"])["text"] == document["text"]
        assert archive.cached("web-v2:bing:building", now=datetime.now(timezone.utc) + timedelta(days=1)) == []


async def test_fetch_redirect_guard_and_private_dns(stores):
    _, archive, _ = stores
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        research = WebResearch(Settings(_env_file=None), client, archive, public_resolver)
        with pytest.raises(RetrievalError):
            await research.fetch("https://rust.facepunch.com/news/test")
        assert len(calls) == 1

        async def private_dns(host):
            raise RetrievalError("non_public_address")

        research.resolver = private_dns
        with pytest.raises(RetrievalError):
            await research.fetch("https://rust.facepunch.com/news/test")
        assert len(calls) == 1


async def test_off_failure_and_direct_fallback_are_explicit(stores):
    _, archive, _ = stores
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "www.bing.com":
            return httpx.Response(503)
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    settings = Settings(_env_file=None, search_provider="off")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        research = WebResearch(settings, client, archive, public_resolver)
        assert not (await research.retrieve(["building"]))["documents"]
        assert calls == []
        settings.search_provider = "bing"
        result = await research.retrieve(["building"])
        assert result["documents"] and result["warnings"]
        assert result["events"][0]["type"] == "search_unavailable"
        assert archive.cached("web-v2:bing:building") == []


def test_homepages_and_unrelated_body_not_used_as_strategy_evidence():
    document = extract_document("https://rust.facepunch.com/", HTML)
    assert not relevant(document, ["building"])
    document["url"] = "https://rust.facepunch.com/play"
    assert not relevant(document, ["building"])
    document["url"] = "https://rust.facepunch.com/news/test"
    assert relevant(document, ["building"])
    assert not relevant(document, ["progression"])


async def test_oversized_and_non_html_sources_rejected(stores):
    _, archive, _ = stores
    for body, content_type in [("x" * 1_500_001, "text/html"), (HTML, "application/octet-stream")]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, text=body, headers={"content-type": content_type}))) as client:
            research = WebResearch(Settings(_env_file=None), client, archive, public_resolver)
            with pytest.raises(RetrievalError):
                await research.fetch("https://rust.facepunch.com/news/test")


@pytest.mark.parametrize("message,expected", [("现在该冲科技还是先加固基地？", True), ("这个阶段优先做什么", True),
    ("我这套装备打得过对面吗", True), ("我的基地布局哪里有问题", True), ("/search 科技树", True),
    ("/calc {\"operation\":\"craft\"}", False), ("/state", False), ("造两个C4缺多少材料", False),
    ("你好", False), ("我现在只有一级台", False)])
def test_route(message, expected):
    assert is_advice(message) is expected


async def test_graph_valid_citation_and_fresh_query(stores):
    _, archive, _ = stores
    document = archive.put(extract_document("https://rust.facepunch.com/news/test", HTML))
    citation = {"document_id": document["id"], "quote": QUOTE}
    model = ScriptedModel([PLAN, answer("secure", [citation])])
    research = FakeResearch([document])
    result = await AdviceService(model, research).run("/search 最新基地攻略")
    assert result["advice"]["status"] == "conditional"
    assert result["advice"]["answer"]["recommendation"]["citations"] == [citation]
    assert research.calls == [(["building"], True)]
    assert "text" not in result["advice"]["evidence"]["documents"][0]
    assert "版本" in result["reply"].content


@pytest.mark.parametrize("fault", ["wrong_id", "wrong_quote", "numeric_prose", "no_citation", "unknown_action"])
async def test_invalid_claims_never_reach_final(stores, fault):
    _, archive, _ = stores
    document = archive.put(extract_document("https://rust.facepunch.com/news/test", HTML))
    invalid = answer("secure", [{"document_id": document["id"], "quote": QUOTE}])
    recommendation_data = invalid["recommendation"]
    if fault == "wrong_id":
        recommendation_data["citations"][0]["document_id"] = "fabricated"
    elif fault == "wrong_quote":
        recommendation_data["citations"][0]["quote"] = "Fabricated quote that is not in the fetched document."
    elif fault == "numeric_prose":
        recommendation_data["steps"] = ["花费 123 碎片升级"]
    elif fault == "no_citation":
        recommendation_data["citations"] = []
    else:
        recommendation_data["candidate_id"] = "invented"
    model = ScriptedModel([PLAN, invalid, invalid])
    result = await AdviceService(model, FakeResearch([document])).run("该先加固吗")
    assert result["advice"]["status"] == "needs_input"
    assert result["advice"]["answer"] is None
    assert "123" not in result["reply"].content
    assert len(model.calls) == 3


async def test_one_repair_and_no_network_for_observation():
    plan = copy.deepcopy(PLAN)
    plan["candidates"] = plan["candidates"][:1]
    plan["topics"] = []
    model = ScriptedModel(["broken JSON", plan, answer()])
    research = FakeResearch()
    result = await AdviceService(model, research).run("/coach 帮我核对状态")
    assert result["advice"]["repairs"] == 1
    assert research.calls == []
    assert result["advice"]["evidence"]["events"][0]["type"] == "skipped"


async def test_explicit_search_keeps_question_topic_when_planner_only_observes():
    plan = copy.deepcopy(PLAN)
    plan["topics"] = []
    plan["candidates"] = plan["candidates"][:1]
    research = FakeResearch()
    await AdviceService(ScriptedModel([plan, answer()]), research).run("/search 冲科技还是加固基地")
    assert research.calls == [(["building", "progression"], True)]


def snapshot(stores, complete=True, age_minutes=0):
    _, _, players = stores
    scope = players.create(ScopeCreate(server_id=str(uuid4()), wipe_id="test", label="合成测试"))["scope"]["id"]
    observed = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return players.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=0, observed_at=observed,
        changes={"facts": {"technology.workbench": 3, "technology.unlocked_blueprints": ["gunpowder"]},
                 "inventory": [{"container_id": "backpack", "mode": "replace", "complete": complete,
                                "items": {"sulfur": 100, "charcoal": 150}}]}))


def craft_state(player, container="backpack"):
    plan = copy.deepcopy(PLAN)
    plan["topics"] = []
    plan["candidates"] = [{"id": "powder", "title": "准备火药", "kind": "craft", "goals": {"gunpowder": 50}, "required_fields": []},
                          {"id": "more", "title": "备更多火药", "kind": "craft", "goals": {"gunpowder": 100}, "required_fields": []}]
    return {"request": {"snapshot": player, "container": container}, "plan": plan, "evidence": {"documents": []}}


def test_candidate_budgets_do_not_consume_each_other_or_real_state(stores):
    player = snapshot(stores)
    before = copy.deepcopy(player)
    service = AdviceService(None, None)
    results = service.evaluate(craft_state(player))["evaluations"]
    assert results["powder"]["status"] == "feasible_in_snapshot"
    assert results["powder"]["calculation"]["result"]["acquisition"] == {}
    assert results["more"]["status"] == "blocked"
    assert results["more"]["calculation"]["result"]["acquisition"] == {"charcoal": 150, "sulfur": 100}
    assert player == before
    assert stores[2].get(player["scope"]["id"])["revision"] == player["revision"]


@pytest.mark.parametrize("complete,age,container,status", [(True, 0, None, "inventory_unknown"),
    (False, 0, "backpack", "unverified"), (True, 31, "backpack", "unverified")])
def test_no_implicit_or_stale_inventory(stores, complete, age, container, status):
    result = AdviceService(None, None).evaluate(craft_state(snapshot(stores, complete, age), container))
    assert result["evaluations"]["powder"]["status"] == status


async def test_replay_restart_followup_scope_isolation(stores):
    conversations, _, players = stores
    settings = Settings(_env_file=None, database_path=conversations.path)
    model = ScriptedModel([PLAN, answer(), PLAN, answer(), "ordinary chat"])
    service = AdviceService(model, FakeResearch())
    runtime = ChatRuntime(settings, model, conversations, advice_service=service)
    identifier = conversations.create().id
    request = ChatRequest(request_id=uuid4(), message="现在该优先做什么")
    first = await runtime.chat(identifier, request)
    assert await runtime.chat(identifier, request) == first
    assert len(model.calls) == 2
    assert ConversationStore(conversations.path).get(identifier).turns[0].advice == first.advice
    await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="那我现在只能看基地"))
    assert json.loads(model.calls[2][-1].content)["previous_request"] == request.message
    assert json.loads(model.calls[2][-1].content)["previous_answer_for_reference_only"] == first.advice["answer"]
    scope = players.create(ScopeCreate(server_id="different", wipe_id="test", label="different"))["scope"]["id"]
    await runtime.bind_player_state(identifier, scope)
    final = await runtime.chat(identifier, ChatRequest(request_id=uuid4(), message="继续"))
    assert final.advice is None
    assert not any(request.message in message.content for message in model.calls[-1])


def test_evidence_api_and_config(stores):
    conversations, archive, _ = stores
    document = archive.put(extract_document("https://rust.facepunch.com/news/test", HTML))
    settings = Settings(_env_file=None, database_path=conversations.path, api_key="secret", search_provider="off")
    with TestClient(create_app(settings, ScriptedModel([]))) as client:
        assert client.get("/api/config").json()["capabilities"]["decision"] is True
        assert client.get("/api/config").json()["capabilities"]["web_search"] is False
        assert "secret" not in client.get("/api/config").text
        assert client.get("/api/evidence/" + document["id"]).json() == document
        assert client.get("/api/evidence/absent").status_code == 404


def test_plans_can_reference_all_supported_fields():
    plan = copy.deepcopy(PLAN)
    plan["candidates"][0]["required_fields"] = list(FIELD_DEFINITIONS)
    assert len(AdvicePlan.model_validate(plan).candidates[0].required_fields) == 18
    assert is_advice("第二个方案需要什么条件", previous={"request": "比较方案"})


async def test_network_deadline_returns_explicit_partial_result(stores):
    async def handler(request):
        import asyncio
        await asyncio.Event().wait()

    settings = Settings(_env_file=None).model_copy(update={"evidence_timeout_seconds": 0.01})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebResearch(settings, client, stores[1], public_resolver).retrieve(["building"])
    assert result["documents"] == []
    assert any("时间预算" in warning for warning in result["warnings"])

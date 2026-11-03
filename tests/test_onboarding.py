import copy
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from rust_coach.advice.intent import is_onboarding
from rust_coach.advice.baseline import BASELINE, SOURCE, baseline_guide
from rust_coach.advice.onboarding import guide_documents, resolve_guide, review_evidence, validate_guide
from rust_coach.advice.schemas import GuideAnswer
from rust_coach.advice.service import AdviceService, infer_topics, is_advice
from rust_coach.architecture.input import TextExtractor
from rust_coach.config import Settings
from rust_coach.contracts import AppError, ChatRequest, ModelReply
from rust_coach.knowledge.store import EvidenceStore
from rust_coach.knowledge.web import TOPICS, WebResearch, extract_document, relevant
from rust_coach.persistence import ConversationStore
from rust_coach.player_state import PlayerStateStore
from rust_coach.player_state.schemas import ScopeCreate, StateUpdate
from rust_coach.presentation.rendering import finalize_response
from rust_coach.progress import chat_events
from rust_coach.runtime import ChatRuntime


TOOL_QUOTE = "Gather wood and stone to craft a stone hatchet and a stone pickaxe before gathering more materials."
BAG_QUOTE = "Collect hemp for cloth and prepare a sleeping bag so you have a place to respawn."
BASE_QUOTE = "Build a small starter base with a locked door and a tool cupboard before expanding."
GUIDE_HTML = "<html><title>Your First Day in Rust - Beginner Survival Guide</title><main>" + " ".join([
    TOOL_QUOTE, BAG_QUOTE, BASE_QUOTE,
    "Look for food and water nearby. Avoid busy roads and watch for other players while gathering resources.",
    "Keep the first shelter small. Learn the building controls before expanding the base into a larger structure.",
    "Once you have a locked shelter, store gathered materials and consider what to improve next. This route is a general beginner guide, not a guarantee of safety.",
]) + "</main></html>"


class Model:
    def __init__(self, outputs, before=None):
        self.outputs = list(outputs)
        self.calls = []
        self.before = before

    async def complete(self, messages):
        if self.before:
            self.before()
        self.calls.append(messages)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if isinstance(output, dict) and "stages" in output:
            payload = json.loads(messages[1].content)
            passages = [passage for document in payload["documents"] for passage in document["passages"]]
            stages = []
            for stage in output["stages"]:
                refs = [next((passage["id"] for passage in passages if citation["quote"] in passage["text"] and
                    (len(citation["document_id"]) == 64 or citation["document_id"] == "source_1")), "unknown")
                    for citation in stage["citations"]]
                stages.append({"action": stage["action"], "completion": stage["completion"], "evidence_refs": refs})
            output = {**output, "stages": stages}
        return ModelReply(content=output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))


class Research:
    def __init__(self, documents):
        self.documents, self.calls = documents, []

    async def retrieve(self, topics, force=False):
        self.calls.append((topics, force))
        return {"documents": self.documents, "events": [], "warnings": [], "queries": [TOPICS[topic][0] for topic in topics], "cache_hit": False}


@pytest.fixture
def evidence(tmp_path):
    conversations = ConversationStore(tmp_path / "guide.sqlite3")
    archive = EvidenceStore(conversations)
    document = archive.put(extract_document(TOPICS["first_day"][1], GUIDE_HTML))
    return conversations, archive, document


def guide(document):
    return {"goal": "先把采集、复活点和落脚点准备起来。", "assumption": "如果你刚出生、还没有基地，可以按这个顺序起步。",
        "stages": [
            {"id": "stage_1", "action": "采集木头和石头，按制作界面准备石斧和石镐。", "completion": "能用基础工具继续采集。",
             "citations": [{"document_id": document["id"], "quote": document["text"]}]},
            {"id": "stage_2", "action": "寻找麻获取布，再准备睡袋作为复活落点。", "completion": "已有可用的复活落点。",
             "citations": [{"document_id": document["id"], "quote": document["text"]}]},
            {"id": "stage_3", "action": "条件允许时建小据点，准备门锁和工具柜。", "completion": "有能关门上锁的落脚点。",
             "citations": [{"document_id": document["id"], "quote": document["text"]}]}],
        "stop_when": "遇到其他玩家交火时先避开，不要停下来制作。",
        "followup": "你是刚出生，还是已经有落脚点？", "limitations": ["社区攻略未核对当前游戏版本。"]}


def review(draft):
    return {"supported_stage_ids": [stage["id"] for stage in draft["stages"]], "corrections": [], "issues": []}


@pytest.mark.parametrize("text,allowed", [("下一步准备一级工作台。", True), ("Prepare a level 1 workbench.", True),
    ("Prepare a Workbench Level 1.", True), ("准备四级工作台。", False), ("一级工作台需要 50 废料。", False),
    ("建造一个简单、有气闸的起步基地。", True), ("你拥有一个封闭的小基地。", True),
    ("已经在所选位置建成一个小基地。", True), ("找到一个落脚点。", True),
    ("建造一个带气闸的小基地；气闸是入口的双门缓冲区。", True),
    ("先找到一个靠近树林而且能上锁的临时落脚点。", True),
    ("如果你还没有一个基地，先准备睡袋。", True),
    ("完成一个安全的起步基地。", True), ("准备一个工具柜。", True),
    ("建造两个小基地。", False), ("建造一个2x2基地。", False),
    ("消耗一个硫磺就能合成。", False), ("成本只需一个睡袋。", False),
    ("一个基地需要50木头。", False), ("建造一个小基地只要十分钟。", False)])
def test_named_workbenches_are_not_confused_with_material_costs(evidence, text, allowed):
    _, _, document = evidence
    draft = guide(document)
    draft["stages"][0]["action"] = text
    parsed = GuideAnswer.model_validate(draft)
    if allowed:
        validate_guide(parsed, {"documents": [document]})
    else:
        with pytest.raises(ValueError, match="numeric_prose_forbidden"):
            validate_guide(parsed, {"documents": [document]})


@pytest.mark.parametrize("message", ["我是新手，什么都不懂，应该优先做什么？", "不知道从哪开始", "小白怎么玩",
    "刚开始玩 Rust", "刚进服应该干什么", "我是新手，已经有基地，接下来做什么？",
    "I am new to Rust, what should I do first?", "Where do I start?"])
def test_beginner_routing_and_topic(message):
    assert is_advice(message)
    assert is_onboarding(message)
    assert TextExtractor().request_kind(message) == "decision.recommendation"
    assert infer_topics(message) == ["first_day"]


@pytest.mark.parametrize("message", ["我是新手，合成两个C4需要多少资源？", "那我还差多少材料",
    "刚开始玩，怎么制作石斧", "那制作石斧需要什么材料", "/calc {}", "/state"])
def test_exact_requests_leave_guide_flow(message):
    previous = {"mode": "onboarding", "answer": {"stages": [{"id": "tools"}]}}
    assert not is_onboarding(message, previous_answer=previous["answer"])
    assert not is_advice(message, previous)


def test_comparison_and_ordinary_chat_keep_their_routes():
    assert not is_onboarding("我是新手，先冲科技还是加固基地？")
    assert is_advice("我是新手，先冲科技还是加固基地？")
    assert not is_onboarding("你好")
    assert not is_onboarding("我刚开始玩，正在被打怎么办")
    assert infer_topics("不知道该选哪一个") == []
    assert infer_topics("当前版本更新了什么") == ["updates"]


async def test_guide_retrieves_before_planning_and_persists_stage_evidence(evidence):
    conversations, archive, document = evidence
    draft = guide(document)
    research = Research([document])

    def has_evidence():
        assert research.calls == [(["first_day"], False)]

    generated = copy.deepcopy(draft)
    for stage in generated["stages"]:
        stage["citations"][0]["document_id"] = "source_1"
    model = Model([generated, review(draft)], has_evidence)
    settings = Settings(_env_file=None, api_key="test", database_path=conversations.path)
    runtime = ChatRuntime(settings, model, conversations, advice_service=AdviceService(model, research))
    owner = conversations.create().id
    request = ChatRequest(request_id=uuid4(), message="我是新手，什么都不懂，应该优先做什么？")
    events = [json.loads(frame[6:]) async for frame in chat_events(runtime, owner, request) if frame.startswith("data: ")]
    assert events[-1]["type"] == "result"
    turn = conversations.get(owner).turns[0]
    assert turn.advice["mode"] == "onboarding"
    assert turn.advice["answer"] == GuideAnswer.model_validate(draft).model_dump()
    assert turn.calculation is None
    assert turn.assistant.index("采集木头") < turn.assistant.index("寻找麻") < turn.assistant.index("条件允许时建")
    assert "如果你刚出生" in turn.assistant and "参考：" in turn.assistant
    assert "档案待确认" not in turn.assistant and "可先观察核对" not in turn.assistant
    assert turn.assistant.count("？") == 1
    blocks = turn.presentation["blocks"]
    citation = next(block for block in blocks if block["id"] == "guide.stage_1.citation.0")
    assert citation["placement"] == "detail" and citation["text"] == document["text"]
    assert archive.get(citation["reference"])["text"].find(citation["text"]) >= 0
    assert any(item["reference"] == "guide" for item in turn.presentation["assertions"])
    architecture = turn.presentation["architecture"]
    assert architecture["task"]["route"] == "plan_execute"
    assert architecture["task"]["required_evidence"] == ["first_day_guide"]
    assert architecture["decision_result"]["step_results"][0]["status"] == "ok"
    assert architecture["answer_spec"]["protected_payload"]["guide"] == turn.advice["answer"]
    stages = [event["stage"] for event in events if event["type"] == "stage"]
    assert stages.index("retrieve") < stages.index("plan") < stages.index("check_answer") < stages.index("compose")
    assert len(model.calls) == 2
    assert json.loads(model.calls[0][-1].content)["documents"][0]["id"] == "source_1"
    assert document["id"] not in model.calls[0][-1].content
    assert await runtime.chat(owner, request) == turn
    assert len(model.calls) == 2
    model.outputs.append({"operation": "craft", "goals": {"explosive.timed": 2}})
    calculated = await runtime.chat(owner, ChatRequest(request_id=uuid4(), message="那合成两个C4需要多少材料？"))
    assert calculated.advice is None and calculated.calculation["status"] == "calculated"
    assert calculated.calculation["result"]["inventory_known"] is False
    assert calculated.calculation["result"]["goals"] == {"explosive.timed": 2}
    assert len(model.calls) == 3 and len(research.calls) == 1


async def test_followup_uses_current_progress_without_resetting_or_mutating_profile(evidence):
    conversations, _, document = evidence
    players = PlayerStateStore(conversations)
    scope = players.create(ScopeCreate(server_id="guide", wipe_id="test", label="guide"))
    current = players.update(scope["scope"]["id"], StateUpdate(observation_id=uuid4(), expected_revision=0, observed_at=datetime.now(timezone.utc),
        changes={"facts": {"base.has_base": True}}))
    before = copy.deepcopy(current)
    draft = guide(document)
    draft["goal"] = "先完善已有落脚点，再继续准备采集与复活条件。"
    draft["assumption"] = "按你已经有基地的情况继续。"
    draft["stages"] = [draft["stages"][2], *draft["stages"][:2]]
    draft["stages"][0]["action"] = "检查已有基地的门锁和工具柜，缺少时再补。"
    draft["followup"] = ""
    model = Model([draft, review(draft)])
    service = AdviceService(model, Research([document]))
    result = await service.run("我已经有基地了，接下来呢", snapshot=current,
        previous_request="我是新手，应该优先做什么", previous_answer=guide(document))
    payload = json.loads(model.calls[0][-1].content)
    assert payload["player_state"] is not None and payload["previous_answer"]["stages"]
    assert payload["message"] == "我已经有基地了，接下来呢"
    assert result["advice"]["answer"]["stages"][0]["action"] == draft["stages"][0]["action"]
    output = finalize_response(result)
    assert "按你已经有基地" in output["reply"].content
    assert "如果你刚出生" not in output["reply"].content
    after = players.get(scope["scope"]["id"])
    assert {key: value for key, value in after.items() if key != "evaluated_at"} == {
        key: value for key, value in before.items() if key != "evaluated_at"}


@pytest.mark.parametrize("fault", ["wrong_id", "wrong_quote", "numeric_cost", "false_sufficiency", "no_citation", "many_questions"])
async def test_invalid_guide_never_reaches_user(evidence, fault):
    _, _, document = evidence
    draft = guide(document)
    stage = draft["stages"][0]
    if fault == "wrong_id":
        stage["citations"][0]["document_id"] = "invented"
    elif fault == "wrong_quote":
        stage["citations"][0]["quote"] = "This sentence is not present in the retrieved guide."
    elif fault == "numeric_cost":
        stage["action"] = "消耗 200 木头制作石斧。"
    elif fault == "false_sufficiency":
        stage["action"] = "你的材料已经足够，现在可以直接制作。"
    elif fault == "no_citation":
        stage["citations"] = []
    else:
        draft["followup"] = "在哪？有基地吗？"
    model = Model([draft, draft])
    result = await AdviceService(model, Research([document])).run("小白怎么玩")
    assert result["advice"]["answer"] is None
    assert result["advice"]["status"] == "unavailable"
    assert len(model.calls) == 2
    rendered = finalize_response(result)["reply"].content
    assert "200" not in rendered and "档案" not in rendered


async def test_real_but_unrelated_quote_requires_semantic_repair(evidence):
    _, _, document = evidence
    bad = guide(document)
    bad["stages"][0]["action"] = "去高级纪念碑收集装备。"
    fixed = guide(document)
    rejected = {"supported_stage_ids": ["stage_2", "stage_3"], "corrections": [], "issues": ["工具引用不支持前往高级纪念碑。"]}
    model = Model([bad, rejected, fixed, review(fixed)])
    result = await AdviceService(model, Research([document])).run("小白怎么玩")
    assert result["advice"]["answer"]["stages"][0]["action"] == fixed["stages"][0]["action"]
    assert result["advice"]["repairs"] == 1 and len(model.calls) == 4
    assert "revision_feedback" in json.loads(model.calls[2][-1].content)
    assert "高级纪念碑" not in finalize_response(result)["reply"].content


async def test_repeated_unsupported_plan_stops_after_one_repair(evidence):
    _, _, document = evidence
    draft = guide(document)
    rejected = {"supported_stage_ids": ["stage_1"], "corrections": [], "issues": ["其余阶段缺少支持。"]}
    model = Model([draft, rejected, draft, rejected])
    result = await AdviceService(model, Research([document])).run("小白怎么玩")
    assert result["advice"]["answer"] is None and len(model.calls) == 4
    assert result["advice"]["review"] == rejected


@pytest.mark.parametrize("invalid", [False, True])
async def test_review_can_narrow_wording_without_changing_evidence_or_extra_generation(evidence, invalid):
    _, _, document = evidence
    draft = guide(document)
    checked = review(draft)
    checked["supported_stage_ids"] = ["stage_2", "stage_3"]
    checked["corrections"] = [{"stage_id": "stage_1", "action": "采集木头和石头，制作石斧和石镐。" if not invalid else "消耗 200 木头制作石斧。",
        "completion": "已有基础采集工具。"}]
    corrected = {"stage_id": "stage_1", "action": "采集木头和石头，制作石斧和石镐。", "completion": "已有基础采集工具。"}
    model = Model([draft, checked, corrected] if invalid else [draft, checked])
    result = await AdviceService(model, Research([document])).run("小白怎么玩")
    assert len(model.calls) == (3 if invalid else 2)
    if invalid:
        assert result["advice"]["answer"]["stages"][0]["action"] == corrected["action"]
        assert result["advice"]["repairs"] == 1
        assert result["advice"]["diagnostics"] == [{"phase": "correction_validation",
            "codes": ["numeric_prose_forbidden"], "field": "stage_1.action"}]
        assert result["advice"]["answer"]["stages"][1:] == draft["stages"][1:]
        assert "200" not in result["reply"].content
    else:
        stages = result["advice"]["answer"]["stages"]
        assert stages[0]["action"] == checked["corrections"][0]["action"]
        assert stages[0]["citations"] == draft["stages"][0]["citations"]
        assert stages[1:] == draft["stages"][1:]


@pytest.mark.parametrize("prior_repair", [False, True])
async def test_real_review_completion_with_singular_base_is_accepted(evidence, prior_repair):
    _, _, document = evidence
    draft = guide(document)
    draft["stages"].extend([{**draft["stages"][0], "id": "stage_4"}, {**draft["stages"][1], "id": "stage_5"}])
    checked = {"supported_stage_ids": ["stage_1", "stage_2", "stage_5"], "corrections": [
        {"stage_id": "stage_3", "action": "用采到的布制作睡袋，并放在准备落脚的区域附近；睡袋可作为死亡后的复活地点。",
         "completion": "睡袋已经放置，可在可用时选择该睡袋作为复活地点。"},
        {"stage_id": "stage_4", "action": "选择内陆且附近有树木和石头的位置建造一个小基地；可靠近道路，但不要让基地直接暴露在路边。",
         "completion": "已经在所选位置建成一个小基地。"}], "issues": []}
    model = Model(["invalid json", draft, checked] if prior_repair else [draft, checked])
    result = await AdviceService(model, Research([document])).run("我是新手，什么都不懂，应该优先做什么？")
    assert result["advice"]["status"] == "conditional"
    assert result["advice"]["answer"]["stages"][3]["completion"] == checked["corrections"][1]["completion"]
    assert result["advice"]["answer_source"] == "generated"
    assert len(model.calls) == (3 if prior_repair else 2)
    assert result["advice"]["repairs"] == int(prior_repair)
    if prior_repair:
        assert result["advice"]["diagnostics"][0]["phase"] == "draft"


@pytest.mark.parametrize("defect", ["changed_stage", "numeric", "guarantee"])
async def test_local_repair_cannot_change_stage_or_bypass_claim_checks(evidence, defect):
    _, _, document = evidence
    draft = guide(document)
    correction = {"stage_id": "stage_1", "action": "消耗 200 木头制作石斧。", "completion": "已有基础采集工具。"}
    checked = {"supported_stage_ids": ["stage_2", "stage_3"], "corrections": [correction], "issues": []}
    repaired = {**correction}
    if defect == "changed_stage":
        repaired.update(stage_id="stage_2", action="准备睡袋。")
    elif defect == "guarantee":
        repaired["action"] = "保证不会被拆。"
    model = Model([draft, checked, repaired])
    result = await AdviceService(model, Research([document])).run("小白怎么玩")
    assert result["advice"]["answer"] is None
    assert result["advice"]["diagnostics"][-1]["phase"] == "correction_repair"
    assert len(model.calls) == 3
    assert "没有通过依据核对" not in result["reply"].content


@pytest.fixture
def baseline_document(evidence):
    _, archive, _ = evidence
    return archive.put(extract_document(SOURCE, "<html><title>Your First Day in Rust</title><main>" +
        " ".join(stage["quote"] for stage in BASELINE) + "</main></html>"))


@pytest.mark.parametrize("english", [False, True])
def test_curated_baseline_requires_exact_source_support(baseline_document, english):
    document = baseline_document
    answer = baseline_guide([document], english)
    assert len(answer["stages"]) == 5
    for stage in answer["stages"]:
        assert stage["citations"][0]["quote"] in document["text"]
        assert stage["citations"][0]["document_id"] == document["id"]
    assert baseline_guide([{**document, "url": "https://example.com/"}], english) is None
    assert baseline_guide([{**document, "text": document["text"].replace(BASELINE[0]["quote"], "")}], english) is None
    assert baseline_guide([], english) is None


@pytest.mark.parametrize("failure", ["invalid_draft", "transport", "review_rejected"])
async def test_failed_generation_uses_cited_conditional_baseline(baseline_document, failure):
    document = baseline_document
    draft = guide(document)
    if failure == "invalid_draft":
        outputs = ["invalid json", "invalid json"]
    elif failure == "transport":
        outputs = [AppError("model_timeout", "Timed out", 504)]
    else:
        rejected = {"supported_stage_ids": [], "corrections": [], "issues": ["动作缺少支持。"]}
        outputs = [draft, rejected, draft, rejected]
    model = Model(outputs)
    result = await AdviceService(model, Research([document])).run("我是新手，已经有基地和睡袋，应该优先做什么？")
    assert result["advice"]["status"] == "conditional"
    assert result["advice"]["answer_source"] == "curated_baseline"
    assert result["advice"]["diagnostics"]
    assert "就跳过" in result["reply"].content
    assert "没有通过依据核对" not in result["reply"].content
    rendered = finalize_response(result)
    assert SOURCE in rendered["reply"].content
    assert "一级工作台" in rendered["reply"].content
    assert len(model.calls) == len(outputs)


async def test_baseline_failure_diagnostics_and_sources_persist_through_runtime(evidence, baseline_document):
    conversations, _, _ = evidence
    model = Model([AppError("model_timeout", "Timed out", 504)])
    settings = Settings(_env_file=None, api_key="test", database_path=conversations.path)
    runtime = ChatRuntime(settings, model, conversations,
        advice_service=AdviceService(model, Research([baseline_document])))
    owner = conversations.create().id
    request = ChatRequest(request_id=uuid4(), message="我是新手，什么都不懂，应该优先做什么？")
    turn = await runtime.chat(owner, request)
    assert turn.advice["answer_source"] == "curated_baseline"
    assert turn.advice["review"] is None
    assert turn.advice["diagnostics"] == [{"phase": "draft", "codes": ["model_timeout"], "field": None}]
    assert "先准备采集工具" in turn.assistant and SOURCE in turn.assistant
    assert "没有通过依据核对" not in turn.assistant
    assert conversations.get(owner).turns[0].advice == turn.advice
    assert await runtime.chat(owner, request) == turn
    assert len(model.calls) == 1


async def test_review_keeps_quote_context_without_resending_whole_documents(evidence):
    _, _, document = evidence
    document = {**document, "text": "Conditions matter: avoid danger while gathering. " + document["text"] + " unrelated appendix" * 2000}
    draft = guide(document)
    for stage in draft["stages"]:
        stage["citations"][0]["quote"] = TOOL_QUOTE
    model = Model([draft, review(draft)])
    result = await AdviceService(model, Research([document])).run("小白怎么玩")
    assert result["advice"]["answer"]
    payload = json.loads(model.calls[1][-1].content)
    excerpts = [passage["text"] for passage in payload["documents"][0]["passages"]]
    assert any("Conditions matter" in excerpt and TOOL_QUOTE in excerpt for excerpt in excerpts)
    assert sum(len(excerpt) for excerpt in excerpts) < 4000
    assert "text" not in payload["documents"][0]


def test_stop_conditions_keep_their_own_source_passages(evidence):
    _, _, document = evidence
    document = {**document, "text": document["text"] + " Extra contextual background." * 50 +
        " A torch makes you visible at night. Avoid using it outside a closed shelter."}
    modeled, passages = guide_documents([document])
    final_ref = modeled[0]["passages"][-1]["id"]
    draft = {key: value for key, value in guide(document).items() if key != "stages"}
    draft.update(stages=[{"action": "准备基础工具。", "completion": "能继续采集。", "evidence_refs": ["source_1_p1"]}] * 3,
                 stop_evidence_refs=[final_ref])
    answer = resolve_guide(draft, passages)
    validate_guide(answer, {"documents": [document]})
    assert "A torch makes you visible" in answer.stop_citations[0].quote
    selected = review_evidence(draft, modeled)
    assert final_ref in {passage["id"] for document in selected for passage in document["passages"]}
    draft["stop_evidence_refs"] = ["missing"]
    with pytest.raises(ValueError, match="unknown_evidence_ref"):
        resolve_guide(draft, passages)


async def test_style_changes_preserve_guide_and_english_fallback(evidence):
    _, _, document = evidence
    draft = guide(document)
    result = await AdviceService(Model([draft, review(draft)]), Research([document])).run("小白怎么玩")
    standard = finalize_response(result)
    detailed = finalize_response({**result, "response_context": {"preferences": {"verbosity": "detailed"}}})
    assert standard["presentation"]["assertions"] == detailed["presentation"]["assertions"]
    assert standard["presentation"]["protected_payload_hash"] == detailed["presentation"]["protected_payload_hash"]
    assert TOOL_QUOTE not in standard["reply"].content and TOOL_QUOTE in detailed["reply"].content
    empty = await AdviceService(Model([]), Research([])).run("Where do I start?", response_context={"language": "en"})
    assert "beginner guide" in empty["reply"].content and "档案" not in empty["reply"].content


async def test_no_evidence_gives_limited_help_without_model_or_inventory_form():
    model = Model([])
    result = await AdviceService(model, Research([])).run("小白怎么玩")
    assert not model.calls
    text = finalize_response(result)["reply"].content
    assert "没有读到" in text and "地图" in text
    assert "盘点" not in text and "蓝图" not in text and text.count("？") == 1


async def test_followup_after_failed_guide_keeps_onboarding_mode(evidence):
    _, _, document = evidence
    previous = {"mode": "onboarding", "request": "我是新手，应该优先做什么？", "answer": None}
    message = "我已经有基地了，接下来呢"
    assert is_advice(message, previous)
    draft = guide(document)
    research = Research([document])
    result = await AdviceService(Model([draft, review(draft)]), research).run(message,
        previous_request=previous["request"], previous_answer=None)
    assert result["advice"]["mode"] == "onboarding" and research.calls == [(["first_day"], False)]


async def test_guide_search_topic_quality_fallback_cache_and_refresh(evidence):
    _, archive, _ = evidence
    calls = []

    async def resolver(host):
        return None

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "api.tavily.com":
            payload = json.loads(request.content)
            assert payload["query"] == TOPICS["first_day"][0]
            assert "falconrust.com" in payload["include_domains"]
            assert "private-server" not in json.dumps(payload)
            return httpx.Response(200, json={"results": [
                {"url": "https://rust.facepunch.com/news"},
                {"url": "https://www.rustafied.com/beginner-guide"}]})
        if request.url.host == "falconrust.com":
            return httpx.Response(503)
        return httpx.Response(200, text=GUIDE_HTML, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        settings = Settings(_env_file=None, search_api_key="test")
        research = WebResearch(settings, client, archive, resolver)
        first = await research.retrieve(["first_day", "private-server"])
        assert len(first["documents"]) == 1
        assert first["documents"][0]["url"] == "https://www.rustafied.com/beginner-guide"
        assert any(event["type"] == "irrelevant_body" for event in first["events"])
        count = len(calls)
        assert (await research.retrieve(["first_day"]))["cache_hit"]
        assert len(calls) == count
        assert not (await research.retrieve(["first_day"], force=True))["cache_hit"]
        assert len(calls) > count
        count = len(calls)
        settings.search_provider = "off"
        assert not (await research.retrieve(["first_day"]))["documents"]
        assert len(calls) == count


async def test_seed_is_read_without_search_key_and_unknown_topic_is_not_news(evidence):
    _, archive, _ = evidence
    calls = []

    async def resolver(host):
        return None

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=GUIDE_HTML, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        research = WebResearch(Settings(_env_file=None, search_api_key=""), client, archive, resolver)
        result = await research.retrieve(["first_day"])
        assert calls == [TOPICS["first_day"][1]] and result["documents"]
        assert any("COACH_SEARCH_API_KEY" in warning for warning in result["warnings"])
        count = len(calls)
        empty = await research.retrieve([])
        assert empty["documents"] == [] and len(calls) == count


@pytest.mark.parametrize("url,title", [("https://rust.facepunch.com/news", "Your First Day in Rust"),
    ("https://falconrust.com/guides/", "Beginner Guides"), (TOPICS["first_day"][1], "Rust Update News")])
def test_directories_and_news_are_not_beginner_guides(url, title):
    document = extract_document(url, GUIDE_HTML)
    document["title"] = title
    assert not relevant(document, ["first_day"])

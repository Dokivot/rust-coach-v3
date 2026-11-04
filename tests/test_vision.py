import asyncio
import base64
import io
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from rust_coach.app import create_app
from rust_coach.attachments.store import normalize_image
from rust_coach.config import Settings
from rust_coach.contracts import AppError, ImagePart, ModelMessage, ModelReply, TextPart
from rust_coach.llm import CompatibleChatModel
from rust_coach.player_state.schemas import ScopeCreate, StateUpdate
from rust_coach.vision.schemas import VisualConfirmation
from rust_coach.vision.drafts import ImageStateUpdate, VisualDraftStore
from rust_coach.calculations import Calculator
from rust_coach.advice.service import AdviceService


def png(color="green", size=(320, 180)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


class VisionModel:
    def __init__(self):
        self.calls = []
        self.task = "observe"
        self.coverage = "full_view"
        self.kind = "inventory"
        self.quantity = 100
        self.invalid = 0

    async def complete(self, messages):
        self.calls.append(messages)
        if self.invalid:
            self.invalid -= 1
            return ModelReply(content="invalid JSON")
        parts = next(message.content for message in messages if isinstance(message.content, list))
        context = json.loads(parts[0].text)
        identifier = context["images"][0]["attachment_id"]
        observation = {"id": "item_a", "attachment_id": identifier, "kind": self.kind,
            "region_kind": {"inventory": "backpack", "equipment": "equipped", "base": "scene"}[self.kind],
            "slot": "row_1_col_1", "label": "硫磺" if self.kind == "inventory" else "局部对象",
            "item_id": "sulfur" if self.kind == "inventory" else None,
            "quantity": self.quantity if self.kind == "inventory" else None,
            "quantity_status": "readable" if self.kind == "inventory" else "unknown",
            "bbox": [0.1, 0.2, 0.4, 0.6], "description": "截图局部可见"}
        result = {"task": self.task, "observations": [observation], "inventory_coverage": self.coverage}
        if self.task == "craft":
            result["calculation"] = {"operation": "craft", "goals": {"gunpowder": 10}}
        return ModelReply(content=json.dumps(result), usage={"total_tokens": 12})


@pytest.fixture
def setup(tmp_path):
    settings = Settings(_env_file=None, api_key="test", search_provider="off", database_path=tmp_path / "vision.sqlite3")
    model = VisionModel()
    application = create_app(settings, model)
    with TestClient(application) as client:
        vision = application.state.vision
        vision.drafts.metadata("probe:" + vision.config_id, {"status": "passed"})
        owner = client.post("/api/conversations", json={}).json()["id"]
        yield client, application, model, owner


def upload(client, owner, raw=None):
    response = client.post(f"/api/conversations/{owner}/attachments", files={"file": ("arbitrary.jpg", raw or png(), "image/jpeg")})
    assert response.status_code == 201, response.text
    return response.json()


def analyze(client, owner, attachment, **kwargs):
    response = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()), "message": "识别截图",
        "attachment_ids": [attachment["id"]], **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def confirmation(turn, **kwargs):
    visual = turn["visual"]
    return {"confirmation_id": str(uuid4()), "draft_revision": visual["draft_revision"], "expected_state_revision": visual["scope_revision"],
        "observed_at": datetime.now(timezone.utc).isoformat(), "subject": "self", "inventory_complete": True,
        "edits": [{"observation_id": item["id"], "accepted": True} for item in visual["observations"]], **kwargs}


def test_image_craft_switch_raid_health_and_correction_share_valid_evidence(setup):
    client, application, model, owner = setup
    model.task = "craft"
    first = analyze(client, owner, upload(client, owner), message="截图材料制作火药")
    original_id = first["visual"]["draft_id"]
    turns = []
    for message in ["改成拆一面石墙最省资源", "它还剩250血", "硫磺数量改为150"]:
        response = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()), "message": message})
        assert response.status_code == 200, response.text
        turns.append(response.json())
    raid, health, correction = turns
    assert raid["calculation"]["input"]["operation"] == "raid"
    assert raid["calculation"]["input"]["goals"] == {}
    assert raid["calculation"]["input"]["inventory"] == {"sulfur": 100}
    assert health["calculation"]["input"]["hp"] == 250
    assert correction["calculation"]["input"]["hp"] == 250
    assert correction["calculation"]["input"]["inventory"] == {"sulfur": 150}
    assert correction["visual"]["correction"]["previous_draft_id"] == original_id
    assert application.state.vision.drafts.get(owner, original_id)["payload"]["extraction"]["observations"][0]["quantity"] == 100
    assert len(model.calls) == 1
    assert "未写入玩家档案" in correction["assistant"]


def test_automatic_image_does_not_override_explicit_profile_inventory(setup):
    client, application, model, owner = setup
    states, scope = bind(application, owner)
    states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=0, observed_at=datetime.now(timezone.utc),
        changes={"inventory": [{"container_id": "backpack", "mode": "replace", "complete": True, "items": {"sulfur": 999}}]}))
    model.task = "craft"
    turn = analyze(client, owner, upload(client, owner), player_inventory_container="backpack")
    assert turn["calculation"]["status"] == "needs_input"
    assert turn["calculation"]["error_code"] == "ambiguous_inventory"
    assert turn["presentation"]["architecture"]["task"]["uncertainty"]["factual"] == "conflict"


def test_screenshot_craft_raid_explanation_and_revocation_use_one_release(setup):
    client, application, model, owner = setup
    model.task = "craft"
    first = analyze(client, owner, upload(client, owner), message="截图材料制作火药")
    turns = []
    for message in ["改成拆一面石墙最省资源", "它还剩250血", "为什么250血还需要一颗C4"]:
        response = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()), "message": message})
        assert response.status_code == 200, response.text
        turns.append(response.json())
    raid, health, explanation = turns
    reference = first["presentation"]["knowledge"]
    assert all(turn["presentation"]["knowledge"]["release_sha256"] == reference["release_sha256"] for turn in turns)
    assert raid["calculation"]["input"]["inventory"] == {"sulfur": 100}
    assert health["calculation"]["result"]["hp_each"] == 250
    assert {document["id"] for document in explanation["presentation"]["knowledge"]["documents"]} >= {
        "explain:rounding", "raid:building:base-wall:stone"}
    assert explanation["presentation"]["architecture"]["task"]["execution_node"] == "knowledge"
    application.state.runtime.knowledge.registry.revoke(reference["release_id"], "raid:building:base-wall:stone", "fixture withdrawal")
    response = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()), "message": "它还剩200血"})
    assert response.status_code == 200
    assert response.json()["calculation"]["error_code"] == "knowledge_evidence_unavailable"
    assert len(model.calls) == 1


def test_text_task_wins_when_vision_only_reports_observations(setup, monkeypatch):
    client, _, model, owner = setup
    original = model.complete

    async def with_extractor(messages):
        if isinstance(messages[-1].content, list):
            return await original(messages)
        return ModelReply(content='{"operation":"craft","goals":{"explosive.timed":1}}')

    monkeypatch.setattr(model, "complete", with_extractor)
    turn = analyze(client, owner, upload(client, owner), message="用截图材料制作一个C4")
    assert turn["visual"]["task"] == "observe"
    assert turn["calculation"]["input"]["inventory"] == {"sulfur": 100}
    assert turn["presentation"]["architecture"]["task"]["routing_reason"] == "text_task_with_visual_evidence"


def test_temporary_technology_does_not_invalidate_unbound_image(setup, monkeypatch):
    client, _, model, owner = setup
    model.task = "craft"
    first = analyze(client, owner, upload(client, owner))

    async def text_extractor(messages):
        payload = json.loads(messages[-1].content)
        return ModelReply(content=json.dumps(payload["previous_request"]))

    monkeypatch.setattr(model, "complete", text_extractor)
    response = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()),
        "message": "我有3级工作台，继续计算"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["calculation"]["input"]["inventory"] == {"sulfur": 100}
    assert result["calculation"]["input"]["workbench"] == 3
    assert result["visual"]["draft_id"] == first["visual"]["draft_id"]
    assert result["visual"]["fresh"] is True


def test_predeath_image_cannot_be_revived_by_reanalysis(setup):
    client, application, model, owner = setup
    states, scope = bind(application, owner)
    model.task = "craft"
    attachment = upload(client, owner)
    initial = analyze(client, owner, attachment)
    event = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()), "message": "我刚死了，背包丢了"})
    assert event.status_code == 200, event.text
    repeated = analyze(client, owner, attachment)
    assert initial["calculation"]["input"]["inventory"] == {"sulfur": 100}
    assert repeated["calculation"]["input"]["inventory"] == {}
    assert repeated["calculation"]["input_usage"]["inventory_source"] == "profile"
    assert repeated["calculation"]["visual_usage"]["inventory_used"] is False
    assert states.get(scope)["containers"]["backpack"]["items"] == {}


def confirm(client, owner, turn, payload):
    return client.post(f"/api/conversations/{owner}/visual-drafts/{turn['visual']['draft_id']}/confirm", json=payload)


def bind(application, owner):
    states = application.state.runtime.player_states
    scope = states.create(ScopeCreate(server_id="server", wipe_id="wipe", label="test"))["scope"]["id"]
    states.bind(owner, scope)
    return states, scope


def test_upload_content_sniffing_ownership_delete_and_thumbnail(setup):
    client, application, model, owner = setup
    attachment = upload(client, owner)
    assert client.get(attachment["url"]).headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(client.get(attachment["thumbnail_url"]).content)).size == (320, 180)
    other = client.post("/api/conversations", json={}).json()["id"]
    assert client.get(attachment["url"].replace(owner, other)).status_code == 404
    assert client.delete(attachment["url"].replace(owner, other)).status_code == 404
    assert client.delete(attachment["url"]).status_code == 204
    assert client.get(attachment["url"]).status_code == 410
    assert not list(application.state.attachments.root.glob("*.png"))


@pytest.mark.parametrize("raw,status", [(b"<svg>fake</svg>", 422), (b"GIF89a", 422)])
def test_invalid_files(setup, raw, status):
    client, _, _, owner = setup
    assert client.post(f"/api/conversations/{owner}/attachments", files={"file": ("x.png", raw)}).status_code == status


def test_limits_and_animation():
    with pytest.raises(AppError) as error:
        normalize_image(png(), 10, 100000)
    assert error.value.status == 413
    with pytest.raises(AppError) as error:
        normalize_image(png(), 100000, 100)
    assert error.value.code == "image_dimensions"
    output = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(output, format="PNG", save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")])
    with pytest.raises(AppError) as error:
        normalize_image(output.getvalue(), 100000, 100000)
    assert error.value.code == "image_format"


def test_streaming_upload_limit_returns_error(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "test.sqlite3", attachment_max_bytes=1024)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        owner = client.post("/api/conversations", json={}).json()["id"]
        response = client.post(f"/api/conversations/{owner}/attachments", content=iter([b"x" * 50000, b"y" * 50000]),
            headers={"content-type": "multipart/form-data; boundary=test"})
        assert response.status_code in {400, 413}
        response = client.post(f"/api/conversations/{owner}/attachments", files={"file": ("x.png", b"x" * 100000)})
        assert response.status_code == 413, response.text


def test_automatic_calculation_then_optional_save_and_cache(setup):
    client, application, model, owner = setup
    model.task = "craft"
    attachment = upload(client, owner)
    turn = analyze(client, owner, attachment)
    assert turn["calculation"]["input"]["inventory"] == {"sulfur":100}
    assert turn["calculation"]["visual_usage"]["source"] == "image_inferred"
    assert turn["calculation"]["visual_usage"]["confirmation_id"] is None
    assert turn["visual"]["usable"] is True
    assert turn["visual"]["confirmed"] is False
    payload = confirmation(turn)
    response = confirm(client, owner, turn, payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["receipt"]["saved"] is False
    assert result["turn"]["calculation"]["input"]["inventory"] == {"sulfur": 100}
    assert result["turn"]["calculation"]["visual_usage"]["draft_id"] == turn["visual"]["draft_id"]
    assert confirm(client, owner, turn, payload).json() == result
    assert confirm(client, owner, turn, {**payload, "subject": "opponent"}).status_code == 409
    assert len(model.calls) == 1
    cached = analyze(client, owner, upload(client, owner))
    assert cached["visual"]["cache_hit"] is True
    assert cached["visual"]["observations"][0]["attachment_id"] != attachment["id"]
    assert len(model.calls) == 1


def test_atomic_save_and_revision_conflict(setup):
    client, application, _, owner = setup
    states, scope = bind(application, owner)
    turn = analyze(client, owner, upload(client, owner))
    payload = confirmation(turn, action="save_to_profile", continue_task=False)
    result = confirm(client, owner, turn, payload)
    assert result.status_code == 200, result.text
    assert states.get(scope)["revision"] == 1
    assert states.get(scope)["containers"]["backpack"]["items"]["sulfur"]["value"] == 100
    record = next(iter(states.get(scope)["visual_observations"].values()))
    assert record["source"] == "image_confirmed"
    assert record["evidence_refs"][0]["sha256"]
    assert confirm(client, owner, turn, payload).json() == result.json()
    assert states.get(scope)["revision"] == 1
    other = analyze(client, owner, upload(client, owner))
    states.update(scope, StateUpdate(observation_id=uuid4(), expected_revision=1, observed_at=datetime.now(timezone.utc), changes={"facts": {"technology.workbench": 1}}))
    assert confirm(client, owner, other, confirmation(other, action="save_to_profile")).status_code == 409
    draft = application.state.vision.drafts.get(owner, other["visual"]["draft_id"])
    assert draft["status"] == "pending"
    assert states.get(scope)["revision"] == 2


def test_failed_save_rolls_back_draft_and_state(setup):
    client, application, _, owner = setup
    states, scope = bind(application, owner)
    turn = analyze(client, owner, upload(client, owner))
    response = confirm(client, owner, turn, confirmation(turn, action="save_to_profile", subject="opponent"))
    assert response.status_code == 422
    assert states.get(scope)["revision"] == 0
    assert application.state.vision.drafts.get(owner, turn["visual"]["draft_id"])["status"] == "pending"


def test_partial_stacks_never_patch_aggregate(setup):
    client, application, model, owner = setup
    model.coverage = "partial"
    model.task = "craft"
    states, scope = bind(application, owner)
    turn = analyze(client, owner, upload(client, owner))
    assert confirm(client, owner, turn, confirmation(turn)).status_code == 422
    result = confirm(client, owner, turn, confirmation(turn, inventory_complete=False, action="save_to_profile"))
    assert result.status_code == 200, result.text
    assert result.json()["turn"]["calculation"]["input"]["inventory"] is None
    assert states.get(scope)["containers"] == {}
    assert len(states.get(scope)["visual_observations"]) == 1


@pytest.mark.parametrize("kind", ["base", "equipment"])
def test_non_inventory_observations_saved_without_inventing_stats(setup, kind):
    client, application, model, owner = setup
    model.kind = kind
    model.coverage = "partial"
    states, scope = bind(application, owner)
    turn = analyze(client, owner, upload(client, owner))
    result = confirm(client, owner, turn, confirmation(turn, inventory_complete=False, action="save_to_profile", continue_task=False))
    assert result.status_code == 200, result.text
    snapshot = states.get(scope)
    assert snapshot["facts"] == snapshot["containers"] == {}
    assert next(iter(snapshot["visual_observations"].values()))["value"]["kind"] == kind
    assert states.get(scope, revision=0)["visual_observations"] == {}


@pytest.mark.parametrize("change,code", [
    ({"subject": "opponent", "action": "save_to_profile"}, "visual_subject_conflict"),
    ({"edits": [{"observation_id": "wrong", "accepted": True}]}, "visual_incomplete_review"),
    ({"edits": [{"observation_id": "item_a", "accepted": True, "quantity": None}]}, "visual_unknown_quantity"),
    ({"observed_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}, "future_observation"),
])
def test_confirmation_rejects_invalid_assumptions(setup, change, code):
    client, _, _, owner = setup
    turn = analyze(client, owner, upload(client, owner))
    response = confirm(client, owner, turn, confirmation(turn, **change))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code


def test_capture_time_cannot_be_refreshed_and_deleted_image_invalidates_draft(setup):
    client, application, model, owner = setup
    model.task = "craft"
    attachment = upload(client, owner)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    turn = analyze(client, owner, attachment, image_context={attachment["id"]: {"subject": "self", "captured_at": old}})
    result = confirm(client, owner, turn, confirmation(turn)).json()
    assert result["receipt"]["observed_at"] == old
    assert result["continuation_pending"] is True
    assert result["error"]["code"] == "visual_inventory_not_current"
    assert client.delete(attachment["url"]).status_code == 204
    with pytest.raises(AppError) as error:
        application.state.vision.drafts.get(owner, turn["visual"]["draft_id"])
    assert error.value.status == 410


def test_one_schema_repair_then_stop(setup):
    client, _, model, owner = setup
    model.invalid = 1
    analyze(client, owner, upload(client, owner))
    assert len(model.calls) == 2
    model.invalid = 2
    attachment = upload(client, owner, png("blue"))
    response = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()), "attachment_ids": [attachment["id"]]})
    assert response.status_code == 422
    assert len(model.calls) == 4


def test_probe_gate_and_request_ownership(setup):
    client, application, _, owner = setup
    attachment = upload(client, owner)
    other = client.post("/api/conversations", json={}).json()["id"]
    body = {"request_id": str(uuid4()), "attachment_ids": [attachment["id"]]}
    assert client.post(f"/api/conversations/{other}/messages", json=body).status_code == 404
    vision = application.state.vision
    vision.drafts.metadata("probe:" + vision.config_id, {"status": "failed"})
    assert client.get("/api/config").json()["vision"]["enabled"] is False
    assert client.post(f"/api/conversations/{owner}/messages", json=body).status_code == 503


@pytest.mark.parametrize("mode", ["responses", "chat_completions"])
def test_image_transport(mode):
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        if mode == "responses":
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    async def run():
        settings = Settings(_env_file=None, api_key="test", api_mode=mode)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await CompatibleChatModel(settings, client).complete([ModelMessage(role="user", content=[TextPart(text="hello"),
                ImagePart(data_url="data:image/png;base64," + base64.b64encode(png()).decode())])])
    asyncio.run(run())
    content = calls[0]["input" if mode == "responses" else "messages"][0]["content"]
    assert content[0]["type"] == ("input_text" if mode == "responses" else "text")
    assert content[1]["type"] == ("input_image" if mode == "responses" else "image_url")


@pytest.mark.parametrize("defect", ["different_containers", "different_images", "duplicate_slots", "unknown_item"])
def test_aggregate_inventory_rejects_ambiguous_inputs(setup, defect):
    client, application, _, owner = setup
    turn = analyze(client, owner, upload(client, owner))
    draft = application.state.vision.drafts.get(owner, turn["visual"]["draft_id"])
    payload = draft["payload"]
    original = payload["extraction"]["observations"][0]
    second = {**original, "id": "item_b", "slot": "row_1_col_2"}
    if defect == "different_containers":
        second["region_kind"] = "storage"
    elif defect == "different_images":
        second["attachment_id"] = str(uuid4())
    elif defect == "duplicate_slots":
        second["slot"] = original["slot"]
    else:
        second["item_id"] = None
    payload["extraction"]["observations"].append(second)
    request = VisualConfirmation.model_validate(confirmation(turn, edits=[{"observation_id":"item_a"},{"observation_id":"item_b"}]))
    with pytest.raises(AppError):
        VisualDraftStore.accept(payload, request, Calculator().catalog)


def test_duplicate_images_deduped_and_conflicting_context_rejected(setup):
    client, _, model, owner = setup
    first, second = upload(client, owner), upload(client, owner)
    turn = analyze(client, owner, first, attachment_ids=[first["id"], second["id"]])
    assert len(turn["visual"]["attachments"]) == 1
    assert len(model.calls) == 1
    response = client.post(f"/api/conversations/{owner}/messages",json={"request_id":str(uuid4()),
        "attachment_ids":[first["id"],second["id"]],"image_context":{first["id"]:{"subject":"self"},second["id"]:{"subject":"opponent"}}})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "duplicate_image_context"


def test_save_success_survives_continuation_failure(setup, monkeypatch):
    client, application, _, owner = setup
    states, scope = bind(application, owner)
    turn = analyze(client, owner, upload(client, owner))
    original_chat = application.state.runtime.chat
    async def failure(*args):
        raise RuntimeError("temporary")
    monkeypatch.setattr(application.state.runtime, "chat", failure)
    payload = confirmation(turn, action="save_to_profile")
    response = confirm(client, owner, turn, payload)
    assert response.status_code == 200, response.text
    assert response.json()["receipt"]["saved"] is True
    assert response.json()["continuation_pending"] is True
    monkeypatch.setattr(application.state.runtime, "chat", original_chat)
    replay = confirm(client, owner, turn, payload)
    assert replay.json()["turn"]
    assert states.get(scope)["revision"] == 1


def test_advice_uses_confirmed_ephemeral_visual_inventory(setup):
    client, application, model, owner = setup
    model.task = "craft"
    turn = analyze(client, owner, upload(client, owner))
    response = confirm(client, owner, turn, confirmation(turn)).json()
    visual = response["turn"]["visual"]
    service = AdviceService(model, None)
    state = {"request":{"visual":visual},"plan":{"candidates":[{"id":"craft","kind":"craft","goals":{"gunpowder":10},"required_fields":[]}]},"evidence":{"documents":[]}}
    result = service.evaluate(state)["evaluations"]["craft"]
    assert result["calculation"]["input"]["inventory"] == {"sulfur":100}
    assert result["calculation"]["visual_usage"]["confirmation_id"] == response["receipt"]["confirmation_id"]
    visual["fresh"] = False
    assert service.evaluate(state)["evaluations"]["craft"]["status"] == "unverified"


@pytest.mark.parametrize("kind,region,event", [("base","scene","raided"),("equipment","equipped","death"),("inventory","hotbar","death")])
def test_visual_observation_cannot_backfill_before_invalidation(setup, kind, region, event):
    _, application, _, owner = setup
    states, scope = bind(application, owner)
    now = datetime.now(timezone.utc)
    states.update(scope, StateUpdate(observation_id=uuid4(),expected_revision=0,observed_at=now,changes={"event":event}))
    request = ImageStateUpdate(observation_id=uuid4(),expected_revision=1,observed_at=now-timedelta(minutes=1),visual_draft_id=str(uuid4()),
        changes={"visual_observations":[{"id":"item_a","attachment_id":str(uuid4()),"kind":kind,"region_kind":region,"label":"test","bbox":[0,0,1,1]}]})
    with pytest.raises(AppError) as error:
        states.update(scope, request)
    assert error.value.code == "older_observation"


def test_drafts_persist_after_restart_and_cancel(setup):
    client, application, model, owner = setup
    turn = analyze(client, owner, upload(client, owner))
    settings = application.state.vision.settings
    with TestClient(create_app(settings,model)) as restarted:
        path = f"/api/conversations/{owner}/visual-drafts/{turn['visual']['draft_id']}"
        assert restarted.get(path).json()["status"] == "pending"
        assert restarted.post(path + "/cancel",json={}).status_code == 204
        assert restarted.get(path).json()["available"] is False
        assert confirm(restarted, owner, turn, confirmation(turn)).status_code == 410


def test_explicit_empty_complete_inventory_replaces_old_items(setup):
    client, application, _, owner = setup
    states, scope = bind(application, owner)
    states.update(scope,StateUpdate(observation_id=uuid4(),expected_revision=0,observed_at=datetime.now(timezone.utc),
        changes={"inventory":[{"container_id":"backpack","mode":"replace","complete":True,"items":{"sulfur":500}}]}))
    turn = analyze(client, owner, upload(client, owner))
    drafts = application.state.vision.drafts
    payload = drafts.get(owner,turn["visual"]["draft_id"])["payload"]
    payload["extraction"]["observations"] = []
    draft = drafts.create(owner,str(uuid4()),{**payload,"input_hash":"empty_fixture"})
    request = VisualConfirmation.model_validate(confirmation(turn,edits=[],action="save_to_profile",continue_task=False))
    receipt = drafts.confirm(owner,draft["id"],request,states,Calculator().catalog)
    assert receipt["inventory"] == {}
    container = states.get(scope)["containers"]["backpack"]
    assert container["items"] == {}
    assert container["usable_for_exact_calculation"] is True


def test_missing_original_file_blocks_confirmation(setup):
    client, application, _, owner = setup
    attachment = upload(client, owner)
    turn = analyze(client, owner, attachment)
    application.state.attachments.path(owner,attachment["id"]).unlink()
    assert confirm(client,owner,turn,confirmation(turn)).status_code == 410


def test_repaired_schema_receives_business_error(setup, monkeypatch):
    client, _, model, owner = setup
    model.task = "craft"
    original = model.complete
    captured = []
    async def mistaken(messages):
        captured.append(messages)
        reply = await original(messages)
        if len(captured) == 1:
            data = json.loads(reply.content)
            data["calculation"]["target"] = "gunpowder"
            return ModelReply(content=json.dumps(data))
        return reply
    monkeypatch.setattr(model,"complete",mistaken)
    analyze(client,owner,upload(client,owner))
    assert len(captured) == 2
    assert "Craft requests cannot contain raid parameters" in captured[1][-1].content


def test_old_visual_inventory_never_inherits_into_unrelated_calculation(setup, monkeypatch):
    client, application, model, owner = setup
    model.task = "craft"
    turn = analyze(client,owner,upload(client,owner))
    assert confirm(client,owner,turn,confirmation(turn)).status_code == 200
    seen = []
    original = application.state.runtime.graph.ainvoke
    async def record(state,*args,**kwargs):
        seen.append(state)
        return await original(state,*args,**kwargs)
    monkeypatch.setattr(application.state.runtime.graph,"ainvoke",record)
    response = client.post(f"/api/conversations/{owner}/messages",json={"request_id":str(uuid4()),
        "message":'/calc {"operation":"craft","goals":{"gunpowder":20}}'})
    assert response.status_code == 200
    assert seen[0]["previous_calculation"]["inventory"] is None
    assert response.json()["calculation"]["input"]["inventory"] is None


def test_explicit_calc_with_new_images_does_not_silently_rewrite_parameters(setup):
    client, _, model, owner = setup
    attachment = upload(client,owner)
    response = client.post(f"/api/conversations/{owner}/messages",json={"request_id":str(uuid4()),
        "message":'/calc {"operation":"craft","goals":{"gunpowder":20}}',"attachment_ids":[attachment["id"]]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "visual_command_conflict"
    assert model.calls == []


def test_opponent_candidates_cannot_be_relabelled_as_self(setup):
    client, application, _, owner = setup
    turn = analyze(client,owner,upload(client,owner))
    payload = application.state.vision.drafts.get(owner,turn["visual"]["draft_id"])["payload"]
    payload["extraction"]["observations"][0]["subject"] = "opponent"
    with pytest.raises(AppError) as error:
        VisualDraftStore.accept(payload,VisualConfirmation.model_validate(confirmation(turn)),Calculator().catalog)
    assert error.value.code == "visual_subject_conflict"


def test_vision_subject_candidate_preserved_until_user_confirmation(setup, monkeypatch):
    client, _, model, owner = setup
    original = model.complete
    async def opponent(messages):
        reply = await original(messages)
        data = json.loads(reply.content)
        data["observations"][0]["subject"] = "opponent"
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model,"complete",opponent)
    turn = analyze(client,owner,upload(client,owner))
    assert turn["visual"]["observations"][0]["subject"] == "opponent"
    assert turn["visual"]["confirmed"] is False
    assert confirm(client,owner,turn,confirmation(turn)).status_code == 422


@pytest.mark.parametrize("correct",[True,False])
def test_probe_checks_pixels_not_just_successful_transport(setup, monkeypatch, correct):
    client, application, _, _ = setup
    numbers = iter([123456,234567])
    monkeypatch.setattr("rust_coach.vision.probe.secrets.randbelow",lambda upper:next(numbers))
    captured = []
    class ProbeModel:
        async def complete(self,messages):
            captured.extend(messages)
            return ModelReply(content=json.dumps({"codes":["223456","334567"] if correct else ["000000","000000"]}))
    application.state.vision.model = ProbeModel()
    response = client.post("/api/vision/probe",json={})
    assert response.status_code == (200 if correct else 422)
    assert client.get("/api/config").json()["vision"]["enabled"] is correct
    parts = captured[0].content
    assert len([part for part in parts if isinstance(part,ImagePart)]) == 2
    assert "223456" not in parts[0].text and "334567" not in parts[0].text


def test_normalized_crop_uses_image_coordinates_and_removes_exif(setup):
    client, application, _, owner = setup
    image = Image.new("RGB",(100,50),"red")
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private metadata"
    output = io.BytesIO()
    image.save(output,format="JPEG",exif=exif)
    attachment = upload(client,owner,output.getvalue())
    raw = application.state.attachments.image(owner,attachment["id"])
    normalized = Image.open(io.BytesIO(raw))
    assert normalized.size == (50,100)
    assert not normalized.getexif()
    crop = application.state.attachments.image(owner,attachment["id"],[0,0,0.5,0.5])
    assert Image.open(io.BytesIO(crop)).size == (25,50)


@pytest.mark.parametrize("kind",["inventory","base","equipment"])
def test_screenshot_only_needs_no_time_or_confirmation(setup,kind):
    client, application, model, owner = setup
    states, scope = bind(application,owner)
    model.kind = kind
    turn = analyze(client,owner,upload(client,owner),message="")
    visual = turn["visual"]
    assert visual["usable"] and visual["fresh"]
    assert visual["automatic"]["subject"] == "self"
    assert visual["accepted"] is None and visual["confirmed"] is False
    assert all(item["subject"] == "self" for item in visual["observations"])
    assert "默认按你当前的情况分析" in turn["assistant"]
    assert "请核对识别卡片" not in turn["assistant"]
    assert "请在卡片中确认" not in turn["assistant"]
    assert states.get(scope)["revision"] == 0
    stored = client.get(f"/api/conversations/{owner}").json()["turns"][0]
    assert stored["visual"]["automatic"] == visual["automatic"]


@pytest.mark.parametrize("message,subject,temporal",[
    ("这是对手的背包","opponent","current"),
    ("这是队友的背包","teammate","current"),
    ("这是昨天的截图","self","historical"),
    ("这是公开示意图","self","example"),
])
def test_context_exceptions_never_become_current_self_inventory(setup,message,subject,temporal):
    client, _, model, owner = setup
    model.task = "craft"
    turn = analyze(client,owner,upload(client,owner),message=message)
    visual = turn["visual"]
    assert visual["automatic"]["subject"] == subject
    assert next(iter(visual["resolved_context"].values()))["temporal_context"] == temporal
    assert turn["calculation"]["status"] == "calculated"
    assert turn["calculation"]["input"]["inventory"] is None
    assert not turn["calculation"]["visual_usage"]["inventory_used"]


def test_related_conversation_reaches_context_inference(setup,monkeypatch):
    client, application, model, owner = setup
    application.state.runtime.store.append(owner,str(uuid4()),"接下来发的是昨天对手的装备图。","可以，发来吧。",False)
    original = model.complete
    seen = []
    async def contextual(messages):
        parts = next(message.content for message in messages if isinstance(message.content,list))
        context = json.loads(parts[0].text)
        seen.append(context)
        reply = await original(messages)
        data = json.loads(reply.content)
        data["screenshot_context"] = [{"attachment_id":context["images"][0]["attachment_id"],"subject":"opponent",
            "temporal_context":"historical","reason":"用户在上一轮说明截图归属和时间。"}]
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model,"complete",contextual)
    turn = analyze(client,owner,upload(client,owner),message="看看这张")
    assert "接下来发的是昨天对手的装备图。" in seen[0]["recent_user_context"]
    assert turn["visual"]["automatic"]["subject"] == "opponent"
    assert not turn["visual"]["fresh"]


def test_explicit_image_metadata_wins_over_inferred_subject(setup,monkeypatch):
    client, _, model, owner = setup
    original = model.complete
    async def opponent(messages):
        reply = await original(messages)
        data = json.loads(reply.content)
        data["observations"][0]["subject"] = "opponent"
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model,"complete",opponent)
    attachment = upload(client,owner)
    old = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    turn = analyze(client,owner,attachment,image_context={attachment["id"]:{"subject":"self","captured_at":old}})
    assert turn["visual"]["automatic"]["subject"] == "self"
    assert turn["visual"]["automatic"]["observed_at"] == old
    assert not turn["visual"]["fresh"]


@pytest.mark.parametrize("problem",["partial","unknown_quantity","unknown_item","ambiguous_slot"])
def test_automatic_calculation_preserves_unknowns_without_confirmation_gate(setup,monkeypatch,problem):
    client, _, model, owner = setup
    model.task = "craft"
    original = model.complete
    async def incomplete(messages):
        reply = await original(messages)
        data = json.loads(reply.content)
        if problem == "partial":
            data["inventory_coverage"] = "partial"
        elif problem == "unknown_quantity":
            data["observations"][0].update(quantity=None,quantity_status="unknown")
        elif problem == "unknown_item":
            data["observations"][0]["item_id"] = None
        else:
            data["observations"][0]["slot"] = ""
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model,"complete",incomplete)
    turn = analyze(client,owner,upload(client,owner))
    assert turn["calculation"]["status"] == "calculated"
    assert turn["calculation"]["input"]["inventory"] == ({"sulfur":100} if problem == "partial" else None)
    assert not turn["calculation"]["input"]["inventory_complete"]
    assert "识别结果尚未确认" not in turn["assistant"]


def test_advice_uses_automatic_inventory_immediately(setup):
    client, _, model, owner = setup
    turn = analyze(client,owner,upload(client,owner))
    service = AdviceService(model,None)
    state = {"request":{"visual":turn["visual"]},"plan":{"candidates":[{"id":"craft","kind":"craft","goals":{"gunpowder":10},"required_fields":[]}]},"evidence":{"documents":[]}}
    result = service.evaluate(state)["evaluations"]["craft"]
    assert result["calculation"]["input"]["inventory"] == {"sulfur":100}
    assert result["calculation"]["visual_usage"]["source"] == "image_inferred"
    assert service.visual_context(state["request"])["usable"] is True


def test_followup_context_correction_preserves_image_age_and_changes_inventory_use(setup,monkeypatch):
    client, _, model, owner = setup
    model.task = "craft"
    first = analyze(client,owner,upload(client,owner))
    original = model.complete
    async def contextual(messages):
        reply = await original(messages)
        data = json.loads(reply.content)
        data["screenshot_context"] = [{"attachment_id":data["observations"][0]["attachment_id"],
            "subject":"opponent","temporal_context":"historical","reason":"用户纠正为昨天对手的截图。"}]
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model,"complete",contextual)
    response = client.post(f"/api/conversations/{owner}/messages",json={"request_id":str(uuid4()),
        "message":"这张其实是昨天对手的背包"})
    assert response.status_code == 200,response.text
    corrected = response.json()
    assert corrected["visual"]["automatic"]["subject"] == "opponent"
    assert not corrected["visual"]["fresh"]
    assert corrected["visual"]["automatic"]["observed_at"] == first["visual"]["automatic"]["observed_at"]
    assert corrected["calculation"]["input"]["inventory"] is None
    assert corrected["visual"]["draft_id"] != first["visual"]["draft_id"]


def test_conflicting_image_and_observation_subjects_cannot_offset_inventory(setup,monkeypatch):
    client, _, model, owner = setup
    model.task = "craft"
    original = model.complete
    async def conflicting(messages):
        reply = await original(messages)
        data = json.loads(reply.content)
        data["observations"][0]["subject"] = "self"
        data["screenshot_context"] = [{"attachment_id":data["observations"][0]["attachment_id"],
            "subject":"opponent","temporal_context":"current","reason":"玩家说明这是对手的背包。"}]
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model,"complete",conflicting)
    turn = analyze(client,owner,upload(client,owner))
    assert turn["visual"]["automatic"]["subject"] == "unknown"
    assert turn["calculation"]["input"]["inventory"] is None


def test_cached_image_does_not_refresh_assumed_observation_time(setup):
    client, application, model, owner = setup
    model.task = "craft"
    attachment = upload(client,owner)
    old = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    with application.state.runtime.store.connection() as connection:
        connection.execute("UPDATE attachments SET created_at=? WHERE id=?",(old,attachment["id"]))
    first = analyze(client,owner,attachment)
    second = analyze(client,owner,upload(client,owner))
    assert second["visual"]["cache_hit"]
    assert second["visual"]["automatic"]["observed_at"] == first["visual"]["automatic"]["observed_at"] == old
    assert not second["visual"]["fresh"]
    assert second["calculation"]["input"]["inventory"] is None


@pytest.mark.parametrize("kind",["equipment","base"])
def test_noninventory_observations_do_not_imply_empty_inventory(setup,kind):
    client, _, model, owner = setup
    model.kind,model.task = kind,"craft"
    turn = analyze(client,owner,upload(client,owner))
    assert not turn["visual"]["automatic"]["inventory_complete"]
    assert turn["calculation"]["input"]["inventory"] is None


@pytest.mark.parametrize("intervening",[False,True])
def test_natural_c4_followup_uses_readable_partial_inventory(setup,monkeypatch,intervening):
    client, application, model, owner = setup
    original = model.complete
    async def inventory_and_calculation(messages):
        if not any(isinstance(message.content,list) for message in messages):
            return ModelReply(content=json.dumps({"operation":"craft","goals":{"explosive.timed":1}}))
        reply = await original(messages)
        data = json.loads(reply.content)
        template = data["observations"][0]
        rows = [("wood",8147),("stones",550),("metal.fragments",946),("charcoal",4),("sulfur",140),("gears",None)]
        data["observations"] = [{**template,"id":"slot_" + str(index),"item_id":item,"quantity":quantity,
            "quantity_status":"readable" if quantity is not None else "unknown","slot":"slot_" + str(index)}
            for index,(item,quantity) in enumerate(rows)]
        data["inventory_coverage"] = "partial"
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model,"complete",inventory_and_calculation)
    first = analyze(client,owner,upload(client,owner),message="")
    if intervening:
        application.state.runtime.store.append(owner,str(uuid4()),"谢谢","随时问我。",False)
    response = client.post(f"/api/conversations/{owner}/messages",json={"request_id":str(uuid4()),"message":"请问我合成一个C4还差多少资源？"})
    assert response.status_code == 200,response.text
    turn = response.json()
    assert turn["visual"]["draft_id"] == first["visual"]["draft_id"]
    assert turn["calculation"]["input"]["inventory"] == {"wood":8147,"stones":550,"metal.fragments":946,"charcoal":4,"sulfur":140}
    assert turn["calculation"]["result"]["inventory_used"] == {"metal.fragments":200,"sulfur":140,"charcoal":4}
    assert turn["calculation"]["result"]["acquisition"] == {"techparts":2,"lowgradefuel":60,"sulfur":2060,"charcoal":2996,"cloth":5}
    main = "\n".join(block["text"] for block in turn["presentation"]["blocks"] if block["placement"] == "main")
    assert "按截图里能确认的库存" in main and "实际缺口可能更少" in main
    details = "\n".join(block["text"] for block in turn["presentation"]["blocks"] if block["placement"] == "detail")
    assert "已抵扣库存" in details
    assert "库存未知" not in main
    assert len(model.calls) == 1


@pytest.mark.parametrize("boundary",["death","new_scope","cancelled","deleted","explicit_total","expired"])
def test_recent_screenshot_respects_context_boundaries(setup,monkeypatch,boundary):
    client, application, model, owner = setup
    first = analyze(client,owner,upload(client,owner))
    if boundary == "death":
        application.state.runtime.store.append(owner,str(uuid4()),"我死了，背包丢了。","先确认当前位置。",False)
    elif boundary == "new_scope":
        bind(application,owner)
    elif boundary == "cancelled":
        application.state.vision.drafts.cancel(owner,first["visual"]["draft_id"])
    elif boundary == "deleted":
        application.state.attachments.delete(owner,first["visual"]["attachments"][0]["id"])
    elif boundary == "expired":
        draft = application.state.vision.drafts.get(owner,first["visual"]["draft_id"])
        old = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        for context in draft["payload"]["resolved_context"].values():
            context["observed_at"] = old
        with application.state.runtime.store.connection() as connection:
            connection.execute("UPDATE visual_drafts SET payload=? WHERE id=?",(json.dumps(draft["payload"]),draft["id"]))
    async def calculate(messages):
        return ModelReply(content=json.dumps({"operation":"craft","goals":{"explosive.timed":1}}))
    monkeypatch.setattr(model,"complete",calculate)
    question = "只算总需求：合成一个C4" if boundary == "explicit_total" else "合成一个C4还差多少资源？"
    response = client.post(f"/api/conversations/{owner}/messages",json={"request_id":str(uuid4()),"message":question})
    assert response.status_code == 200,response.text
    assert response.json()["calculation"]["input"]["inventory"] is None


def test_partial_inventory_advice_does_not_claim_complete_budget(setup):
    client, _, model, owner = setup
    model.coverage = "partial"
    turn = analyze(client,owner,upload(client,owner))
    service = AdviceService(model,None)
    state = {"request":{"visual":turn["visual"]},"plan":{"candidates":[{"id":"craft","kind":"craft","goals":{"gunpowder":10},"required_fields":[]}]},"evidence":{"documents":[]}}
    result = service.evaluate(state)["evaluations"]["craft"]
    assert result["calculation"]["result"]["inventory_used"] == {"sulfur":20}
    assert result["status"] == "inventory_unknown"
    assert result["calculation"]["visual_usage"]["complete"] is False


@pytest.mark.parametrize("explicit_status",[False,True])
def test_same_turn_c4_answers_first_with_quality_warning_and_recognition_details(setup,monkeypatch,explicit_status):
    client, application, model, owner = setup
    model.task = "craft"
    original = model.complete
    async def quality_notes(messages):
        reply = await original(messages)
        data = json.loads(reply.content)
        template = data["observations"][0]
        data["observations"] = [{**template,"id":item,"item_id":item,"label":label,"quantity":quantity,
            "slot":"row_1_col_" + str(index),"uncertainty":["图标受低分辨率影响。"],
            **({"item_status":"identified"} if explicit_status else {})}
            for index,(item,label,quantity) in enumerate([("charcoal","木炭",4),("sulfur","硫磺",140)],1)]
        data["inventory_coverage"] = "partial"
        data["calculation"] = {"operation":"craft","goals":{"explosive.timed":1}}
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model,"complete",quality_notes)
    turn = analyze(client,owner,upload(client,owner),message="我合成C4还差什么？")
    assert turn["calculation"]["input"]["inventory"] == {"charcoal":4,"sulfur":140}
    assert turn["calculation"]["result"]["inventory_used"] == {"charcoal":4,"sulfur":140}
    assert turn["calculation"]["result"]["acquisition"]["charcoal"] == 2996
    assert turn["calculation"]["result"]["acquisition"]["sulfur"] == 2060
    assert turn["presentation"]["blocks"][0]["id"] == "calculation_summary"
    assert turn["assistant"].startswith("按截图里能确认的库存，制作 1 个 C4")
    recognition = next(block for block in turn["presentation"]["blocks"] if block["id"] == "visual_scope")
    assert recognition["placement"] == "detail" and "• 木炭 × 4" in recognition["text"]
    assert "• 木炭 × 4" not in turn["assistant"]
    assert "部分图标有画质提醒" in recognition["text"]
    assert "部分图标有画质提醒" not in turn["assistant"]
    draft = application.state.vision.drafts.get(owner,turn["visual"]["draft_id"])
    for item in draft["payload"]["extraction"]["observations"]:
        item.pop("item_status",None)
    draft["payload"]["automatic"]["inventory"] = {}
    restored = application.state.vision.evidence(draft)
    assert restored["automatic"]["inventory"] == {"charcoal":4,"sulfur":140}
    assert restored["automatic"]["observed_at"] == turn["visual"]["automatic"]["observed_at"]
    service = AdviceService(model,None)
    state = {"request":{"visual":restored},"plan":{"candidates":[{"id":"craft","kind":"craft","goals":{"explosive.timed":1},"required_fields":[]}]},"evidence":{"documents":[]}}
    evaluated = service.evaluate(state)["evaluations"]["craft"]
    assert evaluated["calculation"]["result"]["inventory_used"] == {"charcoal":4,"sulfur":140}


@pytest.mark.parametrize("uncertainty,status",[
    ("无法可靠确认具体物品。",None),
    ("图标受低分辨率影响。","ambiguous"),
    ("", "unknown"),
])
def test_unresolved_identity_is_excluded_and_displayed_as_unresolved(setup,monkeypatch,uncertainty,status):
    client, _, model, owner = setup
    model.task = "craft"
    original = model.complete
    async def ambiguous(messages):
        reply = await original(messages)
        data = json.loads(reply.content)
        data["observations"][0].update(uncertainty=[uncertainty] if uncertainty else [],item_status=status)
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model,"complete",ambiguous)
    turn = analyze(client,owner,upload(client,owner),message="制作10火药还差什么？")
    assert turn["calculation"]["input"]["inventory"] is None
    assert "本次截图识别结果没有可抵扣的库存" in turn["assistant"]
    recognition = next(block for block in turn["presentation"]["blocks"] if block["id"] == "visual_scope")
    assert "物品身份未确定，未计入" in recognition["text"]


def test_automatic_subject_is_not_presented_as_an_explicit_unknown_owner(setup):
    client, _, model, owner = setup
    attachment = upload(client,owner)
    turn = analyze(client,owner,attachment,image_context={attachment["id"]:{"subject":"unknown"}})
    parts = next(message.content for message in model.calls[0] if isinstance(message.content,list))
    context = json.loads(parts[0].text)
    assert "subject" not in context["images"][0]
    assert "captured_at" not in context["images"][0]
    assert turn["visual"]["automatic"]["subject"] == "self"


@pytest.mark.parametrize("coverage", ["partial", "full_view"])
def test_unknown_container_uses_readable_inventory_for_ten_c4_then_raid(setup, monkeypatch, coverage):
    client, application, model, owner = setup
    original = model.complete
    rows = [("wood", 8147), ("metal.fragments", 550), ("cloth", 946), ("charcoal", 4), ("sulfur", 140)]
    async def cropped_inventory(messages):
        reply = await original(messages)
        data = json.loads(reply.content)
        template = data["observations"][0]
        data.update(task="craft", inventory_coverage=coverage,
            calculation={"operation": "craft", "goals": {"explosive.timed": 10}},
            screenshot_context=[{"attachment_id": template["attachment_id"], "subject": "unknown", "temporal_context": "current"}])
        data["observations"] = [{**template, "id": "item_" + str(index), "item_id": item, "label": item,
            "quantity": quantity, "item_status": "identified", "region_kind": "unknown",
            "slot": "row_1_col_" + str(index)} for index, (item, quantity) in enumerate(rows, 1)]
        data["observations"].append({**template, "id": "unresolved", "item_id": None, "quantity": None,
            "quantity_status": "unknown", "region_kind": "unknown", "slot": "row_2_col_1"})
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model, "complete", cropped_inventory)
    first = analyze(client, owner, upload(client, owner), message="我还差多少资源才能造10个C4？")
    assert first["calculation"]["input"]["inventory"] == dict(rows)
    assert first["calculation"]["result"]["acquisition"] == {
        "techparts": 20, "metal.fragments": 1450, "lowgradefuel": 600, "sulfur": 21860, "charcoal": 29996}
    assert not first["visual"]["automatic"]["inventory_complete"]
    assert first["visual"]["automatic"]["excluded_observations"] == {"unresolved": "identity_unknown"}
    assert "按截图里能确认的库存" in first["assistant"]
    assert "实际缺口可能更少" in first["assistant"]
    assert "库存未知" not in first["assistant"]
    details = next(block["text"] for block in first["presentation"]["blocks"] if block["id"] == "visual_scope")
    assert "默认将可见格子作为你的局部库存" in details
    assert "数量或槽位范围未确定" not in details
    restored = application.state.vision.evidence(application.state.vision.drafts.get(owner, first["visual"]["draft_id"]))
    assert restored["automatic"]["inventory"] == dict(rows)
    response = client.post(f"/api/conversations/{owner}/messages", json={"request_id": str(uuid4()),
        "message": "拆一面石墙最省资源的方案是什么？"})
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["calculation"]["operation"] == "raid"
    assert second["calculation"]["input"]["goals"] == {}
    assert second["calculation"]["result"]["hp_each"] == 500
    assert second["calculation"]["result"]["options"][0]["count"] == 2
    assert second["calculation"]["result"]["options"][0]["new_sulfur"] == 4260
    assert "暂按满血计算" in second["assistant"]
    assert len(model.calls) == 1


@pytest.mark.parametrize("changes,reason,explanation", [
    ({"region_kind": "crafting"}, "not_inventory", "不是可用库存区域"),
    ({"slot": ""}, "slot_missing", "槽位位置未确定"),
    ({"quantity": None, "quantity_status": "unknown"}, "quantity_unreadable", "数量未读清"),
])
def test_excluded_inventory_displays_actual_reason(setup, monkeypatch, changes, reason, explanation):
    client, _, model, owner = setup
    model.task, model.coverage = "craft", "partial"
    original = model.complete
    async def unusable_inventory(messages):
        reply = await original(messages)
        data = json.loads(reply.content)
        data["observations"][0].update({"region_kind": "unknown", **changes})
        return ModelReply(content=json.dumps(data))
    monkeypatch.setattr(model, "complete", unusable_inventory)
    turn = analyze(client, owner, upload(client, owner), message="制作10火药还差什么？")
    assert turn["calculation"]["input"]["inventory"] is None
    assert turn["visual"]["automatic"]["excluded_observations"] == {"item_a": reason}
    details = next(block["text"] for block in turn["presentation"]["blocks"] if block["id"] == "visual_scope")
    assert explanation in details
    assert "按已辨认物品和清晰数量计算" not in details
    assert "具体原因见截图识别详情" in turn["assistant"]


def test_stream_reports_real_vision_and_calculation_stages_and_skips_cached_recognition(setup):
    client, _, model, owner = setup
    model.task = "craft"
    attachment = upload(client, owner)
    payload = {"request_id": str(uuid4()), "message": "合成火药还缺什么？", "attachment_ids": [attachment["id"]]}

    def stream(request):
        response = client.post(f"/api/conversations/{owner}/messages", json=request, headers={"Accept": "text/event-stream"})
        return [json.loads(frame[6:]) for frame in response.text.split("\n\n") if frame.startswith("data: ")]

    events = stream(payload)
    stages = [event["stage"] for event in events if event["type"] == "stage"]
    assert stages == ["context", "image_context", "recognize_image", "check_image", "understand",
        "calculate_craft", "compose", "check_answer", "save"]
    assert events[-1]["turn"]["calculation"]["result"]["inventory_used"] == {"sulfur": 20}
    cached = stream({**payload, "request_id": str(uuid4())})
    assert cached[-1]["type"] == "result"
    assert "recognize_image" not in [event.get("stage") for event in cached]
    assert len(model.calls) == 1


@pytest.mark.parametrize("verbosity", ["standard", "detailed"])
def test_two_c4_answer_keeps_budget_and_does_not_echo_visual_unknowns(setup, monkeypatch, verbosity):
    client, application, model, owner = setup
    original = model.complete
    notes = [
        "截图仅显示库存局部，无法确认完整库存范围。",
        "未看到定时炸弹、炸药、电子零件等清晰数量，因此无法从本图确定还差多少资源。",
        "目标为2个定时炸弹；未提供工作台等级或蓝图信息。",
    ]

    async def inventory(messages):
        parsed = json.loads((await original(messages)).content)
        template = parsed["observations"][0]
        parsed.update(task="craft", inventory_coverage="partial", unknowns=notes,
            calculation={"operation": "craft", "goals": {"explosive.timed": 2}})
        parsed["observations"] = [{**template, "id": identifier, "item_id": identifier, "label": label,
            "slot": "slot_" + identifier, "quantity": amount}
            for identifier, label, amount in [("sulfur", "硫磺", 140), ("charcoal", "木炭", 4), ("cloth", "布", 10)]]
        return ModelReply(content=json.dumps(parsed, ensure_ascii=False))

    monkeypatch.setattr(model, "complete", inventory)
    turn = analyze(client, owner, upload(client, owner), message="合成两个C4还差多少资源？",
        presentation_preferences={"verbosity": verbosity})
    result = turn["calculation"]["result"]
    assert result["acquisition"] == {"techparts": 4, "metal.fragments": 400, "lowgradefuel": 120,
        "sulfur": 4260, "charcoal": 5996}
    assert result["inventory_used"] == {"cloth": 10, "sulfur": 140, "charcoal": 4}
    blocks = {block["id"]: block for block in turn["presentation"]["blocks"]}
    summary = blocks["calculation_summary"]["text"]
    assert summary.startswith("按截图里能确认的库存，制作 2 个 C4 还差：")
    for text in ("4 电子零件", "400 金属碎片", "120 低品质燃油", "4260 硫磺", "5996 木炭"):
        assert "• " + text in summary
    assert summary.count("实际缺口可能更少") == 1
    if verbosity == "standard":
        assert "制作还需要 3 级工作台，并确保相关配方能制作" in summary
        assert turn["assistant"] == summary
        assert blocks["rule_limit"]["placement"] == blocks["visual_scope"]["placement"] == "detail"
        assert all(text not in turn["assistant"] for text in ("尚未提供", "待核对配方", "截图识别结果", "尚不明确", "build"))
    else:
        assert blocks["rule_limit"]["placement"] == blocks["original_report"]["placement"] == "main"
        assert "来源：" in turn["assistant"]
    assert all(note not in block["text"] for note in notes for block in blocks.values())
    assert turn["visual"]["unknowns"] == notes
    observation = next(assertion for assertion in turn["presentation"]["assertions"] if assertion["kind"] == "observation")
    assert observation["value"]["unknowns"] == notes
    assert len(model.calls) == 1
    stored = application.state.runtime.store.get(owner).turns[0]
    assert stored.assistant == turn["assistant"] and stored.visual["unknowns"] == notes

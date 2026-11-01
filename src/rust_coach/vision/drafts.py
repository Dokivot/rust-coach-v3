import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from pydantic import Field

from rust_coach.contracts import AppError
from rust_coach.player_state.domain import INVENTORY_TTL_MINUTES
from rust_coach.player_state.schemas import StateChanges, StateUpdate
from rust_coach.presentation.validation import signature
from rust_coach.vision.schemas import VisualConfirmation, VisualObservation


class ImageStateUpdate(StateUpdate):
    source: Literal["image_confirmed"] = "image_confirmed"
    visual_draft_id: str
    evidence_refs: list[dict] = Field(default_factory=list)


class VisualDraftStore:
    def __init__(self, store, attachments, settings):
        self.store, self.attachments, self.settings = store, attachments, settings
        with store.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS visual_drafts (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(conversation_id,request_id));
                CREATE TABLE IF NOT EXISTS visual_confirmations (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    confirmation_id TEXT NOT NULL, draft_id TEXT NOT NULL REFERENCES visual_drafts(id),
                    payload_hash TEXT NOT NULL, receipt TEXT NOT NULL,
                    PRIMARY KEY(conversation_id,confirmation_id));
                CREATE TABLE IF NOT EXISTS vision_metadata (key TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS vision_cache (
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    key TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(conversation_id,key));
            """)

    def metadata(self, key, payload=None):
        with self.store.connection() as connection:
            if payload is not None:
                connection.execute("INSERT INTO vision_metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, json.dumps(payload)))
                return payload
            row = connection.execute("SELECT payload FROM vision_metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def cache(self, conversation_id, key, payload=None):
        with self.store.connection() as connection:
            if payload is not None:
                connection.execute("INSERT INTO vision_cache VALUES (?,?,?,?) ON CONFLICT(conversation_id,key) DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at",
                                   (conversation_id, key, json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
                return payload
            row = connection.execute("SELECT payload,created_at FROM vision_cache WHERE conversation_id=? AND key=?", (conversation_id,key)).fetchone()
            if row and datetime.fromisoformat(row["created_at"]) + timedelta(hours=self.settings.visual_draft_hours) > datetime.now(timezone.utc):
                return json.loads(row["payload"])
            return None

    def create(self, conversation_id, request_id, payload):
        now = datetime.now(timezone.utc)
        identifier = str(uuid4())
        with self.store.connection() as connection:
            row = connection.execute("SELECT id,payload FROM visual_drafts WHERE conversation_id=? AND request_id=?", (conversation_id, request_id)).fetchone()
            if row:
                if json.loads(row["payload"])["input_hash"] != payload["input_hash"]:
                    raise AppError("request_conflict", "该请求标识已用于不同的图片或问题。", 409)
                return self.get(conversation_id, row["id"])
            connection.execute("INSERT INTO visual_drafts VALUES (?,?,?,?,?,?,?,?)", (identifier,conversation_id,request_id,1,"pending",now.isoformat(),
                (now + timedelta(hours=self.settings.visual_draft_hours)).isoformat(),json.dumps(payload,ensure_ascii=False)))
        return self.get(conversation_id, identifier)

    def find_request(self, conversation_id, request_id, input_hash):
        with self.store.connection() as connection:
            row = connection.execute("SELECT id,payload FROM visual_drafts WHERE conversation_id=? AND request_id=?", (conversation_id,request_id)).fetchone()
        if row:
            if json.loads(row["payload"])["input_hash"] != input_hash:
                raise AppError("request_conflict", "该请求标识已用于不同的图片或问题。", 409)
            return self.get(conversation_id,row["id"])
        return None

    def get(self, conversation_id, identifier, *, allow_inactive=False):
        with self.store.connection() as connection:
            row = connection.execute("SELECT * FROM visual_drafts WHERE id=? AND conversation_id=?", (identifier,conversation_id)).fetchone()
        if row is None:
            raise AppError("visual_draft_not_found", "截图草稿不存在或不属于本会话。", 404)
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        expired = datetime.fromisoformat(result["expires_at"]) <= datetime.now(timezone.utc)
        result["available"] = not expired and result["status"] != "cancelled"
        for attachment in result["payload"]["attachments"]:
            try:
                current = self.attachments.get(conversation_id,attachment["id"],allow_deleted=True)
                result["available"] = result["available"] and current["available"]
            except AppError:
                result["available"] = False
        if not result["available"] and not allow_inactive:
            raise AppError("visual_draft_expired", "草稿已取消、过期或原图已删除，请重新选择图片。", 410)
        return result

    def cancel(self, conversation_id, identifier):
        draft = self.get(conversation_id,identifier,allow_inactive=True)
        if draft["status"] == "accepted":
            raise AppError("visual_already_confirmed", "该草稿已确认；取消不能撤销已经保存的状态。", 409)
        with self.store.connection() as connection:
            connection.execute("UPDATE visual_drafts SET status='cancelled',revision=revision+1 WHERE id=? AND status='pending'", (identifier,))

    def confirm(self, conversation_id, identifier, request: VisualConfirmation, player_states, catalog):
        request_hash = signature(request.model_dump(mode="json"))
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute("SELECT * FROM visual_confirmations WHERE conversation_id=? AND confirmation_id=?", (conversation_id,str(request.confirmation_id))).fetchone()
            if replay:
                if replay["payload_hash"] != request_hash or replay["draft_id"] != identifier:
                    raise AppError("confirmation_conflict", "确认标识已用于不同内容，请重新核对。", 409)
                return json.loads(replay["receipt"])
            row = connection.execute("SELECT * FROM visual_drafts WHERE id=? AND conversation_id=?", (identifier,conversation_id)).fetchone()
            if not row:
                raise AppError("visual_draft_not_found", "截图草稿不存在。", 404)
            draft = self.get(conversation_id,identifier)
            if draft["revision"] != request.draft_revision or draft["status"] != "pending":
                raise AppError("visual_revision_conflict", "草稿已被确认或修改，请刷新后核对。", 409)
            payload = draft["payload"]
            scope_id = player_states.binding(conversation_id)
            if scope_id != payload["scope_id"]:
                raise AppError("visual_scope_conflict", "会话关联档案已变化，请重新识别或切回原档案。", 409)
            if scope_id:
                current = player_states.get(scope_id)
                expected = request.expected_state_revision
                if expected is None or expected != payload["scope_revision"] or expected != current["revision"]:
                    raise AppError("state_revision_conflict", "档案在识别后发生变化，请重新提交图片并核对新状态。", 409)
            accepted, inventory, complete, observed_at = self.accept(payload,request,catalog)
            expires_at = observed_at + timedelta(minutes=INVENTORY_TTL_MINUTES)
            receipt = {"draft_id":identifier,"confirmation_id":str(request.confirmation_id),"action":request.action,
                "observed_at":observed_at.isoformat(),"confirmed_at":datetime.now(timezone.utc).isoformat(),
                "expires_at":expires_at.isoformat(),"observations":accepted,"inventory":inventory,
                "inventory_complete":complete,"container_id":request.container_id,"subject":request.subject,
                "calculation":request.calculation.model_dump(mode="json") if request.calculation else payload["extraction"].get("calculation"),
                "continue_task":request.continue_task,"saved":False,"state_revision":None}
            if request.action == "save_to_profile":
                if scope_id is None or request.subject != "self":
                    raise AppError("visual_save_scope", "只能把本人观测保存到已关联的玩家档案。对手或历史分析可仅用于本轮。", 422)
                declared = {context.get("subject") for context in payload["image_context"].values()}
                declared.update(item["subject"] for item in payload["extraction"]["observations"])
                if declared & {"opponent", "teammate"}:
                    raise AppError("visual_subject_conflict", "图片原先声明为他人，不能直接保存成本人状态。请重新提交并正确标明主体。", 422)
                if expires_at <= datetime.now(timezone.utc):
                    raise AppError("visual_observation_stale", "截图观测已过期，请重新盘点后保存。", 422)
                changes = {"visual_observations":accepted}
                if complete or inventory and request.quantities_are_container_totals:
                    changes["inventory"] = [{"container_id":request.container_id,"mode":"replace" if complete else "patch",
                                             "complete":complete,"items":inventory}]
                update = ImageStateUpdate(observation_id=request.confirmation_id,expected_revision=request.expected_state_revision,
                    observed_at=observed_at,visual_draft_id=identifier,
                    evidence_refs=[{"attachment_id":item["id"],"sha256":item["sha256"]} for item in payload["attachments"]],
                    changes=StateChanges.model_validate(changes))
                snapshot = player_states.update(scope_id,update,connection=connection)
                receipt.update(saved=True,state_revision=snapshot["revision"])
            payload["accepted"] = receipt
            connection.execute("UPDATE visual_drafts SET status='accepted',revision=revision+1,payload=? WHERE id=?", (json.dumps(payload,ensure_ascii=False),identifier))
            connection.execute("INSERT INTO visual_confirmations VALUES (?,?,?,?,?)",(conversation_id,str(request.confirmation_id),identifier,request_hash,json.dumps(receipt,ensure_ascii=False)))
        return receipt

    @staticmethod
    def accept(payload, request, catalog):
        now = datetime.now(timezone.utc)
        observed_at = request.observed_at.astimezone(timezone.utc)
        if observed_at > now + timedelta(minutes=1):
            raise AppError("future_observation", "观测时间不能在未来。", 422)
        times = [datetime.fromisoformat(context["captured_at"]) for context in payload["image_context"].values() if context.get("captured_at")]
        if times:
            observed_at = min(observed_at,*times)
        originals = {item["id"]:item for item in payload["extraction"]["observations"]}
        if {edit.observation_id for edit in request.edits} != originals.keys():
            raise AppError("visual_incomplete_review", "请逐项核对全部观测，明确接受或排除。", 422)
        subjects = {originals[edit.observation_id]["subject"] for edit in request.edits if edit.accepted} - {"unknown"}
        if len(subjects) > 1:
            raise AppError("visual_mixed_subjects", "这份草稿包含多个主体，请分别确认，或仅作未确认的条件对比。", 422)
        if subjects and request.subject != "unknown" and request.subject not in subjects:
            raise AppError("visual_subject_conflict", "确认主体与图片观测不一致，请重新提交并明确正确主体。", 422)
        accepted = []
        for edit in request.edits:
            if not edit.accepted:
                continue
            original = originals[edit.observation_id]
            values = {**original,"subject":request.subject}
            if "item_id" in edit.model_fields_set:
                values["item_id"] = edit.item_id
            if "quantity" in edit.model_fields_set:
                values.update(quantity=edit.quantity,quantity_status="readable" if edit.quantity is not None else "unknown")
            if edit.description is not None:
                values["description"] = edit.description
            accepted.append(VisualObservation.model_validate(values).model_dump(mode="json"))
        if not accepted and not (request.inventory_complete and not originals and payload["extraction"].get("inventory_coverage") == "full_view"):
            raise AppError("visual_empty_confirmation", "未选择可用观测；可以取消草稿。", 422)
        inventory_rows = [item for item in accepted if item["kind"] == "inventory" and item["region_kind"] not in {"crafting","unknown","scene","equipped"}]
        original_inventory = [item for item in originals.values() if item["kind"] == "inventory"]
        complete = request.inventory_complete
        if complete and payload["extraction"].get("inventory_coverage") != "full_view":
            raise AppError("visual_partial_image", "截图未覆盖完整库存。请上传完整容器截图，或在玩家状态页手动完整盘点。", 422)
        if complete and len(inventory_rows) != len(original_inventory):
            raise AppError("visual_inventory_coverage", "存在未核对或未归属的库存格子，不能标为完整盘点。", 422)
        if inventory_rows and (complete or request.quantities_are_container_totals):
            regions = {(item["region_kind"],item["container_label"]) for item in inventory_rows}
            if len(regions) != 1:
                raise AppError("visual_multiple_containers", "不能把不同库存区域合并为一个容器。请分别提交或保留为观测。", 422)
            image_ids = {item["attachment_id"] for item in inventory_rows}
            if len(image_ids) > 1 and not request.same_snapshot:
                raise AppError("visual_image_relationship", "多图库存须确认属于同一时点、同一容器且不重复。", 422)
            slots = [item["slot"] for item in inventory_rows if item["slot"]]
            if len(slots) != len(set(slots)):
                raise AppError("visual_duplicate_slots", "库存槽位可能重复，不能相加。请用一张完整截图或手动录入总量。", 422)
        inventory = Counter()
        if not complete and not request.quantities_are_container_totals:
            inventory_rows = []
        for item in inventory_rows:
            if item["item_id"] is None or item["quantity"] is None:
                if complete:
                    raise AppError("visual_unknown_quantity", "完整库存仍有未知物品或数量，请修正后确认。", 422)
                continue
            inventory[item["item_id"]] += item["quantity"]
        if any(value > 1_000_000 for value in inventory.values()):
            raise AppError("visual_quantity_range", "确认后的合计数量超出支持范围。", 422)
        if request.calculation:
            catalog.require_items(set(request.calculation.goals) | set(request.calculation.methods))
        return accepted,dict(inventory),complete,observed_at

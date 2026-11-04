from datetime import datetime, timezone

from rust_coach.calculations.schemas import CalculationRequest
from rust_coach.contracts import AppError
from rust_coach.player_state.domain import calculation_inventory, project
from rust_coach.vision.context import visual_input


def resolve_calculation_input(request, snapshot, container, visual, supported, selection=None):
    accepted = visual_input(visual)
    excluded = []
    if selection in {"total", "image", "text", "backpack", "storage"}:
        if accepted and selection != "image":
            excluded.append({"source": "image", "reason": "explicit_inventory_selection"})
            accepted = None
        if request.inventory is not None and selection != "text":
            excluded.append({"source": "text", "reason": "explicit_inventory_selection"})
            request = request.model_copy(update={"inventory": None, "inventory_complete": False})
        if selection in {"backpack", "storage"}:
            container = selection
        else:
            if container:
                excluded.append({"source": "profile", "reason": "explicit_inventory_selection"})
            container = None
        if selection == "total":
            request = request.model_copy(update={"inventory": None, "inventory_complete": False, "reserve": {}})
    updates = {}
    usage = {"inventory_source": "text" if request.inventory is not None else "unknown", "excluded_sources": excluded}
    image_inventory = bool(accepted and (accepted["inventory_complete"] or accepted["inventory"]))
    image_current = bool(accepted and visual["fresh"] and accepted["subject"] == "self" and
                         datetime.fromisoformat(accepted["expires_at"]) > datetime.now(timezone.utc))
    if image_current and snapshot:
        keys = ["inventory.*", "inventory." + accepted.get("container_id", "backpack")]
        if accepted.get("container_id", "backpack") not in {"storage", "tool_cupboard"}:
            keys.append("visual.nonbase")
        if any(key in snapshot.get("invalidations", {}) and
               datetime.fromisoformat(accepted["observed_at"]) <= datetime.fromisoformat(snapshot["invalidations"][key])
               for key in keys):
            image_current = False
    if accepted and not image_current:
        usage["excluded_sources"].append({"source": "image", "reason": "not_current_self_inventory"})
        if visual.get("accepted") and image_inventory:
            raise AppError("visual_inventory_not_current", "这份截图库存不是当前本人库存，请重新盘点或只算总需求。", 422)
    if image_inventory and image_current and (container or request.inventory is not None):
        raise AppError("ambiguous_inventory", "截图与文本或档案同时提供库存，请明确只使用一个来源；不会合并或覆盖。", 422)
    if container:
        if snapshot is None or not snapshot["scope"].get("id"):
            raise AppError("player_scope_required", "请先关联玩家状态档案。", 422)
        if request.inventory is not None:
            raise AppError("ambiguous_inventory", "文本与档案同时提供库存，请只使用一个来源。", 422)
        snapshot = project(snapshot, datetime.now(timezone.utc))
        inventory = calculation_inventory(snapshot, container)
        updates.update(inventory={key: amount for key, amount in inventory.items() if key in supported}, inventory_complete=True)
        usage["inventory_source"] = "profile"
        usage["player_state_usage"] = {"scope_id": snapshot["scope"]["id"], "revision": snapshot["revision"],
            "container_id": container, "fields": ["inventory"],
            "observed_at": snapshot["containers"][container]["observed_at"],
            "excluded_items": sorted(set(inventory) - supported.keys())}
    elif image_inventory and image_current:
        updates.update(inventory={key: value for key, value in accepted["inventory"].items() if key in supported},
                       inventory_complete=accepted["inventory_complete"])
        usage["inventory_source"] = accepted.get("source", "image_confirmed")
    elif accepted and request.inventory is None:
        updates["inventory_complete"] = False
    if accepted:
        usage["visual_usage"] = {"draft_id": visual["draft_id"], "confirmation_id": accepted["confirmation_id"],
            "observed_at": accepted["observed_at"], "complete": accepted["inventory_complete"], "subject": accepted["subject"],
            "fresh": image_current, "inventory_used": usage["inventory_source"] in {"image_confirmed", "image_inferred"},
            "source": accepted.get("source", "image_confirmed"),
            "excluded_items": sorted(set(accepted["inventory"]) - supported.keys()),
            "attachment_hashes": [item["sha256"] for item in visual.get("attachments", [])]}
    if snapshot:
        snapshot = project(snapshot, datetime.now(timezone.utc))
        for key, state_key in [("workbench", "technology.workbench"), ("blueprints", "technology.unlocked_blueprints")]:
            record = snapshot["facts"].get(state_key)
            if getattr(request, key) is None and record and record["status"] == "fresh":
                updates[key] = record["value"] if key == "workbench" else [item for item in record["value"] if item in supported]
                usage.setdefault("fact_sources", {})[key] = {"source": record.get("source"),
                    "observed_at": record["observed_at"], "scope_revision": snapshot["revision"]}
                if usage.get("player_state_usage"):
                    usage["player_state_usage"]["fields"].append(key)
    return CalculationRequest.model_validate(request.model_dump() | updates), usage

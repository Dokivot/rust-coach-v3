import copy
import json
from datetime import datetime, timedelta, timezone

from rust_coach.contracts import AppError
from rust_coach.player_state.schemas import FIELD_DEFINITIONS, StateUpdate


INVENTORY_TTL_MINUTES = 30


def timestamp(value):
    return datetime.fromisoformat(value)


def freshness(record, now):
    if record.get("invalidated_by"):
        return "invalidated"
    return "stale" if timestamp(record["expires_at"]) <= now else "fresh"


def metadata(update, ttl):
    observed = update.observed_at.astimezone(timezone.utc)
    result = {"observed_at": observed.isoformat(), "expires_at": (observed + timedelta(minutes=ttl)).isoformat(),
            "observation_id": str(update.observation_id), "source": update.source,
            "verification": "user_reported"}
    if getattr(update, "visual_draft_id", None):
        result["visual_draft_id"] = update.visual_draft_id
        result["evidence_refs"] = update.evidence_refs
    return result


def apply_update(snapshot, update: StateUpdate, now):
    if update.observed_at > now + timedelta(minutes=1):
        raise AppError("future_observation", "观测时间不能在未来。", 422)
    result = copy.deepcopy(snapshot)
    facts, containers = result["facts"], result["containers"]
    changes = update.changes
    watermarks = result.setdefault("invalidations", {})

    def check_watermark(key):
        keys = [key, "inventory.*"] if key.startswith("inventory.") else [key]
        if any(name in watermarks and timestamp(watermarks[name]) > update.observed_at for name in keys):
            raise AppError("older_observation", "这条观测早于相关失效事件，请重新确认当前状态。", 409)

    def check_order(record):
        previous = record.get("last_changed_at", record.get("observed_at"))
        if previous and timestamp(previous) > update.observed_at:
            raise AppError("older_observation", "这条观测早于已有记录，请核对时间后重新提交。", 409)

    def invalidate(record):
        check_order(record)
        record["invalidated_by"] = changes.event or str(update.observation_id)
        record["last_changed_at"] = update.observed_at.isoformat()

    if changes.event:
        for key in FIELD_DEFINITIONS:
            if (changes.event == "invalidate_all" or
                    changes.event == "death" and key.startswith("session.") or
                    changes.event == "raided" and key.startswith(("base.", "session.")) or
                    changes.event == "blueprint_reset" and key == "technology.unlocked_blueprints"):
                check_watermark(key)
                watermarks[key] = update.observed_at.isoformat()
        affected_containers = ["inventory.*"] if changes.event in {"invalidate_all", "raided"} else (
            ["inventory.backpack", "inventory.equipped", "visual.nonbase"] if changes.event == "death" else [])
        for key in affected_containers:
            check_watermark(key)
            watermarks[key] = update.observed_at.isoformat()
        for key, record in facts.items():
            affected = (changes.event == "invalidate_all" or
                        changes.event == "death" and key.startswith("session.") or
                        changes.event == "raided" and key.startswith(("base.", "session.")) or
                        changes.event == "blueprint_reset" and key == "technology.unlocked_blueprints")
            if affected:
                invalidate(record)
        for key, container in containers.items():
            if (changes.event in {"invalidate_all", "raided"} or
                    changes.event == "death" and key in {"backpack", "equipped"}):
                invalidate(container)
                for record in container["items"].values():
                    invalidate(record)
        for record in result.get("visual_observations", {}).values():
            if changes.event in {"invalidate_all", "raided"} or changes.event == "death" and record["value"]["kind"] != "base":
                invalidate(record)
        return result
    for key in changes.clear_fields:
        check_watermark(key)
        watermarks[key] = update.observed_at.isoformat()
        if key in facts:
            invalidate(facts[key])
    for key, value in changes.facts.items():
        check_watermark(key)
        if key in facts:
            check_order(facts[key])
        facts[key] = {"value": value, **metadata(update, FIELD_DEFINITIONS[key][3])}
    for key in changes.invalidate_containers:
        if key not in containers:
            raise AppError("unknown_container", "尚未记录这个容器：" + key, 422)
        invalidate(containers[key])
        watermarks["inventory." + key] = update.observed_at.isoformat()
        for record in containers[key]["items"].values():
            invalidate(record)
    for change in changes.inventory:
        check_watermark("inventory." + change.container_id)
        previous = containers.get(change.container_id)
        if previous:
            check_order(previous)
        if previous is None and len(containers) >= 20:
            raise AppError("container_limit", "每个状态档案最多记录 20 个容器。", 422)
        container = copy.deepcopy(previous) if previous else {"items": {}}
        if change.mode == "replace":
            container = {"items": {}, **metadata(update, INVENTORY_TTL_MINUTES),
                         "coverage": "complete" if change.complete else "partial"}
        else:
            container.update(metadata(update, INVENTORY_TTL_MINUTES))
            container["coverage"] = "partial"
            container.pop("invalidated_by", None)
        container["last_changed_at"] = update.observed_at.isoformat()
        for item, amount in change.items.items():
            container["items"][item] = {"value": amount, **metadata(update, INVENTORY_TTL_MINUTES)}
        if len(container["items"]) > 150:
            raise AppError("inventory_limit", "每个容器最多记录 150 种物品。", 422)
        containers[change.container_id] = container
    if changes.visual_observations:
        result["schema_version"] = 2
        visual = result.setdefault("visual_observations", {})
        for key in list(visual):
            if freshness(visual[key],now) != "fresh":
                del visual[key]
        for observation in changes.visual_observations:
            if observation.kind != "base":
                check_watermark("visual.nonbase")
            check_watermark("base.grade" if observation.kind == "base" else "inventory.equipped" if observation.kind == "equipment" else "inventory." + observation.region_kind)
            key = str(observation.attachment_id) + ":" + observation.id
            if key in visual:
                check_order(visual[key])
            visual[key] = {"value": observation.model_dump(mode="json"), **metadata(update, 120 if observation.kind == "base" else 30)}
        if len(visual) > 240:
            raise AppError("visual_state_limit", "截图观测过多，请先清理旧观测或使用新的清档档案。", 422)
    team = facts.get("session.team_size")
    online = facts.get("session.teammates_online")
    if team and online and freshness(team, now) == freshness(online, now) == "fresh" and online["value"] >= team["value"]:
        raise AppError("state_conflict", "在线队友人数不应达到或超过队伍总人数（总人数包含自己）。", 422)
    return result


def project(snapshot, now):
    result = copy.deepcopy(snapshot)
    result["evaluated_at"] = now.isoformat()
    result["unknown_fields"] = sorted(set(FIELD_DEFINITIONS) - result["facts"].keys())
    for record in result["facts"].values():
        record["status"] = freshness(record, now)
    for container in result["containers"].values():
        container["status"] = freshness(container, now)
        for record in container["items"].values():
            record["status"] = freshness(record, now)
        container["usable_for_exact_calculation"] = container["coverage"] == "complete" and container["status"] == "fresh" and all(
            record["status"] == "fresh" for record in container["items"].values())
    for record in result.get("visual_observations", {}).values():
        record["status"] = freshness(record, now)
    return result


def model_context(snapshot, max_chars=12000):
    def compact(record):
        return {key: record[key] for key in ("value", "observed_at", "expires_at", "verification")}

    result = {"scope": snapshot["scope"], "revision": snapshot["revision"], "evaluated_at": snapshot["evaluated_at"],
            "facts": {},
            "unavailable_fields": sorted(set(snapshot["unknown_fields"]) | {
                key for key, record in snapshot["facts"].items() if record["status"] != "fresh"}),
            "containers": {}, "omitted_fields": [], "context_truncated": False}
    for key, record in snapshot["facts"].items():
        if record["status"] != "fresh":
            continue
        result["facts"][key] = compact(record)
        if len(json.dumps(result, ensure_ascii=False)) > max_chars - 1500:
            del result["facts"][key]
            result["omitted_fields"].append(key)
            result["context_truncated"] = True
    for key, value in snapshot["containers"].items():
        result["containers"][key] = {"coverage": value["coverage"], "status": value["status"],
                                     "items": {}, "context_truncated": False}
        target = result["containers"][key]
        for item, record in value["items"].items():
            if record["status"] != "fresh":
                target["coverage"] = "partial"
                continue
            target["items"][item] = compact(record)
            if len(json.dumps(result, ensure_ascii=False)) > max_chars - 1500:
                del target["items"][item]
                target["context_truncated"] = True
                target["coverage"] = "partial"
                result["context_truncated"] = True
                break
        if len(json.dumps(result, ensure_ascii=False)) > max_chars - 1500:
            del result["containers"][key]
            result["context_truncated"] = True
            break
    if snapshot.get("visual_observations"):
        result["visual_observations"] = []
        for record in snapshot["visual_observations"].values():
            if record["status"] != "fresh":
                continue
            observation = compact(record)
            if len(json.dumps(result, ensure_ascii=False)) + len(json.dumps(observation, ensure_ascii=False)) > max_chars:
                result["context_truncated"] = True
                break
            result["visual_observations"].append(observation)
    return result


def calculation_inventory(snapshot, container_id):
    container = snapshot["containers"].get(container_id)
    if not container or not container["usable_for_exact_calculation"]:
        raise AppError("player_inventory_incomplete", "所选容器尚未完整盘点，或状态已过期/失效。请在玩家状态页重新完整盘点后计算。", 422)
    return {item: record["value"] for item, record in container["items"].items()}

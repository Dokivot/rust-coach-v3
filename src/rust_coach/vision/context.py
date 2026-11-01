import re
from collections import Counter
from datetime import datetime, timedelta, timezone


def visual_input(visual):
    return (visual or {}).get("accepted") or (visual or {}).get("automatic")


def resolve_screenshots(extraction, request, attachments):
    inferred = {item["attachment_id"]: item for item in extraction.get("screenshot_context", [])}
    contexts = {}
    message = request["message"]
    for attachment in attachments:
        identifier = attachment["id"]
        declared = request["image_context"].get(identifier, {})
        candidate = inferred.get(identifier)
        subjects = {item["subject"] for item in extraction["observations"] if item["attachment_id"] == identifier} - {"unknown"}
        subject = candidate["subject"] if candidate else next(iter(subjects)) if len(subjects) == 1 else "self"
        temporal = candidate["temporal_context"] if candidate else "current"
        if not candidate:
            if re.search(r"旧图|旧的.*图|昨天的.*图|昨晚的.*图|上次.*截图|之前的.*截图|old screenshot|yesterday.s screenshot", message, re.I):
                temporal = "historical"
            if re.search(r"测试图|示意图|攻略图|网图|公开.*(?:图|截图)|synthetic|example image|reference image", message, re.I):
                temporal = "example"
            if len(attachments) == 1:
                if re.search(r"(?:这是|这张|图中|图里|识别).{0,10}(?:对手|对面|敌人)|opponent.s", message, re.I):
                    subject = "opponent"
                elif re.search(r"(?:这是|这张|图中|图里|识别).{0,10}队友|teammate.s", message, re.I):
                    subject = "teammate"
        if declared.get("subject", "unknown") != "unknown":
            subject = declared["subject"]
        if subject == "unknown" and temporal == "current" and not candidate:
            subject = "self"
        observed_at = declared.get("captured_at") or attachment["created_at"]
        if datetime.fromisoformat(observed_at) > datetime.now(timezone.utc) + timedelta(minutes=1):
            temporal = "unknown"
        contexts[identifier] = {"subject": subject, "temporal_context": temporal, "observed_at": observed_at,
            "time_source": "provided" if declared.get("captured_at") else "upload_assumption",
            "reason": candidate["reason"] if candidate else "无相反说明时，默认按玩家当前截图理解。"}
    return contexts


def automatic_input(extraction, contexts):
    observations = extraction["observations"]
    inventory_rows = [item for item in observations if item["kind"] == "inventory"]
    regions = {(item["region_kind"], item["container_label"]) for item in inventory_rows}
    slots = [item["slot"] for item in inventory_rows]
    subjects = {item["subject"] for item in observations} | {value["subject"] for value in contexts.values()}
    subject = next(iter(subjects)) if len(subjects) == 1 else "unknown"
    observed_at = min(datetime.fromisoformat(value["observed_at"]) for value in contexts.values())
    current = all(value["temporal_context"] == "current" for value in contexts.values())
    complete = (extraction["inventory_coverage"] == "full_view" and len(contexts) == 1 and len(regions) <= 1
        and (bool(inventory_rows) or not observations)
        and all(item["region_kind"] in {"backpack", "hotbar", "storage", "tool_cupboard"}
            and item["item_id"] is not None and item["quantity"] is not None and item["slot"] for item in inventory_rows)
        and len(slots) == len(set(slots)) and not any(item["uncertainty"] for item in inventory_rows))
    inventory = Counter()
    if complete:
        for item in inventory_rows:
            inventory[item["item_id"]] += item["quantity"]
        if any(value > 1_000_000 for value in inventory.values()):
            complete, inventory = False, Counter()
    return {"source": "image_inferred", "confirmation_id": None, "observations": observations,
        "subject": subject, "observed_at": observed_at.isoformat(),
        "expires_at": (observed_at + timedelta(minutes=30)).isoformat(), "current_assumption": current,
        "inventory": dict(inventory), "inventory_complete": complete,
        "container_id": inventory_rows[0]["region_kind"] if inventory_rows else "backpack",
        "calculation": extraction.get("calculation"), "saved": False, "state_revision": None}

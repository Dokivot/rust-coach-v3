import copy
import re

from rust_coach.contracts import AppError
from rust_coach.vision.schemas import VisualExtraction


def correct_observation(extraction, message, items):
    names = {item["name"]: identifier for identifier, item in items.items()}
    names.update({identifier: identifier for identifier in items})
    pattern = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    identities = list(re.finditer(r"不是(?P<old>" + pattern + r")[，,\s]*(?:而)?是(?P<new>" + pattern + r")", message))
    quantities = list(re.finditer(r"(?P<item>" + pattern + r")(?:其实是|应为|数量改为|数量是|改为)(?P<quantity>\d+)", message))
    unknowns = list(re.finditer(r"(?P<item>" + pattern + r")(?:保持未知|改为未知|无法确定)", message))
    matches = identities + quantities + unknowns
    if matches and re.search(r"[？?\"“”‘’]|如果|假如|假设|要是|不要|别把|不能|昨天|举例|比如|就好了|吗", message):
        raise AppError("visual_correction_unclear", "这条信息不是明确的当前纠正，截图尚未修改。请直接说明要改的物品和数量。", 422)
    if len(matches) > 1:
        raise AppError("visual_correction_ambiguous", "一条消息包含多项或冲突的修改。请每次明确纠正一格，或在确认界面修改。", 422)
    identity = identities[0] if identities else None
    quantity = quantities[0] if quantities else None
    unknown = unknowns[0] if unknowns else None
    if not (identity or quantity or unknown):
        if re.search(r"识别错了|识别不对|不是.+是", message):
            raise AppError("visual_correction_unclear", "请明确要纠正的物品，例如“硫磺数量改为150”或“不是齿轮，是电子零件”；不会继续沿用有争议的识别。", 422)
        return None
    result = copy.deepcopy(extraction)
    match = identity or quantity or unknown
    original = names[match["old"] if identity else match["item"]]
    candidates = [item for item in result["observations"] if item["kind"] == "inventory" and item["item_id"] == original]
    if len(candidates) != 1:
        raise AppError("visual_correction_ambiguous", "截图中该物品未唯一定位，请在截图确认界面按格子修改；不会合并多个格子。", 422)
    observation = candidates[0]
    before = copy.deepcopy(observation)
    if identity:
        observation.update(item_id=names[identity["new"]], label=identity["new"], item_status="identified")
    elif quantity:
        observation.update(quantity=int(quantity["quantity"]), quantity_status="readable", raw_quantity=quantity["quantity"])
    else:
        observation.update(item_id=None, item_status="unknown")
    observation["description"] = "用户当前明确纠正；仅更新指定字段，其他观测保持原样。"
    validated = VisualExtraction.model_validate(result).model_dump(mode="json")
    return validated, {"observation_id": observation["id"], "before": before, "after": observation,
                       "source": "explicit_user_correction", "message": message}
